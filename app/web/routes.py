"""管理后台路由（T-301/T-302）。

页面全部服务端渲染（内联样式，无独立前端工程）。
登录后可用；案件审批遵循 PROJECT_CONTEXT 状态机与一次性确认码流程：
  预览（含成员信息，标注"未验证QQ号/OpenID"）→ 生成5分钟一次性确认码 → 凭码确认执行。
"""

from __future__ import annotations

import html
import json
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.cases.case_sm import IllegalTransitionError
from app.cases.models import Case, ViolationRecord
from app.db import SessionLocal
from app.reports.cleanup import purge_expired
from app.reports.service import build_daily, build_weekly, pending_manual_review
from app.web import auth
from app.web.confirm import generate, verify_and_consume

router = APIRouter(prefix="/admin", include_in_schema=False)

_STYLE = (
    "<style>body{font-family:'Microsoft YaHei',sans-serif;margin:0;background:#f4f5f7;color:#222}"
    "header{background:#1f2937;color:#fff;padding:12px 24px;display:flex;justify-content:space-between;align-items:center}"
    "main{padding:24px;max-width:1100px;margin:0 auto}"
    "table{border-collapse:collapse;width:100%;background:#fff}th,td{border:1px solid #ddd;padding:8px 10px;font-size:14px;text-align:left}"
    "th{background:#eef0f3}a{color:#2563eb;text-decoration:none}"
    ".btn{display:inline-block;padding:6px 14px;border:1px solid #ccc;border-radius:6px;background:#fff;cursor:pointer;margin-right:8px}"
    ".btn.danger{border-color:#c0392b;color:#c0392b}.btn.ok{border-color:#16804b;color:#16804b}"
    ".card{background:#fff;border:1px solid #e2e4e8;border-radius:8px;padding:16px;margin-bottom:16px}"
    ".warn{color:#b45309}.muted{color:#888;font-size:13px}pre{background:#f7f7f8;padding:10px;overflow:auto}</style>"
)


def _page(title: str, body: str, logged_in: bool = True) -> Response:
    header = ""
    if logged_in:
        header = (
            "<header><b>QQ群管理后台</b><div>"
            '<a href="/admin" style="color:#93c5fd">案件</a> &nbsp; '
            '<a href="/admin/shadow" style="color:#93c5fd">影子判定</a> &nbsp; '
            '<a href="/admin/rules" style="color:#93c5fd">规则</a> &nbsp; '
            '<a href="/admin/reports" style="color:#93c5fd">报告</a> &nbsp; '
            '<a href="/admin/logout" style="color:#fca5a5">退出</a></div></header>'
        )
    return HTMLResponse(
        f"<!DOCTYPE html><html lang=zh-CN><head><meta charset=utf-8><title>{title}</title>{_STYLE}</head>{header}<main>{body}</main></html>"
    )


def _esc(value: object) -> str:
    return html.escape(str(value))


async def _require_login(request: Request) -> str | None:
    token = request.cookies.get(auth.SESSION_COOKIE)
    if not auth.is_valid(token):
        return None
    return token


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/admin/login", status_code=303)


# ---------- 登录/退出 ----------


@router.get("/login", response_class=HTMLResponse)
async def login_page(error: str = "") -> Response:
    err = f"<p class=warn>{_esc(error)}</p>" if error else ""
    return _page(
        "登录",
        f'<div class=card style="max-width:380px;margin:60px auto"><h2>管理后台登录</h2>{err}'
        '<form method=post action=/admin/login>用户名 <input name=username style="width:100%"><br><br>'
        '密码 <input name=password type=password style="width:100%"><br><br>'
        "<button class=btn>登录</button></form></div>",
        logged_in=False,
    )


@router.post("/login")
async def login_submit(username: str = Form(""), password: str = Form("")) -> RedirectResponse:
    try:
        token = auth.login(username, password)
    except auth.AuthError as exc:
        return RedirectResponse(f"/admin/login?error={_esc(str(exc))}", status_code=303)
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(auth.SESSION_COOKIE, token, httponly=True, samesite="lax")
    return resp


@router.get("/logout")
async def logout_page(request: Request) -> RedirectResponse:
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        auth.logout(token)
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


# ---------- 案件列表 ----------


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> Response:
    if not await _require_login(request):
        return _login_redirect()
    async with SessionLocal() as session:
        pending = await pending_manual_review(session)
        stmt = select(Case).order_by(Case.created_at.desc()).limit(50)
        cases = (await session.execute(stmt)).scalars().all()
    rows = "".join(
        f"<tr><td>{_esc(c.case_no)}</td><td>{_esc(c.member_openid)}</td>"
        f"<td>{_esc(c.status)}</td><td>{_esc(f'{c.created_at:%m-%d %H:%M}')}</td>"
        f'<td><a href="/admin/cases/{c.id}">查看</a></td></tr>'
        for c in cases
    )
    pending_rows = (
        "".join(f"<li>{_esc(p['case_no'])} 成员{_esc(p['member_openid'])}</li>" for p in pending)
        or "<li>无</li>"
    )
    body = (
        f"<h2>待人工处理（{len(pending)}）</h2><ul>{pending_rows}</ul>"
        f"<h2>最近案件</h2><table><tr><th>批次号</th><th>成员</th><th>状态</th><th>创建</th><th></th></tr>{rows}</table>"
    )
    return _page("案件列表", body)


# ---------- 案件详情 ----------


async def _load_case(case_id: int) -> tuple[Case, list[ViolationRecord]]:
    async with SessionLocal() as session:
        case = await session.get(Case, case_id)
        if case is None:
            raise HTTPException(404, "案件不存在")
        ids = json.loads(case.violation_ids_json)
        records: list[ViolationRecord] = []
        for vid in ids:
            record = await session.get(ViolationRecord, int(vid))
            if record:
                records.append(record)
        return case, records


def _evidence_html(records: list[ViolationRecord]) -> str:
    parts: list[str] = []
    for r in records:
        snapshot: dict[str, Any] = json.loads(r.message_snapshot_json)
        text = _esc(snapshot.get("text", "(已按保留期清理)"))
        snapshot.get("sender") or {}
        parts.append(
            f"<div class=card><b>违规 #{r.id}</b>（{_esc(r.category)}，置信度{_esc(r.confidence)}，"
            f"{_esc(f'{r.created_at:%m-%d %H:%M}')}）{_esc(' 已撤销' if r.revoked else '')}"
            f"<p>成员OpenID：<code>{_esc(r.member_openid)}</code> <span class=warn>（未验证QQ号，仅供人工核对）</span></p>"
            f"<pre>{text[:500]}</pre></div>"
        )
    return "".join(parts) or "<p>无证据记录</p>"


@router.get("/cases/{case_id}", response_class=HTMLResponse)
async def case_detail(request: Request, case_id: int, code: str = "", notice: str = "") -> Response:
    if not await _require_login(request):
        return _login_redirect()
    case, records = await _load_case(case_id)
    show_code = (
        f'<p>一次性确认码（5分钟有效，仅显示一次）：<b style="font-size:22px">{_esc(code)}</b></p>'
        if code
        else ""
    )
    notice_html = f"<p class=warn>{_esc(notice)}</p>" if notice else ""
    evidence = _evidence_html(records)
    buttons = ""
    if case.status == "PENDING_REVIEW":
        buttons = (
            '<form method=post style="display:inline" action="'
            + f"/admin/cases/{case.id}/approve-manual"
            + '"><button class=btn ok>① 人工处理：预览并生成确认码</button></form>'
            '<form method=post style="display:inline" action="'
            + f"/admin/cases/{case.id}/keep"
            + '"><button class=btn>保留（不处罚）</button></form>'
            '<form method=post style="display:inline" action="'
            + f"/admin/cases/{case.id}/false-positive"
            + '"><button class=btn>标记误判（撤销违规）</button></form>'
        )
    elif case.status == "MANUAL_PENDING":
        buttons = (
            f'<form method=post action="/admin/cases/{case.id}/confirm-kick">'
            "已在QQ客户端手动踢出？输入确认码：<input name=code maxlength=6 style=width:90px> "
            '<button class="btn danger">② 确认已踢出</button></form>'
            f'<form method=post action="/admin/cases/{case.id}/cancel" style="margin-top:8px">'
            "<button class=btn>无法确认成员/取消</button></form>"
        )
    audit = _esc(json.dumps(json.loads(case.audit_json), ensure_ascii=False, indent=1))
    body = (
        f"<h2>案件 {_esc(case.case_no)}</h2>{notice_html}{show_code}"
        f"<div class=card><p>状态：<b>{_esc(case.status)}</b>　成员OpenID：<code>{_esc(case.member_openid)}</code> "
        "<span class=warn>（未验证QQ号）</span>　群：<code>"
        + _esc(case.group_openid)
        + "</code></p>"
        f"<p>证据：</p>{evidence}</div>"
        f"<div class=card><h3>操作</h3>{buttons or '<p class=muted>案件已终态，无可用操作</p>'}</div>"
        f"<div class=card><h3>审计记录</h3><pre>{audit}</pre></div>"
    )
    return _page(f"案件 {case.case_no}", body)


# ---------- 审批动作 ----------


@router.post("/cases/{case_id}/approve-manual")
async def approve_manual(request: Request, case_id: int) -> Response:
    token = await _require_login(request)
    if not token:
        return _login_redirect()
    operator = await _operator(request)
    case, _ = await _load_case(case_id)
    try:
        await transition_with_session(case_id, "APPROVED_MANUAL", operator)
        await transition_with_session(case_id, "MANUAL_PENDING", operator)
    except IllegalTransitionError as exc:
        return _page(
            "错误",
            f'<div class=card><p class=warn>{_esc(str(exc))}</p><a href="/admin/cases/{case_id}">返回</a></div>',
        )
    code = generate(case_id)
    return await case_detail(request, case_id, code=code)


@router.post("/cases/{case_id}/confirm-kick")
async def confirm_kick(request: Request, case_id: int, code: str = Form("")) -> Response:
    token = await _require_login(request)
    if not token:
        return _login_redirect()
    if not verify_and_consume(case_id, code):
        return await case_detail(request, case_id, notice="确认码错误或已过期，请重新生成预览")
    try:
        await transition_with_session(case_id, "KICKED", await _operator(request))
        await transition_with_session(case_id, "CLOSED", await _operator(request))
    except IllegalTransitionError as exc:
        return _page(
            "错误",
            f'<div class=card><p class=warn>{_esc(str(exc))}</p><a href="/admin/cases/{case_id}">返回</a></div>',
        )
    return RedirectResponse(
        f"/admin/cases/{case_id}?notice=已记录为人工踢出并结案", status_code=303
    )


@router.post("/cases/{case_id}/keep")
async def keep_case(request: Request, case_id: int) -> Response:
    if not await _require_login(request):
        return _login_redirect()
    try:
        await transition_with_session(case_id, "KEEP", await _operator(request))
        await transition_with_session(case_id, "CLOSED", await _operator(request))
    except IllegalTransitionError as exc:
        return _page("错误", f"<div class=card><p class=warn>{_esc(str(exc))}</p></div>")
    return RedirectResponse("/admin", status_code=303)


@router.post("/cases/{case_id}/false-positive")
async def false_positive(request: Request, case_id: int) -> Response:
    if not await _require_login(request):
        return _login_redirect()
    case, records = await _load_case(case_id)
    try:
        await transition_with_session(case_id, "FALSE_POSITIVE", await _operator(request))
        await transition_with_session(case_id, "STRIKE_REVOKED", await _operator(request))
        await transition_with_session(case_id, "CLOSED", await _operator(request))
        async with SessionLocal() as session:
            for r in records:
                stored = await session.get(ViolationRecord, r.id)
                if stored and not stored.revoked:
                    stored.revoked = True
                    stored.revoke_reason = f"管理员标记误判（案件{case.case_no}）"
            await session.commit()
    except IllegalTransitionError as exc:
        return _page("错误", f"<div class=card><p class=warn>{_esc(str(exc))}</p></div>")
    return RedirectResponse("/admin", status_code=303)


@router.post("/cases/{case_id}/cancel")
async def cancel_case(request: Request, case_id: int) -> Response:
    if not await _require_login(request):
        return _login_redirect()
    try:
        await transition_with_session(case_id, "CANCELLED", await _operator(request))
        await transition_with_session(case_id, "CLOSED", await _operator(request))
    except IllegalTransitionError as exc:
        return _page("错误", f"<div class=card><p class=warn>{_esc(str(exc))}</p></div>")
    return RedirectResponse("/admin", status_code=303)


async def _operator(request: Request) -> str:
    return f"web:{(request.cookies.get(auth.SESSION_COOKIE) or '')[:8]}"


async def transition_with_session(case_id: int, target: str, operator: str) -> None:
    from app.cases.service import transition_case

    async with SessionLocal() as session:
        await transition_case(session, case_id, target, operator)


# ---------- 影子判定视图 ----------


VERDICT_ZH = {"allow": "放行", "record_only": "转人工复核", "violation_high": "高置信违规"}
KIND_ZH = {
    "text": "文字",
    "image": "图片",
    "gif": "动图",
    "voice": "语音",
    "video": "视频",
    "file": "文件",
    "forward_record": "转发记录",
    "share_card": "分享卡片",
    "mixed": "图文混合",
    "unknown": "未知",
}


def _zh_verdict(verdict: str) -> str:
    return VERDICT_ZH.get(verdict, verdict)


def _zh_kind(kind: str) -> str:
    return KIND_ZH.get(kind, kind)


@router.get("/shadow", response_class=HTMLResponse)
async def shadow_page(request: Request, verdict: str = "") -> Response:
    if not await _require_login(request):
        return _login_redirect()
    from app.models import GroupAlias
    from app.runtime.models import ShadowDecision

    async with SessionLocal() as session:
        stmt = select(ShadowDecision).order_by(ShadowDecision.created_at.desc()).limit(100)
        if verdict:
            stmt = stmt.where(ShadowDecision.verdict == verdict)
        records = (await session.execute(stmt)).scalars().all()
        distinct_groups = sorted({r.group_openid for r in records})
        group_rows = await session.execute(
            select(GroupAlias).where(GroupAlias.group_openid.in_(distinct_groups))
        )
        group_names = {g.group_openid: g.name for g in group_rows.scalars()}

    counts: dict[str, int] = {}
    for r in records:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    summary = "、".join(f"{_zh_verdict(k)}={v}" for k, v in sorted(counts.items())) or "暂无"

    def _beijing(naive_utc: Any) -> str:
        """数据库存 naive UTC，展示转为北京时间。"""
        return f"{naive_utc + timedelta(hours=8):%m-%d %H:%M:%S}"

    def _member_display(record: Any) -> str:
        name = _esc(getattr(record, "sender_name", "") or "")
        if name:
            return f"<b>{name}</b>"
        return f"<code>{_esc(record.member_openid[:16])}…</code>"

    def _group_display(openid: str) -> str:
        name = group_names.get(openid)
        return _esc(name) if name else f"<code>{_esc(openid[:10])}…</code>"

    rows = "".join(
        "<tr>"
        f"<td>{_esc(_beijing(r.created_at))}</td>"
        f"<td>{_group_display(r.group_openid)}</td>"
        f"<td>{_zh_kind(r.kind)}</td>"
        f"<td>{_zh_verdict(r.verdict)}</td>"
        f"<td>{r.confidence}</td>"
        f"<td>{_member_display(r)}</td>"
        f"<td>{_esc(r.reason[:80])}</td>"
        "</tr>"
        for r in records
    )

    group_forms = "".join(
        "<tr><td><code>" + _esc(openid[:20]) + "…</code></td>"
        "<td>"
        + (
            _esc(group_names[openid])
            if openid in group_names
            else "<span class=muted>未备注</span>"
        )
        + "</td>"
        f'<td><form method=post action="/admin/groups/alias" style="display:flex;gap:6px">'
        f'<input type=hidden name=group_openid value="{_esc(openid)}">'
        '<input name=name placeholder="群名称" style="width:160px">'
        "<button class=btn>保存</button></form></td></tr>"
        for openid in distinct_groups
    )

    body = (
        f"<h2>影子模式判定（最近100条）</h2><p>分布：{_esc(summary)}　"
        "<span class=muted>影子模式只记录不处罚；时间为北京时间</span></p>"
        "<table><tr><th>时间</th><th>群</th><th>类型</th><th>判定</th><th>置信度</th><th>成员（群昵称）</th><th>原因</th></tr>"
        f"{rows}</table>"
        '<div class=card style="margin-top:20px"><h3>群名称备注</h3>'
        "<p class=muted>官方接口只提供群加密OpenID。把下面各OpenID对应的群名填一次，"
        "之后列表将直接显示群名称。</p>"
        f"<table><tr><th>群OpenID</th><th>已备注</th><th>备注操作</th></tr>{group_forms}</table></div>"
    )
    return _page("影子判定", body)


@router.post("/groups/alias")
async def save_group_alias(
    request: Request,
    group_openid: str = Form(""),
    name: str = Form(""),
) -> Response:
    if not await _require_login(request):
        return _login_redirect()
    if not group_openid:
        return RedirectResponse("/admin/shadow", status_code=303)
    async with SessionLocal() as session:
        from app.models import GroupAlias

        existing = await session.get(GroupAlias, group_openid)
        if existing is None:
            existing = GroupAlias(group_openid=group_openid)
            session.add(existing)
        existing.name = name.strip()[:64]
        await session.commit()
    return RedirectResponse("/admin/shadow", status_code=303)


# ---------- 规则视图 ----------


@router.get("/rules", response_class=HTMLResponse)
async def rules_page() -> Response:
    from app.moderation.rules import BLACKLIST_EXPLICIT, SOFT_SIGNALS

    blacklist = "、".join(_esc(w) for w in BLACKLIST_EXPLICIT)
    soft = "、".join(_esc(w) for w in SOFT_SIGNALS)
    body = (
        "<div class=card><h3>允许内容</h3><ul>"
        "<li>群主/管理员内容（保护角色，只记录不处罚）</li>"
        "<li>带「万能校园墙」小程序码的分享图（白名单）</li>"
        "<li>正常聊天内容</li></ul></div>"
        "<div class=card><h3>违规类别</h3><ul>"
        "<li>广告/引流（兼职、刷单、代发、房产等）——自动撤回+禁言+警告</li>"
        "<li>色情/暴力/血腥/恐怖——只记录转人工复核</li>"
        "<li>刷屏：1分钟>5条相同字样/图片/表情包</li></ul></div>"
        f"<div class=card><h3>黑名单词（{len(BLACKLIST_EXPLICIT)}）</h3><p>{blacklist}</p></div>"
        f"<div class=card><h3>弱信号词（{len(SOFT_SIGNALS)}，组合达到阈值才处罚）</h3><p>{soft}</p></div>"
    )
    return _page("规则", body)


# ---------- 报告 ----------


@router.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request) -> Response:
    if not await _require_login(request):
        return _login_redirect()
    async with SessionLocal() as session:
        daily = await build_daily(session)
        weekly = await build_weekly(session)
        pending = await pending_manual_review(session)
    body = (
        "<div class=card><h3>昨日日报</h3><pre>"
        + _esc(json.dumps(daily, ensure_ascii=False, indent=1))
        + "</pre></div>"
        "<div class=card><h3>近7天周报</h3><pre>"
        + _esc(json.dumps(weekly, ensure_ascii=False, indent=1))
        + "</pre></div>"
        f"<div class=card><h3>待人工处理清单（{len(pending)}）</h3><ul>"
        + "".join(f"<li>{_esc(p['case_no'])} — {_esc(p['member_openid'])}</li>" for p in pending)
        + "</ul></div>"
        "<div class=card><h3>数据保留清理</h3>"
        '<form method=post action="/admin/cleanup"><button class=btn>立即执行保留期清理</button></form></div>'
    )
    return _page("报告", body)


@router.post("/cleanup")
async def cleanup_now(request: Request) -> RedirectResponse:
    if not await _require_login(request):
        return _login_redirect()
    async with SessionLocal() as session:
        await purge_expired(session)
    return RedirectResponse("/admin/reports", status_code=303)
