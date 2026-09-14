"""管理后台路由（T-301/T-302）。

页面全部服务端渲染（内联样式，无独立前端工程）。
登录后可用；案件审批遵循 PROJECT_CONTEXT 状态机与一次性确认码流程：
  预览（含成员信息，标注"未验证QQ号/OpenID"）→ 生成5分钟一次性确认码 → 凭码确认执行。
"""

from __future__ import annotations

import html
import json
import secrets
from datetime import timedelta
from hashlib import sha256
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.cases.case_sm import IllegalTransitionError
from app.cases.models import Case, ViolationRecord
from app.config import get_settings
from app.db import SessionLocal
from app.models import AdminAudit
from app.reports.cleanup import purge_expired
from app.reports.service import build_daily, build_weekly, pending_manual_review
from app.reports.stats import build_stats
from app.web import auth

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


def _page(
    title: str,
    body: str,
    logged_in: bool = True,
    refresh_seconds: int | None = None,
    *,
    extra_head: str = "",
) -> Response:
    header = ""
    if logged_in:
        header = (
            "<header><b>QQ群管理后台</b><div>"
            '<a href="/admin" style="color:#93c5fd">案件</a> &nbsp; '
            '<a href="/admin/shadow" style="color:#93c5fd">影子判定</a> &nbsp; '
            '<a href="/admin/rules" style="color:#93c5fd">规则</a> &nbsp; '
            '<a href="/admin/feedback" style="color:#93c5fd">反馈学习</a> &nbsp; '
            '<a href="/admin/groups" style="color:#93c5fd">群管理</a> &nbsp; '
            '<a href="/admin/stats" style="color:#93c5fd">统计</a> &nbsp; '
            '<a href="/admin/reports" style="color:#93c5fd">报告</a> &nbsp; '
            '<a href="/admin/notifications" style="color:#93c5fd">通知与接手</a> &nbsp; '
            '<a href="/admin/settings" style="color:#93c5fd">设置</a> &nbsp; '
            '<a href="/admin/logout" style="color:#fca5a5">退出</a></div></header>'
        )
    refresh = ""
    if refresh_seconds:
        # 仅用脚本刷新（不用meta硬刷新）：正在打字/填写反馈时跳过本次刷新
        interval_ms = refresh_seconds * 1000
        refresh = (
            "<script>setInterval(function(){var a=document.activeElement;"
            "if(a&&(a.tagName==='INPUT'||a.tagName==='SELECT'||a.tagName==='TEXTAREA'))return;"
            "var i=document.querySelectorAll('input[name=reason]');"
            "for(var j=0;j<i.length;j++){if(i[j].value)return;}"
            f"location.reload();}}, {interval_ms});</script>"
        )
    return HTMLResponse(
        f"<!DOCTYPE html><html lang=zh-CN><head><meta charset=utf-8><title>{title}</title>"
        f"{refresh}{_STYLE}{extra_head}</head>{header}<main>{body}</main></html>"
    )


def _esc(value: object) -> str:
    return html.escape(str(value))


def _kpis(cards: list[tuple[str, object]]) -> str:
    items = "".join(
        "<div class=card style='flex:1;min-width:140px;text-align:center;margin:0 8px 16px 0'>"
        f"<div style='font-size:26px;font-weight:600'>{_esc(value)}</div>"
        f"<div class=muted>{_esc(label)}</div></div>"
        for label, value in cards
    )
    return f"<div style='display:flex;flex-wrap:wrap'>{items}</div>"


def _bars(title: str, rows: list[tuple[str, int]]) -> str:
    if not rows:
        return f"<div class=card><h3>{_esc(title)}</h3><p class=muted>暂无数据</p></div>"
    maxv = max(v for _, v in rows) or 1
    items = []
    for label, value in rows:
        pct = (value / maxv * 100) if maxv else 0
        items.append(
            "<div style='display:flex;align-items:center;margin:4px 0'>"
            f"<div style='width:150px;font-size:13px'>{_esc(label)}</div>"
            "<div style='flex:1;background:#eef0f3;border-radius:4px;height:18px'>"
            f"<div style='width:{pct:.1f}%;background:#2563eb;height:18px;border-radius:4px'></div></div>"
            f"<div style='width:60px;text-align:right;font-size:13px'>{value}</div></div>"
        )
    return f"<div class=card><h3>{_esc(title)}</h3>{''.join(items)}</div>"


def _ai_model_table(models: list[dict[str, Any]]) -> str:
    if not models:
        return "<div class=card><h3>AI 调用（按模型）</h3><p class=muted>暂无调用</p></div>"
    rows = "".join(
        f"<tr><td>{_esc(m['model'])}</td><td>{m['calls']}</td><td>{m['ok']}</td>"
        f"<td>{m['fail']}</td><td>{m['avg_ms']}ms</td><td>未核算（以供应商账单为准）</td></tr>"
        for m in models
    )
    return (
        "<div class=card><h3>AI 调用（按模型）</h3>"
        "<table><tr><th>模型</th><th>调用</th><th>成功</th><th>失败</th>"
        "<th>平均延迟</th><th>费用</th></tr>" + rows + "</table></div>"
    )


def _ai_daily_table(daily: list[dict[str, Any]]) -> str:
    """AI 每日消耗表（调用量/成功率/延迟/费用）。"""
    if not daily:
        return "<div class=card><h3>AI 每日消耗</h3><p class=muted>暂无AI调用记录</p></div>"
    rows = "".join(
        f"<tr><td>{_esc(d['day'])}</td><td>{d['calls']}</td>"
        f"<td>{d['primary_calls']}</td><td>{d['secondary_calls']}</td>"
        f"<td>{d['cache_hits']}</td><td>{d['blocked_calls']}</td>"
        f"<td>{d['fail']}</td><td>{d['avg_latency_ms']}ms</td><td>未核算</td></tr>"
        for d in daily
    )
    return (
        "<div class=card><h3>AI 每日消耗（UTC，金额未核算）</h3>"
        "<p class=muted>实际调用不含缓存和本地额度拒绝；旧记录未区分主/次模型时不猜测补齐。供应商账单为准。</p>"
        "<table><tr><th>日期</th><th>实际调用</th><th>主模型</th><th>次模型</th>"
        "<th>缓存</th><th>额度阻断</th><th>失败</th>"
        "<th>平均延迟</th><th>费用</th></tr>" + rows + "</table></div>"
    )


def _stats_body(stats: dict[str, Any]) -> str:
    t = stats["totals"]
    ai_fail_rate = (t["ai_failed"] / t["ai_calls"] * 100) if t["ai_calls"] else 0.0
    agr = stats["agreement"]
    agree_label = f"{agr['rate'] * 100:.0f}%" if agr["total"] else "—"
    kpis = _kpis(
        [
            ("总处理消息", t["shadow"]),
            ("违规记录", t["violations"]),
            ("待审案件", t["pending_cases"]),
            ("已标注反馈", t["feedback"]),
            ("AI 调用", t["ai_calls"]),
            ("主模型调用", stats["ai_usage"]["primary_calls"]),
            ("次模型调用", stats["ai_usage"]["secondary_calls"]),
            ("缓存命中", stats["ai_usage"]["cache_hits"]),
            ("额度阻断", stats["ai_usage"]["blocked_calls"]),
            ("AI 失败率", f"{ai_fail_rate:.1f}%"),
            ("已标注样本一致率（非验收）", agree_label),
        ]
    )
    return (
        kpis
        + _bars("判定分布", stats["verdicts"])
        + _bars("违规类别分布", stats["categories"])
        + _bars("最近7天每日处理量", stats["last7"])
        + _ai_model_table(stats["ai_by_model"])
        + _bars("人工反馈标注分布", stats["feedback_labels"])
        + _bars("候选规则状态", stats["candidate_status"])
    )


async def _require_login(request: Request) -> str | None:
    token = request.cookies.get(auth.SESSION_COOKIE)
    if not auth.is_valid(token):
        return None
    return token


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/admin/login", status_code=303)


def _rules_notice_redirect(notice: str) -> RedirectResponse:
    return RedirectResponse(f"/admin/rules?notice={quote(notice)}", status_code=303)


def _feedback_notice_redirect(notice: str) -> RedirectResponse:
    return RedirectResponse(f"/admin/feedback?notice={quote(notice)}", status_code=303)


def _csrf_field(session_token: str) -> str:
    return f'<input type=hidden name="csrf" value="{_esc(auth.csrf_token(session_token))}">'


async def _require_admin_post(request: Request, csrf: str) -> str:
    token = await _require_login(request)
    if not token:
        raise HTTPException(401, "需要先登录管理后台")
    if not auth.validate_csrf(token, csrf):
        raise HTTPException(403, "CSRF token invalid")
    return token


async def record_admin_audit(
    operator: str,
    action: str,
    target_type: str,
    target_id: str,
    details: dict[str, Any] | None = None,
) -> None:
    safe_details = details or {}
    async with SessionLocal() as session:
        session.add(
            AdminAudit(
                operator=operator,
                action=action,
                target_type=target_type,
                target_id=str(target_id)[:128],
                detail_json=json.dumps(safe_details, ensure_ascii=False),
            )
        )
        await session.commit()


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
async def login_submit(
    request: Request, username: str = Form(""), password: str = Form("")
) -> RedirectResponse:
    try:
        token = auth.login(username, password)
    except auth.AuthError as exc:
        return RedirectResponse(f"/admin/login?error={_esc(str(exc))}", status_code=303)
    settings = get_settings()
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(
        auth.SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=settings.admin_session_ttl_seconds,
    )
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

# 案件状态中文名（未知状态回退英文原文）
STATUS_ZH = {
    "PENDING_REVIEW": "待处理",
    "APPROVED_MANUAL": "已批准人工",
    "MANUAL_PENDING": "待确认踢出",
    "APPROVED_NAPCAT": "已批准自动",
    "CONFIRM_PENDING": "待执行确认",
    "EXECUTING": "执行中",
    "KICKED": "已踢出",
    "FAILED": "执行失败",
    "CANCELLED": "已取消",
    "KEEP": "保留不处罚",
    "FALSE_POSITIVE": "误判",
    "STRIKE_REVOKED": "违规已撤销",
    "CLOSED": "已关闭",
}


def _status_zh(status: str) -> str:
    return STATUS_ZH.get(status, status)


async def _alias_map(session: AsyncSession) -> dict[str, str]:
    """group_openid -> 群备注名（案件/群管理列表共用）。"""
    from app.models import GroupAlias

    return {
        row.group_openid: row.name
        for row in (await session.scalars(select(GroupAlias))).all()
        if row.name.strip()
    }


def _group_display(alias_map: dict[str, str], group: str) -> str:
    """群名优先，无备注回退群号。"""
    name = alias_map.get(group or "")
    return name if name else (group or "-")


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    notice: str = "",
    page: int = 1,
    status: str = "",
    group: str = "",
    date_from: str = "",
    date_to: str = "",
) -> Response:
    token = await _require_login(request)
    if not token:
        return _login_redirect()
    csrf = _csrf_field(token)
    page = max(1, page)
    page_size = 50
    async with SessionLocal() as session:
        pending = await pending_manual_review(session)
        alias_map = await _alias_map(session)
        conditions = []
        if status:
            conditions.append(Case.status == status)
        if group.strip():
            # 支持群号或群名（含别名模糊匹配）
            needle = group.strip()
            matched = {gid for gid, name in alias_map.items() if needle in name}
            if needle.isdigit() or needle not in matched:
                matched.add(needle)
            conditions.append(Case.group_openid.in_(matched))
        if date_from:
            conditions.append(Case.created_at >= f"{date_from} 00:00:00")
        if date_to:
            conditions.append(Case.created_at <= f"{date_to} 23:59:59")
        where = [*conditions] if conditions else []
        base = select(Case)
        if where:
            base = base.where(*where)
        total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
        cases = (
            (
                await session.execute(
                    base.order_by(Case.created_at.desc())
                    .limit(page_size)
                    .offset((page - 1) * page_size)
                )
            )
            .scalars()
            .all()
        )
    total_pages = max(1, (total + page_size - 1) // page_size)

    def _qs(p: int) -> str:
        from urllib.parse import urlencode

        return "?" + urlencode(
            {k: v for k, v in {
                "page": p, "status": status, "group": group,
                "date_from": date_from, "date_to": date_to,
            }.items() if v}
        ) or "?"

    rows = "".join(
        f"<tr><td><input type=checkbox name=case_ids value={c.id}></td>"
        f"<td><a href=/admin/cases/{c.id}>{_esc(c.case_no)}</a></td>"
        f"<td>{_esc(_group_display(alias_map, c.group_openid))}<span class=muted> {_esc(c.group_openid)}</span></td>"
        f"<td>{_esc(c.member_openid)}</td>"
        f"<td>{_esc(_status_zh(c.status))}</td><td>{_esc(f'{c.created_at:%m-%d %H:%M}')}</td>"
        f'<td><a href="/admin/cases/{c.id}">查看</a></td></tr>'
        for c in cases
    )
    status_opts = "".join(
        f'<option value="{code}" {"selected" if status == code else ""}>{zh}</option>'
        for code, zh in sorted(STATUS_ZH.items())
    )
    filter_form = (
        '<form method=get class=card style="display:flex;gap:12px;flex-wrap:wrap;align-items:end">'
        "<label>状态 <select name=status><option value=''>全部</option>"
        f"{status_opts}</select></label>"
        f'<label>群（群号或群名） <input name=group value="{_esc(group)}" style=width:140px></label>'
        f'<label>从 <input type=date name=date_from value="{_esc(date_from)}"></label>'
        f'<label>至 <input type=date name=date_to value="{_esc(date_to)}"></label>'
        "<button class=btn>筛选</button>"
        '<a class=btn href="/admin">重置</a></form>'
    )
    batch_form = (
        '<form method=post action="/admin/cases/batch-delete" '
        'onsubmit="return confirm(\'确定删除勾选的案件吗？将同时删除其违规证据记录，不可恢复。\')">'
        f"{csrf}"
        f"<table><tr><th><input type=checkbox onclick=\"document.querySelectorAll('input[name=case_ids]').forEach(c=>c.checked=this.checked)\" title=全选></th>"
        f"<th>批次号</th><th>群</th><th>成员</th><th>状态</th><th>创建</th><th></th></tr>{rows}</table>"
        f'<p style=margin-top:8px><button class="btn danger">删除勾选案件</button>'
        f'<span class=muted>（共 {total} 条，第 {page}/{total_pages} 页）</span></p></form>'
    )
    pager = (
        '<p>'
        + (f'<a class=btn href="/admin{_qs(page - 1)}">上一页</a>' if page > 1 else "")
        + (f'<a class=btn href="/admin{_qs(page + 1)}">下一页</a>' if page < total_pages else "")
        + "</p>"
        if total_pages > 1
        else ""
    )
    pending_rows = (
        "".join(
            f"<li>{_esc(p['case_no'])}　群 {_esc(_group_display(alias_map, p.get('group_openid') or ''))}　"
            f"成员{_esc(p['member_openid'])}</li>"
            for p in pending
        )
        or "<li>无</li>"
    )
    notice_html = f'<p class=warn>{_esc(notice)}</p>' if notice else ""
    body = (
        f"{notice_html}<h2>待人工处理（{len(pending)}）</h2><ul>{pending_rows}</ul>"
        f"<h2>案件</h2>{filter_form}{batch_form}{pager}"
    )
    return _page("案件列表", body)


# ---------- 统计大盘 ----------


@router.get("/stats", response_class=HTMLResponse)
async def stats_dashboard(request: Request) -> Response:
    if not await _require_login(request):
        return _login_redirect()
    async with SessionLocal() as session:
        stats = await build_stats(session)
        ai_daily = await _build_ai_daily_stats(session)
    return _page("统计大盘", _stats_body(stats) + _ai_daily_table(ai_daily))


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
    token = await _require_login(request)
    if not token:
        return _login_redirect()
    csrf = _csrf_field(token)
    case, records = await _load_case(case_id)
    async with SessionLocal() as session:
        alias_map = await _alias_map(session)
    group_name = alias_map.get(case.group_openid or "", "")
    group_html = (
        f"{_esc(group_name)} <code>{_esc(case.group_openid)}</code>"
        if group_name
        else f"<code>{_esc(case.group_openid)}</code>"
    )
    notice_html = f"<p class=warn>{_esc(notice)}</p>" if notice else ""
    evidence = _evidence_html(records)
    buttons = ""
    if case.status == "PENDING_REVIEW":
        buttons = (
            '<form method=post style="display:inline" action="'
            + f"/admin/cases/{case.id}/manual-kick"
            + f'" onsubmit="return confirm(\'确认人工处理：已在QQ客户端踢出该成员？点击后案件记为已踢出并结案。\')'
            + f'">{csrf}<button class="btn danger">人工处理（确认踢出并结案）</button></form>'
            '<form method=post style="display:inline" action="'
            + f"/admin/cases/{case.id}/keep"
            + f'" onsubmit="return confirm(\'确认保留该成员、不做处罚？\')'
            + f'">{csrf}<button class=btn>保留（不处罚）</button></form>'
            '<form method=post style="display:inline" action="'
            + f"/admin/cases/{case.id}/false-positive"
            + f'" onsubmit="return confirm(\'确认为误判？将撤销该案件全部违规记录。\')'
            + f'">{csrf}<button class=btn>标记误判（撤销违规）</button></form>'
        )
    elif case.status == "MANUAL_PENDING":
        # 存量案件兼容：旧流程（已批准、待确认码）仍可确认/取消
        buttons = (
            f'<form method=post action="/admin/cases/{case.id}/confirm-kick">'
            f"{csrf}已在QQ客户端手动踢出？输入确认码：<input name=code maxlength=6 style=width:90px> "
            '<button class="btn danger">确认已踢出</button></form>'
            f'<form method=post action="/admin/cases/{case.id}/cancel" style="margin-top:8px" '
            f'onsubmit="return confirm(\'确认取消该案件？\')">'
            f"{csrf}<button class=btn>无法确认成员/取消</button></form>"
        )
    audit = _esc(json.dumps(json.loads(case.audit_json), ensure_ascii=False, indent=1))
    body = (
        f"<h2>案件 {_esc(case.case_no)}</h2>{notice_html}"
        f"<div class=card><p>状态：<b>{_esc(_status_zh(case.status))}</b>　成员OpenID：<code>{_esc(case.member_openid)}</code> "
        "<span class=warn>（未验证QQ号）</span>　群："
        + group_html
        + "</p>"
        f"<p>证据（{len(records)} 条）：</p>{evidence}</div>"
        f"<div class=card><h3>操作</h3>{buttons or '<p class=muted>案件已终态，无可用操作</p>'}</div>"
        f"<div class=card><h3>审计记录</h3><pre>{audit}</pre></div>"
    )
    return _page(f"案件 {case.case_no}", body)


# ---------- 审批动作 ----------


def _transition_error_html(case_id: int, exc: IllegalTransitionError) -> str:
    """友好化状态机报错：给出原因解释与当前可执行的操作入口。"""
    msg = str(exc)
    hint = ""
    if "KEEP" in msg or "FALSE_POSITIVE" in msg:
        if "MANUAL_PENDING" in msg:
            hint = "该案件已在人工处理流程中（已批准待踢出），不能再改为保留/误判。"
        hint += "请刷新案件页后按当前状态选择可用操作。"
    elif "CLOSED" in msg:
        hint = "该案件已关闭（终态），不能再变更。"
    return (
        f'<div class=card><p class=warn>操作未执行：{_esc(msg)}</p>'
        f"<p class=muted>{_esc(hint)}</p>"
        f'<p><a href="/admin/cases/{case_id}">返回案件页（页面已按最新状态显示可用操作）</a>　'
        f'<a href="/admin">返回案件列表</a></p></div>'
    )


@router.post("/cases/{case_id}/manual-kick")
async def manual_kick(request: Request, case_id: int, csrf: str = Form("")) -> Response:
    """人工处理一键确认：记录为已踢出并结案（负责人 2026-09-12 简化，取消确认码）。"""
    await _require_admin_post(request, csrf)
    operator = await _operator(request)
    case, _ = await _load_case(case_id)
    try:
        for target in ("APPROVED_MANUAL", "MANUAL_PENDING", "KICKED", "CLOSED"):
            await transition_with_session(case_id, target, operator)
        await record_admin_audit(
            operator,
            "case_manual_kick",
            "case",
            str(case_id),
            {"case_no": case.case_no, "mode": "one_click"},
        )
    except IllegalTransitionError as exc:
        return _page("操作未执行", _transition_error_html(case_id, exc))
    return RedirectResponse(f"/admin/cases/{case_id}?notice=已记录为人工踢出并结案", status_code=303)


@router.post("/cases/{case_id}/confirm-kick")
async def confirm_kick(
    request: Request, case_id: int, code: str = Form(""), csrf: str = Form("")
) -> Response:
    """存量案件兼容：旧流程（已批准待确认码）的确认入口。"""
    from app.web.confirm import verify_and_consume

    await _require_admin_post(request, csrf)
    if not verify_and_consume(case_id, code):
        return await case_detail(request, case_id, notice="确认码错误或已过期")
    try:
        await transition_with_session(case_id, "KICKED", await _operator(request))
        await transition_with_session(case_id, "CLOSED", await _operator(request))
        await record_admin_audit(
            await _operator(request), "case_confirm_manual_kick", "case", str(case_id)
        )
    except IllegalTransitionError as exc:
        return _page("操作未执行", _transition_error_html(case_id, exc))
    return RedirectResponse(
        f"/admin/cases/{case_id}?notice=已记录为人工踢出并结案", status_code=303
    )


@router.post("/cases/{case_id}/keep")
async def keep_case(request: Request, case_id: int, csrf: str = Form("")) -> Response:
    await _require_admin_post(request, csrf)
    try:
        await transition_with_session(case_id, "KEEP", await _operator(request))
        await transition_with_session(case_id, "CLOSED", await _operator(request))
        await record_admin_audit(await _operator(request), "case_keep", "case", str(case_id))
    except IllegalTransitionError as exc:
        return _page("操作未执行", _transition_error_html(case_id, exc))
    return RedirectResponse("/admin", status_code=303)


@router.post("/cases/{case_id}/false-positive")
async def false_positive(request: Request, case_id: int, csrf: str = Form("")) -> Response:
    await _require_admin_post(request, csrf)
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
        await record_admin_audit(
            await _operator(request),
            "case_false_positive",
            "case",
            str(case_id),
            {"case_no": case.case_no, "records": len(records)},
        )
    except IllegalTransitionError as exc:
        return _page("操作未执行", _transition_error_html(case_id, exc))
    return RedirectResponse("/admin", status_code=303)


@router.post("/cases/{case_id}/cancel")
async def cancel_case(request: Request, case_id: int, csrf: str = Form("")) -> Response:
    await _require_admin_post(request, csrf)
    try:
        await transition_with_session(case_id, "CANCELLED", await _operator(request))
        await transition_with_session(case_id, "CLOSED", await _operator(request))
        await record_admin_audit(await _operator(request), "case_cancel", "case", str(case_id))
    except IllegalTransitionError as exc:
        return _page("操作未执行", _transition_error_html(case_id, exc))
    return RedirectResponse("/admin", status_code=303)


@router.post("/cases/batch-delete")
async def batch_delete_cases(request: Request, csrf: str = Form(""), case_ids: list[str] = Form([])) -> Response:
    """勾选批量删除案件（含其违规证据记录）——负责人授权的清理功能，不可恢复。"""
    await _require_admin_post(request, csrf)
    operator = await _operator(request)
    ids = []
    for raw in case_ids:
        try:
            ids.append(int(raw))
        except ValueError:
            continue
    if not ids:
        return RedirectResponse("/admin?notice=" + quote("未勾选任何案件"), status_code=303)
    deleted_cases = 0
    deleted_records = 0
    async with SessionLocal() as session:
        for cid in ids:
            case = await session.get(Case, cid)
            if case is None:
                continue
            for vid in json.loads(case.violation_ids_json):
                record = await session.get(ViolationRecord, int(vid))
                if record is not None:
                    await session.delete(record)
                    deleted_records += 1
            await session.delete(case)
            deleted_cases += 1
        await session.commit()
    await record_admin_audit(
        operator,
        "case_batch_delete",
        "case",
        ",".join(str(i) for i in ids),
        {"cases": deleted_cases, "violation_records": deleted_records},
    )
    return RedirectResponse(
        "/admin?notice=" + quote(f"已删除 {deleted_cases} 个案件（含 {deleted_records} 条违规证据）"),
        status_code=303,
    )


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


def _beijing(naive_utc: Any) -> str:
    """数据库存 naive UTC，展示转为北京时间。"""
    return f"{naive_utc + timedelta(hours=8):%m-%d %H:%M:%S}"


def _feedback_form_html(
    record: Any, csrf: str, saved_label: str = "", saved_reason: str = ""
) -> str:
    """人工反馈表单；已保存时回显（下拉选中、原因带默认值、按钮为更新）。"""
    category = _esc(record.category or "other")
    has_saved = bool(saved_label)
    options = [
        ("confirmed_violation", "确认违规"),
        ("confirmed_normal", "确认正常"),
        ("false_positive", "误判"),
        ("unknown_recall", "未知原因撤回"),
        ("other_recall", "其他原因撤回"),
    ]
    sel = "".join(
        f"<option value={v}{' selected' if v == saved_label else ''}>{t}</option>"
        for v, t in options
    )
    return (
        '<form method=post action="/admin/feedback" style="display:grid;gap:4px">'
        f"{csrf}"
        f'<input type=hidden name=message_id value="{_esc(record.message_id)}">'
        f'<input type=hidden name=category value="{category}">'
        f"<select name=label>{sel}</select>"
        f'<input name=reason list=feedback-reasons value="{_esc(saved_reason)}" '
        f'data-default="{_esc(saved_reason)}" placeholder="原因，可选" style="width:140px">'
        + ("<span class=muted>已反馈✓</span>" if has_saved else "")
        + f"<button class=btn>{'更新反馈' if has_saved else '保存反馈'}</button></form>"
    )


@router.get("/shadow", response_class=HTMLResponse)
async def shadow_page(request: Request, verdict: str = "") -> Response:
    token = await _require_login(request)
    if not token:
        return _login_redirect()
    csrf = _csrf_field(token)
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
        # 已保存的人工反馈：用于回显（保存后下拉/原因保持，不再被刷新重置）
        saved_map: dict[str, tuple[str, str]] = {}
        if records:
            from app.moderation.feedback import FeedbackRecord

            fb_rows = await session.execute(
                select(FeedbackRecord).where(
                    FeedbackRecord.message_id.in_([r.message_id for r in records])
                )
            )
            for f in fb_rows.scalars():
                saved_map.setdefault(f.message_id, (f.label, f.reason or ""))

    counts: dict[str, int] = {}
    for r in records:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    summary = "、".join(f"{_zh_verdict(k)}={v}" for k, v in sorted(counts.items())) or "暂无"

    def _member_display(record: Any) -> str:
        name = _esc(getattr(record, "sender_name", "") or "")
        if name:
            return f"<b>{name}</b>"
        return f"<code>{_esc(record.member_openid[:16])}…</code>"

    def _group_display(openid: str) -> str:
        name = group_names.get(openid)
        return _esc(name) if name else f"<code>{_esc(openid[:10])}…</code>"

    def _feedback_form(record: Any) -> str:
        saved_label, saved_reason = saved_map.get(record.message_id, ("", ""))
        return _feedback_form_html(record, csrf, saved_label, saved_reason)

    rows = "".join(
        "<tr>"
        f'<td><a href="/admin/shadow/detail?message_id={quote(r.message_id)}">'
        f"{_esc(_beijing(r.created_at))}</a></td>"
        f"<td>{_group_display(r.group_openid)}</td>"
        f"<td>{_zh_kind(r.kind)}</td>"
        f"<td>{_zh_verdict(r.verdict)}</td>"
        f"<td>{r.confidence}</td>"
        f"<td>{_member_display(r)}</td>"
        f"<td>{_esc(r.reason[:80])}</td>"
        f"<td>{_feedback_form(r)}</td>"
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
        f"{csrf}"
        f'<input type=hidden name=group_openid value="{_esc(openid)}">'
        '<input name=name placeholder="群名称" style="width:160px">'
        "<button class=btn>保存</button></form></td></tr>"
        for openid in distinct_groups
    )

    body = (
        f"<h2>影子模式判定（最近100条）</h2><p>分布：{_esc(summary)}　"
        "<span class=muted>影子模式只记录不处罚；时间为北京时间</span></p>"
        f'<form style="display:none">{csrf}</form>'
        "<div class=card><b>判定说明：</b>高置信违规=确定违规（正式模式自动撤回+禁言+警告）；"
        "转人工复核=有疑点但证据不足（不处罚，人工确认）；放行=正常内容。"
        "<b>置信度</b>=系统对判定的把握程度（0~1），≥0.90才自动处罚。</div>"
        '<div class=card style="border-color:#b45309"><b>首次使用：</b>'
        "群名称尚未备注时，列表「群」列显示OpenID代码——请在<b>页面最底部「群名称备注」表格</b>"
        "把每个代码对应的群名填一次并保存，之后列表直接显示群名。</div>"
        "<table><tr><th>时间</th><th>群</th><th>类型</th><th>判定</th><th>置信度</th><th>成员（群昵称）</th><th>原因</th><th>人工反馈</th></tr>"
        f"{rows}</table>"
        # 人工反馈原因预设（datalist：可下拉选择，也可自由填写）
        "<datalist id=feedback-reasons>"
        "<option value=广告/引流>"
        "<option value=诈骗/钓鱼>"
        "<option value=色情/低俗>"
        "<option value=违禁品交易>"
        "<option value=刷屏/灌水>"
        "<option value=骚扰/辱骂>"
        "<option value=泄露他人隐私>"
        "<option value=白名单来源（校园墙等）>"
        "<option value=正常聊天>"
        "<option value=规则误判>"
        "<option value=媒体无法自动判定>"
        "</datalist>"
        '<div class=card style="margin-top:20px"><h3>群名称备注</h3>'
        "<p class=muted>官方接口只提供群加密OpenID。把下面各OpenID对应的群名填一次，"
        "之后列表将直接显示群名称。</p>"
        f"<table><tr><th>群OpenID</th><th>已备注</th><th>备注操作</th></tr>{group_forms}</table>"
        '<form method=post action="/admin/groups/alias/bulk" style="margin-top:12px">'
        f"{csrf}"
        '<label style="display:block"><b>批量导入</b>（每行一条，格式：群ID 群名，空格分隔；'
        "适合接入 100+ 群时一次性维护）</label>"
        '<textarea name=bulk rows=5 style="width:100%;font-family:monospace" '
        'placeholder="17598122 靠谱日结兼职群&#10;470794920 大学生兼职群"></textarea>'
        '<button class=btn style="margin-top:6px">批量保存</button></form></div>'
    )
    return _page("影子判定", body, refresh_seconds=30)


@router.get("/shadow/detail", response_class=HTMLResponse)
async def shadow_detail(request: Request, message_id: str = "") -> Response:
    """单条影子判定详情：原文预览、消息段、媒体原件回看、AI取证。"""
    token = await _require_login(request)
    if not token:
        return _login_redirect()
    csrf = _csrf_field(token)
    from app.models import GroupAlias
    from app.runtime.models import ShadowDecision

    back = '<p><a href="/admin/shadow">← 返回影子判定列表</a></p>'
    async with SessionLocal() as session:
        record = (
            (
                await session.execute(
                    select(ShadowDecision).where(ShadowDecision.message_id == message_id)
                )
            )
            .scalars()
            .first()
        )
        if record is None:
            return _page(
                "影子判定详情",
                back + "<div class=card><b>未找到该消息的影子记录。</b>"
                "可能产生于本功能上线前，或已被保留期清理。</div>",
            )
        alias = await session.get(GroupAlias, record.group_openid)
        from app.moderation.feedback import FeedbackRecord

        fb = (
            (
                await session.execute(
                    select(FeedbackRecord).where(FeedbackRecord.message_id == message_id)
                )
            )
            .scalars()
            .first()
        )

    try:
        detail = json.loads(record.detail_json or "{}")
    except json.JSONDecodeError:
        detail = {}

    group_name = alias.name if alias else ""
    member = _esc(
        getattr(record, "sender_name", "")
        or record.external_user_id
        or record.member_openid
        or "（未知）"
    )

    # 媒体原件回看（图片直接展示；音视频播放；文件下载）
    media_parts: list[str] = []
    for m in detail.get("media_files") or []:
        name = str(m.get("name") or "")
        mtype = str(m.get("type") or "")
        if not name:
            continue
        url = "/admin/media/" + quote(name)
        if mtype.startswith("image/"):
            media_parts.append(
                f'<div><img src="{url}" alt="消息图片" style="max-width:480px;'
                'max-height:480px;border:1px solid #ddd"></div>'
            )
        elif mtype.startswith("video/"):
            media_parts.append(
                f'<div><video controls src="{url}" style="max-width:480px"></video></div>'
            )
        elif mtype == "voice":
            media_parts.append(f'<div><audio controls src="{url}"></audio></div>')
        else:
            media_parts.append(
                f'<div><a class=btn href="{url}">下载文件（{_esc(name[:24])}）</a></div>'
            )
    if not media_parts:
        media_parts.append(
            '<p class=muted>本条无媒体，或该记录早于"媒体文件名存档"功能上线'
            "；原件也可能已按配置保留期清理，详情以当前记录为准。</p>"
        )

    seg_items = (
        "".join(
            f"<li>类型={_esc(str(s.get('kind')))}　内容={_esc(str(s.get('text'))[:120])}</li>"
            for s in detail.get("segments") or []
        )
        or "<li class=muted>无段摘要（早于该功能）</li>"
    )

    ai_items = (
        "".join(
            f"<li>模型={_esc(str(h.get('rule_name')))}　类别={_esc(str(h.get('category')))}　"
            f"取证（脱敏）={_esc(str(h.get('evidence_masked'))[:200])}</li>"
            for h in detail.get("rule_hits") or []
            if str(h.get("rule_id", "")).startswith("AI_")
        )
        or "<li class=muted>本条无AI辅助记录</li>"
    )

    fb_line = (
        f"<p>已保存反馈：{_esc(fb.label)}　原因：{_esc(fb.reason or '无')}</p>"
        if fb is not None
        else "<p class=muted>尚未保存反馈</p>"
    )
    text_preview = _esc(str(detail.get("text_preview") or ""))

    body = (
        f'<p><a href="/admin/shadow">← 返回影子判定列表</a></p><h2>判定详情</h2>'
        "<div class=card><table>"
        f"<tr><th>时间</th><td>{_esc(_beijing(record.created_at))}（北京时间）</td></tr>"
        f"<tr><th>群</th><td>{_esc(group_name or '（未备注）')}　"
        f"<code>{_esc(record.group_openid[:20])}…</code></td></tr>"
        f"<tr><th>成员</th><td>{member}</td></tr>"
        f"<tr><th>类型</th><td>{_zh_kind(record.kind)}</td></tr>"
        f"<tr><th>判定</th><td><b>{_zh_verdict(record.verdict)}</b>　置信度 {record.confidence}</td></tr>"
        f"<tr><th>判定原因</th><td>{_esc(record.reason)}</td></tr>"
        f"</table>{fb_line}</div>"
        "<div class=card><h3>文字内容</h3>"
        f"<p>{text_preview or '<span class=muted>（无文字）</span>'}</p>"
        "<p class=muted>隐私设计：文字仅保留前60字预览，完整原文不留存。</p></div>"
        f"<div class=card><h3>消息段</h3><ul>{seg_items}</ul></div>"
        "<div class=card><h3>媒体内容（原件保留30天）</h3>"
        f"{''.join(media_parts)}</div>"
        f"<div class=card><h3>AI 取证（脱敏，保留180天）</h3><ul>{ai_items}</ul></div>"
        "<div class=card><h3>人工反馈</h3>"
        + _feedback_form_html(
            record,
            csrf,
            fb.label if fb is not None else "",
            fb.reason if fb is not None else "",
        )
        + "</div>"
    )
    return _page("影子判定详情", body)


@router.get("/media/{name}")
async def media_file(name: str, request: Request) -> FileResponse:
    """受保护的媒体原件查看（仅登录管理员；防路径穿越；仅限 data/media/ 内）。"""
    token = await _require_login(request)
    if not token:
        raise HTTPException(status_code=401, detail="login required")
    from pathlib import Path

    from app.runtime.pipeline import MEDIA_DIR

    safe = Path(name).name
    if safe != name or not name:
        raise HTTPException(status_code=404, detail="not found")
    path = (MEDIA_DIR / safe).resolve()
    if MEDIA_DIR.resolve() not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path)


@router.post("/groups/alias")
async def save_group_alias(
    request: Request,
    group_openid: str = Form(""),
    name: str = Form(""),
    csrf: str = Form(""),
) -> Response:
    await _require_admin_post(request, csrf)
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
    await record_admin_audit(
        await _operator(request), "group_alias_save", "group", group_openid, {"name": name[:64]}
    )
    return RedirectResponse("/admin/shadow", status_code=303)


@router.post("/groups/alias/bulk")
async def bulk_group_alias(
    request: Request,
    bulk: str = Form(""),
    csrf: str = Form(""),
) -> Response:
    """批量导入群备注：每行「群ID 群名」（空白分隔）。适配 100+ 群接入场景。"""
    await _require_admin_post(request, csrf)
    added, skipped = 0, 0
    async with SessionLocal() as session:
        from app.models import GroupAlias

        for line in bulk.splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
                skipped += 1
                continue
            gid, group_name = parts[0].strip(), parts[1].strip()[:64]
            existing = await session.get(GroupAlias, gid)
            if existing is None:
                existing = GroupAlias(group_openid=gid)
                session.add(existing)
            existing.name = group_name
            added += 1
        await session.commit()
    await record_admin_audit(
        await _operator(request),
        "group_alias_bulk",
        "group",
        f"n={added}",
        {"skipped": skipped},
    )
    return RedirectResponse(
        "/admin/shadow?notice=" + quote(f"批量导入完成：成功 {added} 条，跳过 {skipped} 行"),
        status_code=303,
    )


# ---------- 规则视图 ----------


@router.get("/rules", response_class=HTMLResponse)
async def rules_page(request: Request, notice: str = "") -> Response:
    token = await _require_login(request)
    if not token:
        return _login_redirect()
    csrf = _csrf_field(token)
    from collections import defaultdict

    from app.moderation.dynamic_rules import (
        RuleItem,
        RuleSet,
        RuleVersion,
        ensure_default_rules,
    )
    from app.moderation.rules import BLACKLIST_EXPLICIT, SOFT_SIGNALS

    async with SessionLocal() as session:
        await ensure_default_rules(session)
        version_rows = (
            await session.execute(
                select(RuleVersion, RuleSet)
                .join(RuleSet, RuleSet.id == RuleVersion.rule_set_id)
                .order_by(RuleVersion.created_at.desc(), RuleVersion.id.desc())
            )
        ).all()
        version_ids = [v.id for v, _ in version_rows]
        items_by_version: dict[int, list[RuleItem]] = defaultdict(list)
        if version_ids:
            items = (
                (
                    await session.execute(
                        select(RuleItem)
                        .where(RuleItem.version_id.in_(version_ids))
                        .order_by(RuleItem.id.asc())
                    )
                )
                .scalars()
                .all()
            )
            for item in items:
                items_by_version[item.version_id].append(item)

    notice_html = f"<p class=warn>{_esc(notice)}</p>" if notice else ""
    blacklist = "、".join(_esc(w) for w in BLACKLIST_EXPLICIT)
    soft = "、".join(_esc(w) for w in SOFT_SIGNALS)
    version_parts: list[str] = []
    for version, rule_set in version_rows:
        status = _esc(version.status)
        item_rows = (
            "".join(
                "<li>"
                f"#{item.id} [{_esc(item.item_type)} / {_esc(item.category)} / {item.weight:.2f}] "
                f"{_esc(item.pattern)}"
                + (f" — {_esc(item.description)}" if item.description else "")
                + (" <span class=muted>已停用</span>" if not item.enabled else "")
                + "</li>"
                for item in items_by_version.get(version.id, [])
            )
            or "<li class=muted>暂无规则项</li>"
        )

        publish_form = (
            f'<form method=post action="/admin/rules/versions/{version.id}/publish" '
            f'style="display:inline">{csrf}<button class="btn ok">发布此草稿</button></form>'
            if version.status == "DRAFT"
            else ""
        )
        rollback_form = (
            f'<form method=post action="/admin/rules/versions/{version.id}/rollback" '
            f'style="display:inline">{csrf}<button class=btn>回滚到此版本</button></form>'
            if version.status == "ARCHIVED"
            else ""
        )
        add_item_form = (
            f'<form method=post action="/admin/rules/drafts/{version.id}/items" '
            'style="margin-top:10px;display:grid;grid-template-columns:120px 1fr 120px 90px 1fr auto;gap:8px">'
            f"{csrf}"
            "<select name=item_type>"
            "<option value=keyword>关键词</option>"
            "<option value=phrase_combo>组合短语</option>"
            "<option value=domain>域名</option>"
            "<option value=share_source>分享来源</option>"
            "<option value=contact_combo>联系方式组合</option>"
            "</select>"
            '<input name=pattern placeholder="规则内容，例如 招聘+私聊 或 example.com">'
            "<select name=category>"
            "<option value=ad>广告</option>"
            "<option value=fraud>诈骗</option>"
            "<option value=porn>色情</option>"
            "<option value=violence>暴力/违禁</option>"
            "<option value=flood>刷屏</option>"
            "<option value=other>其他</option>"
            "<option value=allow>允许/白名单</option>"
            "</select>"
            "<input name=weight value=0.95>"
            '<input name=description placeholder="备注，可选">'
            "<button class=btn>添加规则项</button></form>"
            if version.status == "DRAFT"
            else ""
        )
        actions = publish_form + rollback_form or "<span class=muted>当前生效版本</span>"
        version_parts.append(
            "<div class=card>"
            f"<h3>版本 #{version.id} — {status}</h3>"
            f"<p>范围：<code>{_esc(rule_set.scope)}</code> / <code>{_esc(rule_set.scope_key)}</code>　"
            f"版本号：{version.version}　说明：{_esc(version.description)}</p>"
            f"<p>{actions}</p><ul>{item_rows}</ul>{add_item_form}</div>"
        )

    version_html = "".join(version_parts) or "<div class=card>暂无动态规则版本</div>"
    body = (
        f"{notice_html}"
        "<div class=card><h2>动态规则配置</h2>"
        "<p class=muted>规则保存在数据库中；发布后运行时会自动读取最新 Active 版本，不需要改代码或重启。"
        "这里只支持安全的结构化文本规则，不允许代码、SQL 或任意正则。</p>"
        '<form method=post action="/admin/rules/drafts" '
        'style="display:grid;grid-template-columns:120px 180px 1fr auto;gap:8px">'
        f"{csrf}"
        "<select name=scope><option value=global>全局规则</option><option value=group>单群规则</option></select>"
        '<input name=scope_key placeholder="单群填 group_openid；全局可留空">'
        '<input name=name placeholder="草稿名称，例如 9月广告规则调整">'
        "<button class=btn>创建草稿</button></form></div>"
        f"{version_html}"
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


@router.post("/rules/drafts")
async def create_rule_draft_submit(
    request: Request,
    scope: str = Form("global"),
    scope_key: str = Form(""),
    name: str = Form(""),
    csrf: str = Form(""),
) -> Response:
    await _require_admin_post(request, csrf)
    operator = await _operator(request)
    from app.moderation.dynamic_rules import RuleScope, create_rule_draft

    scope_clean: RuleScope = "group" if scope == "group" else "global"
    scope_key_clean = scope_key.strip()
    if scope_clean == "global":
        scope_key_clean = "*"
    if scope_clean == "group" and not scope_key_clean:
        return _rules_notice_redirect("单群规则必须填写group_openid")
    try:
        async with SessionLocal() as session:
            draft = await create_rule_draft(
                session,
                scope=scope_clean,
                scope_key=scope_key_clean,
                name=name.strip() or "未命名规则草稿",
                operator=operator,
            )
        await record_admin_audit(
            operator,
            "rule_create_draft",
            "rule_version",
            str(draft.id),
            {"scope": scope_clean, "scope_key": scope_key_clean},
        )
    except ValueError as exc:
        return _rules_notice_redirect(str(exc))
    return _rules_notice_redirect(f"已创建草稿#{draft.id}")


@router.post("/rules/drafts/{version_id}/items")
async def add_rule_item_submit(
    request: Request,
    version_id: int,
    item_type: str = Form("keyword"),
    pattern: str = Form(""),
    category: str = Form("ad"),
    weight: float = Form(0.95),
    description: str = Form(""),
    csrf: str = Form(""),
) -> Response:
    await _require_admin_post(request, csrf)
    operator = await _operator(request)
    from app.moderation.dynamic_rules import add_rule_item

    try:
        async with SessionLocal() as session:
            item = await add_rule_item(
                session,
                version_id,
                item_type=item_type,
                pattern=pattern,
                category=category,
                weight=weight,
                description=description,
                operator=operator,
            )
        await record_admin_audit(
            operator,
            "rule_add_item",
            "rule_item",
            str(item.id),
            {"version_id": version_id, "item_type": item_type, "category": category},
        )
    except ValueError as exc:
        return _rules_notice_redirect(str(exc))
    return _rules_notice_redirect(f"已添加规则项#{item.id}")


async def _rule_plan_state(session: AsyncSession, version_id: int) -> dict[str, Any]:
    from app.moderation.dynamic_rules import RuleItem, RuleSet, RuleVersion

    version = await session.get(RuleVersion, version_id, populate_existing=True)
    if version is None:
        raise HTTPException(404, "规则版本不存在")
    rule_set = await session.get(RuleSet, version.rule_set_id)
    items = (
        await session.scalars(
            select(RuleItem).where(RuleItem.version_id == version_id).order_by(RuleItem.id)
        )
    ).all()
    active = (
        await session.scalars(
            select(RuleVersion.id)
            .where(RuleVersion.rule_set_id == version.rule_set_id, RuleVersion.status == "ACTIVE")
            .order_by(RuleVersion.id)
        )
    ).all()
    return {
        "version_id": version_id,
        "version": version.version,
        "status": version.status,
        "scope": rule_set.scope if rule_set else "",
        "scope_key": rule_set.scope_key if rule_set else "",
        "active_versions": list(active),
        "items": [
            {
                column.name: getattr(item, column.name)
                for column in RuleItem.__table__.columns
                if column.name != "created_at"
            }
            for item in items
        ],
    }


async def _preview_rule_change(
    request: Request, version_id: int, csrf: str, action: str
) -> Response:
    token = await _require_admin_post(request, csrf)
    from app.web.agent_confirm import create_confirmation

    async with SessionLocal() as session:
        state = await _rule_plan_state(session, version_id)
        if action == "rule_publish" and state["status"] != "DRAFT":
            raise HTTPException(409, "只能预览发布草稿规则")
        if action == "rule_rollback" and state["status"] not in ("ACTIVE", "ARCHIVED"):
            raise HTTPException(409, "只能回滚到已发布的历史规则")
        plan = await create_confirmation(
            session,
            action=action,
            params={"version_id": version_id},
            expected_state=state,
            requestor=_human_actor(token),
        )
    return RedirectResponse(f"/admin/plans/{plan.id}", status_code=303)


@router.post("/rules/versions/{version_id}/publish")
async def publish_rule_version_submit(
    request: Request, version_id: int, csrf: str = Form("")
) -> Response:
    return await _preview_rule_change(request, version_id, csrf, "rule_publish")


@router.post("/rules/versions/{version_id}/rollback")
async def rollback_rule_version_submit(
    request: Request, version_id: int, csrf: str = Form("")
) -> Response:
    return await _preview_rule_change(request, version_id, csrf, "rule_rollback")


# ---------- 反馈学习 ----------


@router.get("/feedback", response_class=HTMLResponse)
async def feedback_page(request: Request, notice: str = "") -> Response:
    token = await _require_login(request)
    if not token:
        return _login_redirect()
    csrf = _csrf_field(token)
    from app.moderation.feedback import FeedbackRecord, RuleCandidate

    async with SessionLocal() as session:
        feedback_rows = (
            (
                await session.execute(
                    select(FeedbackRecord)
                    .order_by(FeedbackRecord.created_at.desc(), FeedbackRecord.id.desc())
                    .limit(80)
                )
            )
            .scalars()
            .all()
        )
        candidates = (
            (
                await session.execute(
                    select(RuleCandidate)
                    .order_by(RuleCandidate.created_at.desc(), RuleCandidate.id.desc())
                    .limit(80)
                )
            )
            .scalars()
            .all()
        )

    feedback_html = (
        "".join(
            "<tr>"
            f"<td>{fb.id}</td><td><code>{_esc(fb.message_id)}</code></td>"
            f"<td>{_esc(fb.label)}</td><td>{_esc(fb.category)}</td>"
            f"<td><code>{_esc(fb.group_openid[:16])}</code></td>"
            f"<td>{_esc(fb.reason)}</td><td>{_esc(fb.sample_text_masked[:120])}</td>"
            "</tr>"
            for fb in feedback_rows
        )
        or "<tr><td colspan=7 class=muted>暂无人工反馈</td></tr>"
    )
    candidate_html = (
        "".join(
            "<tr>"
            f"<td>{c.id}</td><td>{_esc(c.status)}</td><td>{_esc(c.scope)}/{_esc(c.scope_key[:16])}</td>"
            f"<td>{_esc(c.item_type)}</td><td>{_esc(c.pattern)}</td><td>{_esc(c.category)}</td>"
            f"<td>{c.support_count}/{c.member_count}</td><td>{c.conflict_count}</td>"
            "<td>"
            + (
                f'<form method=post action="/admin/feedback/candidates/{c.id}/copy-to-draft">'
                f"{csrf}<button class=btn>复制为草稿</button></form>"
                if c.status == "PROPOSED"
                else f"<span class=muted>草稿#{_esc(c.copied_version_id or '')}</span>"
            )
            + "</td></tr>"
            for c in candidates
        )
        or "<tr><td colspan=9 class=muted>暂无候选规则</td></tr>"
    )
    notice_html = f"<p class=warn>{_esc(notice)}</p>" if notice else ""
    body = (
        f"{notice_html}"
        "<div class=card><h2>反馈学习</h2>"
        "<p class=muted>这里只把管理员明确标注作为真值；未知原因撤回只保存，不参与候选规则挖掘。"
        "候选规则复制后仍是草稿，必须在规则页人工发布才会生效。</p>"
        f'<form method=post action="/admin/feedback/mine">{csrf}'
        "<button class=btn>从确认反馈挖掘候选规则</button></form></div>"
        "<div class=card><h3>候选规则</h3>"
        "<table><tr><th>ID</th><th>状态</th><th>范围</th><th>类型</th><th>内容</th><th>类别</th><th>支持/成员</th><th>负例冲突</th><th>操作</th></tr>"
        f"{candidate_html}</table></div>"
        "<div class=card><h3>最近反馈</h3>"
        "<table><tr><th>ID</th><th>消息</th><th>标签</th><th>类别</th><th>群</th><th>原因</th><th>脱敏样本</th></tr>"
        f"{feedback_html}</table></div>"
    )
    return _page("反馈学习", body)


@router.post("/feedback")
async def record_feedback_submit(
    request: Request,
    message_id: str = Form(""),
    label: str = Form(""),
    category: str = Form("other"),
    reason: str = Form(""),
    csrf: str = Form(""),
) -> Response:
    await _require_admin_post(request, csrf)
    operator = await _operator(request)
    from app.moderation.feedback import record_feedback

    try:
        async with SessionLocal() as session:
            feedback = await record_feedback(
                session,
                message_id,
                label,
                category,
                operator,
                reason,
            )
        await record_admin_audit(
            operator,
            "feedback_record",
            "message",
            message_id,
            {"label": label, "feedback_id": feedback.id},
        )
    except ValueError as exc:
        return _feedback_notice_redirect(str(exc))
    return _feedback_notice_redirect(f"已保存反馈#{feedback.id}")


@router.post("/feedback/mine")
async def mine_feedback_submit(request: Request, csrf: str = Form("")) -> Response:
    await _require_admin_post(request, csrf)
    operator = await _operator(request)
    from app.moderation.feedback import mine_rule_candidates

    async with SessionLocal() as session:
        # 负责人确认：确认即真值，门槛降至1条即学（不再需要≥3条×≥2人）
        candidates = await mine_rule_candidates(session, min_messages=3, min_members=2)
    await record_admin_audit(
        operator, "feedback_mine_candidates", "rule_candidate", "batch", {"count": len(candidates)}
    )
    return _feedback_notice_redirect(f"已生成{len(candidates)}条候选规则")


@router.post("/feedback/candidates/{candidate_id}/copy-to-draft")
async def copy_candidate_to_draft_submit(
    request: Request, candidate_id: int, csrf: str = Form("")
) -> Response:
    await _require_admin_post(request, csrf)
    operator = await _operator(request)
    from app.moderation.feedback import copy_candidate_to_draft

    try:
        async with SessionLocal() as session:
            draft_id = await copy_candidate_to_draft(session, candidate_id, operator=operator)
        await record_admin_audit(
            operator,
            "feedback_candidate_copy_to_draft",
            "rule_candidate",
            str(candidate_id),
            {"draft_id": draft_id},
        )
    except ValueError as exc:
        return _feedback_notice_redirect(str(exc))
    return _rules_notice_redirect(
        f"候选规则#{candidate_id}已复制为草稿#{draft_id}，请预览后人工发布"
    )


# ---------- 报告 ----------


@router.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request, notice: str = "") -> Response:
    token = await _require_login(request)
    if not token:
        return _login_redirect()
    csrf = _csrf_field(token)
    async with SessionLocal() as session:
        daily = await build_daily(session)
        weekly = await build_weekly(session)
        pending = await pending_manual_review(session)
    notice_html = f'<p class=warn role=status>{_esc(notice)}</p>' if notice else ""
    body = (
        notice_html
        + "<div class=card><h3>昨日日报</h3><pre>"
        + _esc(json.dumps(daily, ensure_ascii=False, indent=1))
        + "</pre></div>"
        "<div class=card><h3>近7天周报</h3><pre>"
        + _esc(json.dumps(weekly, ensure_ascii=False, indent=1))
        + "</pre></div>"
        f"<div class=card><h3>待人工处理清单（{len(pending)}）</h3><ul>"
        + "".join(f"<li>{_esc(p['case_no'])} — {_esc(p['member_openid'])}</li>" for p in pending)
        + "</ul></div>"
        "<div class=card><h3>数据保留清理</h3>"
        f'<form method=post action="/admin/cleanup">{csrf}<button class=btn>立即执行保留期清理</button></form></div>'
    )
    return _page("报告", body)


@router.post("/cleanup")
async def cleanup_now(request: Request, csrf: str = Form("")) -> RedirectResponse:
    await _require_admin_post(request, csrf)
    async with SessionLocal() as session:
        stats = await purge_expired(session)
    await record_admin_audit(
        await _operator(request), "cleanup_now", "retention", "manual", stats
    )
    summary = "、".join(f"{k}={v}" for k, v in stats.items() if v)
    notice = quote(f"清理完成：{summary or '无过期数据需要清理（各保留期内）'}")
    return RedirectResponse(f"/admin/reports?notice={notice}", status_code=303)


# ---------- 群管理（P0：按群审核/动作开关） ----------


async def _ensure_action_routes(
    session: AsyncSession, group_openid: str, provider: str = "", *, commit: bool = True
) -> list[str]:
    """动作开启时按该群已见消息来源补齐同通道路由（T-307/P1-8）。

    P1-8修复：同群只能有一个动作出口。仅当该群只有一个消息来源 provider
    时自动补齐路由；有多个候选 provider 时拒绝自动路由，要求管理员显式选择。
    """
    from app.core.group_settings import resolve_group_identity
    from app.core.routing import upsert_group_route

    provider = await resolve_group_identity(session, group_openid, provider)
    await upsert_group_route(
        session,
        group_openid,
        message_provider=provider,
        action_provider=provider,
        commit=commit,
    )
    return [provider]


@router.get("/groups", response_class=HTMLResponse)
async def groups_page(request: Request, notice: str = "", show_hidden: str = "") -> Response:
    """Provider-qualified controls with an explicit, human-approved action owner."""
    token = await _require_login(request)
    if not token:
        return _login_redirect()
    csrf = _csrf_field(token)
    from app.core.emergency_stop import emergency_stop_active
    from app.models import GroupActionOwner, HiddenGroup, ProviderGroupSettings
    from app.runtime.models import ShadowDecision

    async with SessionLocal() as session:
        seen = {
            tuple(row)
            for row in (
                await session.execute(
                    select(ShadowDecision.provider, ShadowDecision.external_group_id).distinct()
                )
            ).all()
        }
        settings_rows = (await session.scalars(select(ProviderGroupSettings))).all()
        settings_map = {(row.provider, row.external_group_id): row for row in settings_rows}
        from app.models import GroupAlias

        # 群备注与「群名称备注」统一为同一份数据（影子判定页显示的就是它）
        alias_map = {
            row.group_openid: row.name for row in (await session.scalars(select(GroupAlias))).all()
        }
        seen.update(settings_map)
        owners = {
            row.external_group_id: row.provider
            for row in (await session.scalars(select(GroupActionOwner))).all()
        }
        hidden = set(
            (row.provider, row.external_group_id)
            for row in (await session.scalars(select(HiddenGroup))).all()
        )
        stopped = await emergency_stop_active(session)
    rows = []
    for provider, group in sorted(seen):
        if not group:
            continue
        key = (provider, group)
        is_hidden = key in hidden
        # 正常视图隐藏已隐藏群；show_hidden=1 只显示已隐藏群（可恢复/彻底删除）
        if (not show_hidden and is_hidden) or (show_hidden and not is_hidden):
            continue
        gs = settings_map.get(key)
        if show_hidden:
            action_buttons = (
                f'<form method=post style="display:inline" action="/admin/groups/unhide">{csrf}'
                f'<input type=hidden name=provider value="{_esc(provider)}">'
                f'<input type=hidden name=group_openid value="{_esc(group)}">'
                '<button class=btn>取消隐藏</button></form> '
                f'<form method=post style="display:inline" action="/admin/groups/delete" '
                f'onsubmit="return confirm(\'彻底删除该群的后台数据？此操作不可恢复。\')">{csrf}'
                f'<input type=hidden name=provider value="{_esc(provider)}">'
                f'<input type=hidden name=group_openid value="{_esc(group)}">'
                '<button class="btn danger">彻底删除</button></form>'
            )
        else:
            action_buttons = (
                f'<form method=post style="display:inline" action="/admin/groups/hide" '
                f'onsubmit="return confirm(\'从后台隐藏该群？（数据保留，可随时在「查看已隐藏群」中恢复）\')">{csrf}'
                f'<input type=hidden name=provider value="{_esc(provider)}">'
                f'<input type=hidden name=group_openid value="{_esc(group)}">'
                '<button class=btn>隐藏</button></form> '
                f'<form method=post style="display:inline" action="/admin/groups/delete" '
                f'onsubmit="return confirm(\'彻底删除该群的后台数据？此操作不可恢复。\')">{csrf}'
                f'<input type=hidden name=provider value="{_esc(provider)}">'
                f'<input type=hidden name=group_openid value="{_esc(group)}">'
                '<button class="btn danger">删除</button></form>'
            )
        display_name = alias_map.get(group) or (gs.name if gs else "")
        rows.append(
            f"<tr><td>{_esc(provider)}<br>{f'<b>{_esc(display_name)}</b><br>' if display_name else ''}"
            f"<code>{_esc(group)}</code>{' <span class=warn>（已隐藏）</span>' if is_hidden else ''}</td>"
            f"<td>{_esc(owners.get(group) or '未选择 / 有歧义')}</td><td>"
            f'<form method=post action="/admin/groups/settings">{csrf}'
            f'<input type=hidden name=provider value="{_esc(provider)}">'
            f'<input type=hidden name=group_openid value="{_esc(group)}">'
            f'<label>群备注 <input name=name maxlength=64 value="'
            f'{_esc(alias_map.get(group) or (gs.name if gs else ""))}"></label> '
            f"<label><input type=checkbox name=moderation_enabled value=1 {'checked' if gs is None or gs.moderation_enabled else ''}>审核</label> "
            f"<label><input type=checkbox name=action_enabled value=1 {'checked' if gs and gs.action_enabled else ''}>真实动作</label> "
            '<button class=btn>保存 / 预览高风险变更</button></form>'
            f"<div style=margin-top:6px>{action_buttons}</div></td></tr>"
        )
    hidden_toggle = (
        '<a class=btn href="/admin/groups">返回正常视图</a>'
        if show_hidden
        else '<a class=btn href="/admin/groups?show_hidden=1">查看已隐藏群</a>'
    )
    body = (
        "<h2>群管理</h2><p>群设置按来源和群 ID 隔离。开启真实动作或改变出口需预览并由登录管理员确认；"
        "选择出口会关闭同 ID 其他来源的动作。不同通道 ID 不做猜测映射。</p>"
        f"<div role=status>{_esc(notice)}</div>"
        f"<p>{hidden_toggle}</p>"
        f"<div class=card><strong>急停：{'已开启，禁止外部动作' if stopped else '未开启'}</strong>"
        f'<form method=post action="/admin/emergency-stop">{csrf}'
        '<button class="btn danger">立即停止全部外部动作</button></form>'
        f'<form method=post action="/admin/emergency-resume">{csrf}'
        "<button class=btn>预览解除急停</button></form></div>"
        "<table><tr><th>群身份</th><th>唯一动作出口</th><th>设置</th></tr>"
        + (
            "".join(rows)
            or f"<tr><td colspan=3>{'没有已隐藏的群。' if show_hidden else '暂无群消息。可在下方明确添加群来源。'}</td></tr>"
        )
        + f'</table><div class=card><h3>添加明确的群身份</h3><form method=post action="/admin/groups/settings">{csrf}'
        "<label>来源 <select name=provider><option value=onebot>onebot</option>"
        "<option value=qq_official>qq_official</option></select></label> "
        "<label>群 ID <input name=group_openid required maxlength=128></label> "
        "<label>群备注 <input name=name maxlength=64></label> "
        "<input type=hidden name=moderation_enabled value=1>"
        "<button class=btn>添加（仅审核，动作关闭）</button></form></div>"
    )
    return _page("群管理", body)


@router.post("/groups/hide")
async def hide_group(
    request: Request,
    provider: str = Form(""),
    group_openid: str = Form(""),
    csrf: str = Form(""),
) -> Response:
    """软删除：从后台列表隐藏该群，数据全部保留，可恢复。"""
    from app.models import HiddenGroup

    await _require_admin_post(request, csrf)
    if not group_openid.strip():
        return RedirectResponse("/admin/groups?notice=" + quote("群 ID 为空"), status_code=303)
    async with SessionLocal() as session:
        key = (provider or "onebot", group_openid.strip())
        existing = await session.get(HiddenGroup, key)
        if existing is None:
            session.add(HiddenGroup(provider=key[0], external_group_id=key[1]))
            await session.commit()
    await record_admin_audit(
        await _operator(request), "group_hide", "group", f"{provider}:{group_openid}"
    )
    return RedirectResponse("/admin/groups?notice=" + quote("该群已隐藏（数据保留，可在「查看已隐藏群」中恢复）"), status_code=303)


@router.post("/groups/unhide")
async def unhide_group(
    request: Request,
    provider: str = Form(""),
    group_openid: str = Form(""),
    csrf: str = Form(""),
) -> Response:
    """恢复隐藏的群。"""
    from app.models import HiddenGroup

    await _require_admin_post(request, csrf)
    async with SessionLocal() as session:
        existing = await session.get(HiddenGroup, (provider or "onebot", group_openid.strip()))
        if existing is not None:
            await session.delete(existing)
            await session.commit()
    await record_admin_audit(
        await _operator(request), "group_unhide", "group", f"{provider}:{group_openid}"
    )
    return RedirectResponse(
        "/admin/groups?show_hidden=1&notice=" + quote("该群已恢复显示"), status_code=303
    )


@router.post("/groups/delete")
async def delete_group(
    request: Request,
    provider: str = Form(""),
    group_openid: str = Form(""),
    csrf: str = Form(""),
) -> Response:
    """彻底删除该群在后台的痕迹：设置、备注、动作出口、（可选）历史判定。

    不删除案件与违规记录（独立审计数据，可在案件列表单独清理）。
    """
    from app.models import GroupActionOwner, GroupAlias, HiddenGroup, ProviderGroupSettings
    from app.runtime.models import ShadowDecision

    await _require_admin_post(request, csrf)
    gid = group_openid.strip()
    if not gid:
        return RedirectResponse("/admin/groups?notice=" + quote("群 ID 为空"), status_code=303)
    async with SessionLocal() as session:
        gs = await session.get(ProviderGroupSettings, (provider or "onebot", gid))
        if gs is not None:
            await session.delete(gs)
        alias = await session.get(GroupAlias, gid)
        if alias is not None:
            await session.delete(alias)
        owner = await session.get(GroupActionOwner, gid)
        if owner is not None:
            await session.delete(owner)
        hidden = await session.get(HiddenGroup, (provider or "onebot", gid))
        if hidden is not None:
            await session.delete(hidden)
        result = await session.execute(
            text(
                "DELETE FROM shadow_decisions WHERE provider = :p AND external_group_id = :g"
            ),
            {"p": provider or "onebot", "g": gid},
        )
        await session.commit()
        deleted_decisions = result.rowcount or 0
    await record_admin_audit(
        await _operator(request),
        "group_delete",
        "group",
        f"{provider}:{gid}",
        {"shadow_decisions": deleted_decisions},
    )
    return RedirectResponse(
        "/admin/groups?notice="
        + quote(f"已彻底删除该群（含 {deleted_decisions} 条历史判定记录；案件不受影响）"),
        status_code=303,
    )


@router.post("/groups/settings")
async def save_group_settings(
    request: Request,
    group_openid: str = Form(""),
    provider: str = Form(""),
    name: str = Form(""),
    moderation_enabled: str = Form(""),
    action_enabled: str = Form(""),
    csrf: str = Form(""),
) -> Response:
    session_token = await _require_admin_post(request, csrf)
    actor = _human_actor(session_token)
    try:
        result = await _save_or_plan_group(
            group_openid,
            provider=provider,
            name=name,
            moderation_enabled=moderation_enabled == "1",
            action_enabled=action_enabled == "1",
            actor=actor,
        )
    except HTTPException as exc:
        return _page(
            "无法保存群设置",
            f'<p role=alert>{_esc(str(exc.detail))}</p><a href="/admin/groups">返回群管理</a>',
        )
    if result.get("confirmation_required"):
        return RedirectResponse(str(result["approval_url"]), status_code=303)
    # 群备注与「群名称备注」统一为同一份数据（GroupAlias），影子判定页据此显示群名
    if name.strip():
        async with SessionLocal() as session:
            from app.models import GroupAlias

            existing = await session.get(GroupAlias, group_openid)
            if existing is None:
                existing = GroupAlias(group_openid=group_openid)
                session.add(existing)
            existing.name = name.strip()[:64]
            await session.commit()
    return RedirectResponse("/admin/groups?notice=" + quote("设置已保存"), status_code=303)


# ---------- 系统设置（保留期/清理/备份） ----------


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, notice: str = "") -> Response:
    """系统设置：保留期、清理、备份、存储路径。"""
    token = await _require_login(request)
    if not token:
        return _login_redirect()
    csrf = _csrf_field(token)
    from app.config import get_settings

    settings = get_settings()
    async with SessionLocal() as session:
        from app.models import SystemSetting

        auto_cleanup = await session.get(SystemSetting, "auto_cleanup_enabled")
        last_cleanup = await session.get(SystemSetting, "last_cleanup_at")
        cleanup_status = await session.get(SystemSetting, "last_cleanup_status")
    auto_on = (auto_cleanup.value if auto_cleanup else "0") == "1"
    last_cleanup_text = last_cleanup.value if last_cleanup else "无执行记录"
    cleanup_status_text = cleanup_status.value if cleanup_status else "未验证任务计划"

    notice_html = (
        f'<div class=card style="border-color:#16804b">{_esc(notice)}</div>' if notice else ""
    )
    body = (
        "<h2>系统设置</h2>"
        f"{notice_html}"
        "<div class=card><h3>数据保留期（来自 .env，修改需重启）</h3>"
        f"<p>原始消息/媒体：<b>{settings.raw_retention_days}</b> 天　"
        f"判定/反馈/动作记录：<b>{settings.decision_retention_days}</b> 天</p></div>"
        "<div class=card><h3>存储路径</h3>"
        "<p>数据库位置由本机DATABASE_URL配置决定；备份存放在数据库同级backups目录。</p></div>"
        "<div class=card><h3>数据清理</h3>"
        f"<p>计划清理许可：{'已允许' if auto_on else '未允许'}。此开关不会创建Windows计划任务。</p>"
        "<p class=muted>需在本机配置每6小时执行 uv run python -m app.reports.maintenance cleanup，并核对最近结果；程序未内置定时线程。</p>"
        f"<p>最近尝试（UTC）：{_esc(last_cleanup_text)}；结果：{_esc(cleanup_status_text)}</p>"
        f'<form method=post action="/admin/settings/auto-cleanup" style="display:inline">{csrf}'
        f"<button class=btn>{'关闭自动清理' if auto_on else '开启自动清理'}</button></form>"
        f'<form method=post action="/admin/cleanup" style="display:inline;margin-left:12px">{csrf}'
        "<button class=btn>立即执行清理</button></form></div>"
        "<div class=card><h3>数据库备份</h3>"
        f'<form method=post action="/admin/settings/backup">{csrf}'
        "<button class=btn>创建一致性数据库备份</button></form>"
        "<p class=muted>包含已提交WAL记录；不包含媒体文件和本机凭据。备份含私密数据，请限制访问并另外保管异机副本。</p></div>"
    )
    return _page("系统设置", body)


@router.post("/settings/auto-cleanup")
async def toggle_auto_cleanup(request: Request, csrf: str = Form("")) -> RedirectResponse:
    await _require_admin_post(request, csrf)
    async with SessionLocal() as session:
        from app.models import SystemSetting

        s = await session.get(SystemSetting, "auto_cleanup_enabled")
        if s is None:
            from app.models import SystemSetting

            s = SystemSetting(key="auto_cleanup_enabled", value="0")
            session.add(s)
        s.value = "0" if s.value == "1" else "1"
        await session.commit()
    await record_admin_audit(
        await _operator(request),
        "auto_cleanup_toggle",
        "system",
        "cleanup",
        {"enabled": s.value == "1"},
    )
    return RedirectResponse(
        f"/admin/settings?notice=自动清理已{'开启' if s.value == '1' else '关闭'}", status_code=303
    )


@router.post("/settings/backup")
async def backup_db(request: Request, csrf: str = Form("")) -> RedirectResponse:
    await _require_admin_post(request, csrf)
    import asyncio

    from app.reports.backup import backup_sqlite

    try:
        dest = await asyncio.to_thread(backup_sqlite, get_settings().database_url)
    except Exception as exc:
        await record_admin_audit(
            await _operator(request),
            "db_backup_failed",
            "system",
            "backup",
            {"error_kind": type(exc).__name__},
        )
        raise HTTPException(503, "备份未完成，请检查数据库、磁盘和权限；不能用此结果交付") from exc
    msg = f"已完成一致性备份：{dest.name}（未包含媒体文件）"
    await record_admin_audit(
        await _operator(request),
        "db_backup",
        "system",
        "backup",
        {"filename": dest.name, "integrity_check": "ok", "includes_media": False},
    )
    return RedirectResponse(f"/admin/settings?notice={_esc(msg)}", status_code=303)


# ---------- AI 消费面板（扩展统计页） ----------


async def _build_ai_daily_stats(session: AsyncSession, days: int = 7) -> list[dict[str, Any]]:
    """UTC日窗口：实际外呼、缓存和本地拒绝分开，未定价不能显示免费。"""
    from datetime import UTC, datetime, timedelta

    from app.moderation.ai import AIUsageLog, summarize_ai_usage

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = today - timedelta(days=max(1, days) - 1)

    rows = (
        (
            await session.execute(
                select(AIUsageLog)
                .where(
                    AIUsageLog.created_at >= cutoff,
                    AIUsageLog.created_at < today + timedelta(days=1),
                )
                .order_by(AIUsageLog.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    per_day: dict[str, dict[str, Any]] = {}
    for r in rows:
        day = r.created_at.strftime("%Y-%m-%d")
        d = per_day.setdefault(day, {"calls": 0, "ok": 0, "latency": []})
        if r.source.startswith("cache") or r.error_kind == "ai_rate_or_budget_limited":
            continue
        d["calls"] += 1
        d["ok"] += 1 if r.ok else 0
        d["latency"].append(r.latency_ms or 0)
    result = []
    for day in sorted(per_day, reverse=True)[:days]:
        d = per_day[day]
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
        usage = await summarize_ai_usage(session, since=start, until=start + timedelta(days=1))
        avg = sum(d["latency"]) / len(d["latency"]) if d["latency"] else 0
        result.append(
            {
                "day": day,
                "calls": d["calls"],
                "ok": d["ok"],
                "fail": d["calls"] - d["ok"],
                "cost_yuan": None,
                "cost_known": False,
                **usage,
                "avg_latency_ms": round(avg, 0),
            }
        )
    return result


# ---------- 管理 REST API（AI Agent 对接） ----------

# P0-1: REST API 权限范围
_API_SCOPES = {
    "project:read",
    "settings:write",
    "rules:publish",
    "rules:rollback",
    "actions:enable",
    "routing:write",
    "emergency:stop",
}


def _require_api_token(request: Request, *, scope: str = "project:read") -> str | None:
    """REST API鉴权（P0-1修复）。

    - Cookie session: 必须通过 ``auth.is_valid`` 验证（修复伪造Cookie绕过）；
    - Bearer token: 与 ADMIN_PASSWORD 分离，使用独立 AGENT_API_TOKEN + 常量时间比较；
    - Cookie 发起 POST 写操作必须有 CSRF token；
    - 未授权返回 None（调用方返回 HTTP 401/403）。
    """
    settings = get_settings()
    if scope not in _API_SCOPES:
        raise HTTPException(403, "unknown permission scope")
    cookie_token = request.cookies.get(auth.SESSION_COOKIE)
    if cookie_token:
        # P0-1 修复：必须验证 session 有效性，不接受任意非空字符串
        if not auth.is_valid(cookie_token):
            return None
        # Cookie POST 写操作必须有 CSRF
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            submitted = request.headers.get("x-csrf-token") or ""
            if not submitted:
                # 也接受 form 字段
                try:
                    form = request.scope.get("_form")
                    if form is not None:
                        submitted = form.get("csrf", "")
                except Exception:  # noqa: BLE001
                    pass
            if not auth.validate_csrf(cookie_token, submitted):
                raise HTTPException(403, "CSRF token required for cookie-originated write")
        return _human_actor(cookie_token)
    # Bearer token: 独立 AGENT_API_TOKEN，常量时间比较
    auth_header = request.headers.get("authorization") or ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if settings.agent_api_read_token and secrets.compare_digest(
            token, settings.agent_api_read_token
        ):
            if scope != "project:read":
                raise HTTPException(403, "read-only credential cannot write")
            return "agent:read:" + sha256(token.encode()).hexdigest()[:16]
        if settings.agent_api_token and secrets.compare_digest(token, settings.agent_api_token):
            if scope not in {value.strip() for value in settings.agent_api_write_scopes.split(",")}:
                raise HTTPException(403, "credential lacks required scope")
            return "agent:write:" + sha256(token.encode()).hexdigest()[:16]
    return None


def _human_actor(session_token: str) -> str:
    return (
        "human:"
        + get_settings().admin_username[:20]
        + ":"
        + sha256(session_token.encode()).hexdigest()[:16]
    )


def _api_unauthorized_response() -> dict[str, object]:
    return {"error": "unauthorized"}


def _require_api_auth(request: Request, *, scope: str = "project:read") -> str:
    """鉴权失败直接 raise 401；成功返回 token 标识。"""
    token = _require_api_token(request, scope=scope)
    if token is None:
        raise HTTPException(401, "authentication required", headers={"WWW-Authenticate": "Bearer"})
    return token


@router.get("/api/status")
async def api_status(request: Request) -> dict[str, object]:
    """综合状态：进程、OneBot、判定统计（AI Agent 对接用）。"""
    _require_api_auth(request, scope="project:read")
    from app.runtime.onebot_ws import onebot_status

    async with SessionLocal() as session:
        from app.reports.stats import build_stats

        stats = await build_stats(session)
    return {
        "status": "ok",
        "onebot": onebot_status.snapshot(include_sensitive=False),
        "stats": stats["totals"],
        "generated_at": stats["generated_at"],
    }


@router.get("/api/groups")
async def api_groups(request: Request) -> list[dict[str, Any]]:
    _require_api_auth(request, scope="project:read")
    from app.models import ProviderGroupSettings

    async with SessionLocal() as session:
        rows = (await session.scalars(select(ProviderGroupSettings))).all()
        return [_group_response(row) for row in rows]


def _group_response(gs: Any) -> dict[str, Any]:
    return {
        "provider": gs.provider,
        "external_group_id": gs.external_group_id,
        "group_openid": gs.external_group_id,
        "name": gs.name,
        "moderation_enabled": gs.moderation_enabled,
        "action_enabled": gs.action_enabled,
        "version": gs.version,
    }


async def _group_state(session: AsyncSession, provider: str, group_id: str) -> dict[str, Any]:
    from app.core.routing import GroupProviderRoute
    from app.models import GroupActionOwner, ProviderGroupSettings

    gs = await session.get(ProviderGroupSettings, (provider, group_id), populate_existing=True)
    owner = await session.get(GroupActionOwner, group_id, populate_existing=True)
    routes = (
        await session.execute(
            select(
                GroupProviderRoute.message_provider,
                GroupProviderRoute.action_provider,
            )
            .where(GroupProviderRoute.external_group_id == group_id)
            .order_by(GroupProviderRoute.message_provider)
        )
    ).all()
    return {
        "settings": _group_response(gs) if gs else None,
        "owner": owner.provider if owner else None,
        "routes": [list(row) for row in routes],
    }


async def _apply_group_change(
    session: AsyncSession,
    params: dict[str, Any],
    *,
    actor: str,
    expected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from app.core.group_settings import get_or_create_group_settings
    from app.models import ProviderGroupSettings
    from app.web.agent_confirm import canonical

    provider, group = params["provider"], params["external_group_id"]
    before = await _group_state(session, provider, group)
    if expected is not None and canonical(before) != canonical(expected):
        raise HTTPException(409, "设置或出口已改变，请重新预览和批准")
    gs = await get_or_create_group_settings(session, group, provider=provider)
    version = gs.version
    values: dict[str, Any] = {"version": version + 1}
    for name in ("name", "moderation_enabled", "action_enabled"):
        if params[name] is not None:
            values[name] = params[name]
    changed = await session.execute(
        update(ProviderGroupSettings)
        .where(
            ProviderGroupSettings.provider == provider,
            ProviderGroupSettings.external_group_id == group,
            ProviderGroupSettings.version == version,
        )
        .values(**values)
    )
    if changed.rowcount != 1:  # type: ignore[attr-defined]
        raise HTTPException(409, "并发修改冲突，请重新预览")
    routed: list[str] = []
    if _needs_action_confirmation(before, params):
        routed = await _ensure_action_routes(session, group, provider, commit=False)
    # Legacy table is rollback-only. Disable its action bit even for a new write,
    # so reverting binaries never resurrects an old provider-ambiguous approval.
    from app.models import GroupSettings

    await session.execute(
        update(GroupSettings)
        .where(GroupSettings.group_openid == group)
        .values(action_enabled=False)
    )
    session.add(
        AdminAudit(
            operator=actor,
            action="group_settings_save",
            target_type="group",
            target_id=group,
            detail_json=canonical(
                {"params": params, "before": before, "auto_routed_providers": routed}
            ),
        )
    )
    await session.flush()
    await session.refresh(gs)
    return {**_group_response(gs), "auto_routed_providers": routed}


def _plan_response(plan: Any) -> dict[str, Any]:
    return {
        "confirmation_required": True,
        "confirmation_token": plan.id,
        "plan_id": plan.id,
        "summary": plan.action,
        "params": json.loads(plan.params_json),
        "expected_state": json.loads(plan.expected_state_json),
        "status": plan.status,
        "approval_url": f"/admin/plans/{plan.id}",
        "expires_at": plan.expires_at.isoformat(),
        "expires_in_seconds": 300,
        "message": "请由真人登录管理后台检查并批准；Agent 自己重发令牌不能批准。",
    }


def _needs_action_confirmation(before: dict[str, Any], params: dict[str, Any]) -> bool:
    settings = before.get("settings") or {"moderation_enabled": True, "action_enabled": False}
    action_after = (
        settings["action_enabled"] if params["action_enabled"] is None else params["action_enabled"]
    )
    moderation_after = (
        settings["moderation_enabled"]
        if params["moderation_enabled"] is None
        else params["moderation_enabled"]
    )
    was_active = settings["action_enabled"] and settings["moderation_enabled"]
    return params["action_enabled"] is True or (
        not was_active and action_after and moderation_after
    )


async def _save_or_plan_group(
    group_id: str,
    *,
    provider: str,
    name: str | None,
    moderation_enabled: bool | None,
    action_enabled: bool | None,
    actor: str,
    confirm_token: str = "",
    api_request: Request | None = None,
) -> dict[str, Any]:
    from app.core.group_settings import resolve_group_identity
    from app.web.agent_confirm import create_confirmation

    async with SessionLocal() as session:
        # Authorization and transition detection share the write transaction;
        # an enabled flag cannot change between the scope check and this write.
        await session.execute(text("BEGIN IMMEDIATE"))
        try:
            provider = await resolve_group_identity(session, group_id, provider)
        except ValueError as exc:
            raise HTTPException(422 if provider else 409, str(exc)) from exc
        params = {
            "provider": provider,
            "external_group_id": group_id,
            "name": name.strip()[:64] if name is not None else None,
            "moderation_enabled": moderation_enabled,
            "action_enabled": action_enabled,
        }
        before = await _group_state(session, provider, group_id)
        needs_confirmation = _needs_action_confirmation(before, params)
        if needs_confirmation and api_request is not None:
            _require_api_auth(api_request, scope="actions:enable")
            _require_api_auth(api_request, scope="routing:write")
        if confirm_token:
            return await _execute_plan(
                session, confirm_token, actor=actor, action="group_settings", params=params
            )
        if needs_confirmation:
            plan = await create_confirmation(
                session,
                action="group_settings",
                params=params,
                expected_state=before,
                requestor=actor,
            )
            return _plan_response(plan)
        result = await _apply_group_change(session, params, actor=actor)
        await session.commit()
        return result


@router.post("/api/groups/{group_openid}/settings", response_model=None)
async def api_group_settings(
    request: Request,
    group_openid: str,
    provider: str = "",
    moderation_enabled: bool | None = None,
    action_enabled: bool | None = None,
    name: str | None = None,
    confirm_token: str = "",
) -> dict[str, Any] | JSONResponse:
    actor = _require_api_auth(request, scope="settings:write")
    if action_enabled is True:
        _require_api_auth(request, scope="actions:enable")
        _require_api_auth(request, scope="routing:write")
    result = await _save_or_plan_group(
        group_openid,
        provider=provider,
        name=name,
        moderation_enabled=moderation_enabled,
        action_enabled=action_enabled,
        actor=actor,
        confirm_token=confirm_token,
        api_request=request,
    )
    return JSONResponse(result, status_code=202) if result.get("confirmation_required") else result


async def _execute_plan(
    session: AsyncSession,
    plan_id: str,
    *,
    actor: str,
    action: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from app.web.agent_confirm import canonical, claim_confirmation

    plan = await claim_confirmation(session, plan_id, requestor=actor, action=action, params=params)
    if plan is None:
        raise HTTPException(403, "计划未经真人批准、已过期、已消费或身份/参数不匹配")
    actual = json.loads(plan.params_json)
    expected = json.loads(plan.expected_state_json)
    try:
        # SQLite is the supported deployment store. Lock before comparing the
        # preview and applying it, so a concurrent draft edit/route switch cannot
        # slip between the human-approved snapshot and the mutation.
        await session.execute(text("BEGIN IMMEDIATE"))
        if plan.action == "group_settings":
            result = await _apply_group_change(session, actual, actor=actor, expected=expected)
        elif plan.action == "emergency_resume":
            from app.core.emergency_stop import emergency_stop_state, set_emergency_stop

            if await emergency_stop_state(session) != expected:
                raise HTTPException(409, "急停状态已改变，请重新预览")
            if get_settings().emergency_stop:
                raise HTTPException(409, "环境 EMERGENCY_STOP 仍开启，不能通过后台解除")
            await set_emergency_stop(session, False, actor=actor)
            result = {"emergency_stop": False}
        elif plan.action in ("rule_publish", "rule_rollback"):
            current = await _rule_plan_state(session, actual["version_id"])
            if canonical(current) != canonical(expected):
                raise HTTPException(409, "规则或生效版本已改变，请重新预览")
            from app.moderation.dynamic_rules import publish_rule_version, rollback_to_version

            operation = (
                publish_rule_version if plan.action == "rule_publish" else rollback_to_version
            )
            version = await operation(session, actual["version_id"], operator=actor)
            result = {"version_id": version.id, "status": version.status}
        else:
            raise HTTPException(422, "不支持的计划")
        plan.status = "EXECUTED"
        session.add(
            AdminAudit(
                operator=actor,
                action="plan_execute",
                target_type="admin_plan",
                target_id=plan.id,
                detail_json=canonical({"approved_by": plan.approved_by, "action": plan.action}),
            )
        )
        await session.commit()
        return result
    except Exception as exc:
        await session.rollback()
        plan = await session.get(type(plan), plan_id, populate_existing=True)
        if plan is not None:
            plan.status = "FAILED"
            await session.commit()
        if isinstance(exc, ValueError):
            raise HTTPException(409, str(exc)) from exc
        raise


@router.get("/plans/{plan_id}", response_class=HTMLResponse)
async def plan_page(request: Request, plan_id: str) -> Response:
    token = await _require_login(request)
    if not token:
        return _login_redirect()
    from app.models import AdminChangePlan

    async with SessionLocal() as session:
        plan = await session.get(AdminChangePlan, plan_id)
        if plan is None:
            raise HTTPException(404, "计划不存在")
    body = (
        "<h2>确认具体管理变更</h2><p>请人工核对来源、群 ID、完整参数及当前版本。确认后不能改变参数；5 分钟内仅执行一次。</p>"
        f"<p>发起身份：{_esc(plan.requestor)}；状态：{_esc(plan.status)}；操作：{_esc(plan.action)}</p>"
        f"<h3>完整计划</h3><pre>{_esc(plan.params_json)}</pre>"
        f"<h3>预览时状态 / 版本</h3><pre>{_esc(plan.expected_state_json)}</pre>"
    )
    if plan.status == "PENDING":
        body += f'<form method=post action="/admin/plans/{_esc(plan.id)}/approve">{_csrf_field(token)}<button class="btn danger">我已人工核对，批准此计划</button></form>'
    elif plan.status == "APPROVED" and plan.requestor == _human_actor(token):
        body += f'<form method=post action="/admin/plans/{_esc(plan.id)}/execute">{_csrf_field(token)}<button class="btn danger">执行已批准的完整计划</button></form>'
    elif plan.status == "APPROVED":
        body += "<p>已批准。由发起该计划的 Agent 在有效期内执行。</p>"
    return _page("管理计划", body)


@router.post("/plans/{plan_id}/approve")
async def approve_plan(request: Request, plan_id: str, csrf: str = Form("")) -> Response:
    token = await _require_admin_post(request, csrf)
    from app.web.agent_confirm import approve_confirmation

    async with SessionLocal() as session:
        if not await approve_confirmation(session, plan_id, human=_human_actor(token)):
            raise HTTPException(409, "计划已过期或已处理，请重新预览")
    return RedirectResponse(f"/admin/plans/{plan_id}", status_code=303)


@router.post("/plans/{plan_id}/execute")
async def execute_human_plan(request: Request, plan_id: str, csrf: str = Form("")) -> Response:
    token = await _require_admin_post(request, csrf)
    async with SessionLocal() as session:
        await _execute_plan(session, plan_id, actor=_human_actor(token))
    return RedirectResponse(f"/admin/plans/{plan_id}", status_code=303)


@router.get("/api/plans/{plan_id}")
async def api_plan(request: Request, plan_id: str) -> dict[str, Any]:
    actor = _require_api_auth(request)
    from app.models import AdminChangePlan

    async with SessionLocal() as session:
        plan = await session.get(AdminChangePlan, plan_id)
        if plan is None or plan.requestor != actor:
            raise HTTPException(404, "计划不存在")
        return _plan_response(plan)


@router.post("/api/emergency-stop")
async def api_emergency_stop(request: Request) -> dict[str, bool]:
    actor = _require_api_auth(request, scope="emergency:stop")
    from app.core.emergency_stop import set_emergency_stop

    async with SessionLocal() as session:
        await set_emergency_stop(session, True, actor=actor)
    return {"emergency_stop": True}


@router.post("/emergency-stop")
async def human_emergency_stop(request: Request, csrf: str = Form("")) -> Response:
    token = await _require_admin_post(request, csrf)
    from app.core.emergency_stop import set_emergency_stop

    async with SessionLocal() as session:
        await set_emergency_stop(session, True, actor=_human_actor(token))
    return RedirectResponse("/admin/groups", status_code=303)


@router.post("/emergency-resume")
async def human_emergency_resume(request: Request, csrf: str = Form("")) -> Response:
    token = await _require_admin_post(request, csrf)
    from app.core.emergency_stop import emergency_stop_state
    from app.web.agent_confirm import create_confirmation

    async with SessionLocal() as session:
        plan = await create_confirmation(
            session,
            action="emergency_resume",
            params={"active": False},
            expected_state=await emergency_stop_state(session),
            requestor=_human_actor(token),
        )
    return RedirectResponse(f"/admin/plans/{plan.id}", status_code=303)


@router.get("/api/stats/ai")
async def api_ai_stats(request: Request) -> list[dict[str, Any]]:
    """每日AI调用统计（AI Agent 对接用）。"""
    _require_api_auth(request, scope="project:read")
    async with SessionLocal() as session:
        return await _build_ai_daily_stats(session)
