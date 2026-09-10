"""Human-only, server-rendered notification handoff, never punishment approval."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta, timezone

from fastapi import APIRouter, Form, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.db import SessionLocal
from app.models import AdminAudit
from app.notifications.config import NotificationSettings
from app.notifications.models import NotificationDelivery, NotificationNotice
from app.notifications.service import acknowledge_notice
from app.web.routes import (
    _csrf_field,
    _esc,
    _human_actor,
    _login_redirect,
    _page,
    _require_admin_post,
    _require_login,
)

router = APIRouter(prefix="/admin/notifications", include_in_schema=False)
PAGE_SIZE = 50
_PAGE_HEAD = (
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    "<style>header{flex-wrap:wrap;gap:1rem}header>div{line-height:1.8}"
    "main{padding:clamp(.75rem,3vw,1.5rem)}"
    ".notification-center{--text-subtle:#59616d;--line:#e2e4e8;--focus:#2563eb}"
    ".notification-center .muted{color:var(--text-subtle);font-size:.875rem}"
    ".notification-center h1{font-size:1.5rem;margin:0 0 .75rem}"
    ".notification-center h2{font-size:1.125rem;margin:0;overflow-wrap:anywhere}"
    ".notification-center p{line-height:1.6;overflow-wrap:anywhere}"
    ".notification-item{background:#fff;border:1px solid var(--line);border-radius:.5rem;padding:1rem;margin:1rem 0}"
    ".notice-heading,.notice-footer,.notification-pager{display:flex;flex-wrap:wrap;align-items:center;gap:.75rem}"
    ".notice-heading{justify-content:space-between}.notice-state{font-weight:600}"
    ".notice-description{white-space:pre-wrap}.delivery-statuses{padding-left:1.25rem;line-height:1.8}"
    ".notification-center .btn{font:inherit;min-height:2.75rem;white-space:normal}"
    ".notification-center a:focus-visible,.notification-center button:focus-visible{outline:3px solid var(--focus);outline-offset:3px}"
    ".notification-pager{justify-content:space-between;margin-top:1.5rem}"
    "@media(max-width:30rem){.notification-center form,.notification-center form .btn{width:100%}}"
    "</style>"
)
_DELIVERY_LABELS = {
    "PENDING": "等待投递",
    "SENDING": "投递中",
    "SENT": "服务已接受",
    "FAILED": "投递失败",
    "UNKNOWN": "结果未知（不自动重发）",
    "SKIPPED": "已停止投递",
}


def _private_response(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    )
    return response


def _display_time(value: datetime | None) -> str:
    if value is None:
        return "—"
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    local = aware.astimezone(timezone(timedelta(hours=8)))
    return f"{local:%Y-%m-%d %H:%M:%S}"


def _unavailable_response() -> Response:
    response = _page(
        "通知暂时不可用",
        '<section class="notification-center"><h1>通知暂时不可用</h1>'
        '<p role="alert">无法确认当前接手状态。请通过独立通知渠道联系值班人，不要把本次操作视为接手成功。</p>'
        '<a class="btn" href="/admin/notifications">重试查看</a></section>',
        extra_head=_PAGE_HEAD,
    )
    response.status_code = 503
    return _private_response(response)


def _delivery_list(deliveries: list[NotificationDelivery]) -> str:
    if not deliveries:
        return '<p class="muted">尚无投递记录；不能据此认定通知已送达。</p>'
    rows = []
    for delivery in deliveries:
        channel = {"email": "邮件", "qq": "管理员 QQ 群"}.get(delivery.channel, "通知通道")
        audience = {"primary": "首位接管人", "backup": "备份接管人"}.get(
            delivery.audience, "接管人"
        )
        status = _DELIVERY_LABELS.get(delivery.status, "待核验状态")
        rows.append(f"<li>{_esc(channel)} · {_esc(audience)}：{_esc(status)}</li>")
    # No destinations, internal keys, user IDs or raw transport errors are rendered.
    return '<ul class="delivery-statuses">' + "".join(rows) + "</ul>"


def _notice_card(
    notice: NotificationNotice, deliveries: list[NotificationDelivery], csrf: str, page: int
) -> str:
    urgency = "需及时处理" if notice.severity == "page" else "待处理事项"
    state = "管理员已接手" if notice.acknowledged_at else "尚未有人接手"
    if notice.resolved_at:
        state = "已恢复" if notice.kind == "fault" else "本条提醒已结束（不代表业务处理完成）"
        if notice.acknowledged_at:
            state += " · 管理员已接手"
    escalation = " · 已进入升级流程" if notice.escalated_at else ""
    action = ""
    if notice.acknowledged_at is None and notice.resolved_at is None:
        action = (
            f'<form method="post" action="/admin/notifications/{notice.id}/ack">'
            f'{csrf}<input type="hidden" name="page" value="{page}">'
            '<button type="submit" class="btn ok">确认接手（不执行处罚）</button></form>'
        )
    elif notice.acknowledged_at:
        action = f'<p class="muted">接手时间：{_esc(_display_time(notice.acknowledged_at))}</p>'
    return (
        f'<article class="notification-item" id="notice-{notice.id}" aria-labelledby="notice-title-{notice.id}">'
        '<div class="notice-heading">'
        f'<h2 id="notice-title-{notice.id}">{_esc(notice.subject[:160])}</h2>'
        f'<span class="notice-state">{_esc(state + escalation)}</span></div>'
        f'<p class="muted">{_esc(urgency)} · {_esc(_display_time(notice.created_at))}（北京时间）</p>'
        f'<p class="notice-description">{_esc(notice.body[:2000])}</p>'
        f'{_delivery_list(deliveries)}<div class="notice-footer">{action}</div></article>'
    )


@router.get("")
async def notification_list(
    request: Request,
    page: int = Query(1, ge=1, le=1_000_000),
) -> Response:
    token = await _require_login(request)
    if not token:
        return _private_response(_login_redirect())
    try:
        async with SessionLocal() as session:
            total = int(
                await session.scalar(select(func.count()).select_from(NotificationNotice)) or 0
            )
            pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            if page > pages:
                return _private_response(
                    RedirectResponse(f"/admin/notifications?page={pages}", status_code=303)
                )
            notices = list(
                await session.scalars(
                    select(NotificationNotice)
                    .order_by(NotificationNotice.created_at.desc(), NotificationNotice.id.desc())
                    .offset((page - 1) * PAGE_SIZE)
                    .limit(PAGE_SIZE)
                )
            )
            deliveries: dict[int, list[NotificationDelivery]] = defaultdict(list)
            if notices:
                for delivery in await session.scalars(
                    select(NotificationDelivery)
                    .where(NotificationDelivery.notice_id.in_([notice.id for notice in notices]))
                    .order_by(NotificationDelivery.id)
                ):
                    deliveries[delivery.notice_id].append(delivery)
    except SQLAlchemyError:
        return _unavailable_response()
    enabled = NotificationSettings().enabled
    configuration = (
        "主动通知已启用；请核验实际投递和接手状态。"
        if enabled
        else "主动通知未启用；当前仅可在后台查看和接手。"
    )
    cards = "".join(
        _notice_card(notice, deliveries[notice.id], _csrf_field(token), page) for notice in notices
    )
    if not cards:
        cards = '<section class="card" role="status"><h2>暂无通知</h2><p>没有通知记录不等于所有服务正常，请同时核对健康监控。</p></section>'
    previous = (
        f'<a class="btn" href="/admin/notifications?page={page - 1}">上一页</a>'
        if page > 1
        else "<span>已是第一页</span>"
    )
    following = (
        f'<a class="btn" href="/admin/notifications?page={page + 1}">下一页</a>'
        if page < pages
        else "<span>已是最后一页</span>"
    )
    body = (
        '<section class="notification-center"><h1>通知与接手</h1>'
        f"<p>{_esc(configuration)}</p>"
        "<p>投递状态只表示通道处理结果，不代表人工已收到或接手。确认接手只登记处理责任，"
        "不执行处罚，也不是批准踢人。故障恢复与人工接手是两种不同状态。"
        "摘要投递后结束提醒不等于完成复核。</p>"
        f'<a class="btn" href="/admin/notifications?page={page}">刷新状态</a>{cards}'
        f'<nav class="notification-pager" aria-label="通知分页">{previous}'
        f"<span>第 {page} / {pages} 页 · 共 {total} 条</span>{following}</nav></section>"
    )
    return _private_response(_page("通知与接手", body, extra_head=_PAGE_HEAD))


@router.post("/{notice_id}/ack")
async def acknowledge_notification(
    request: Request,
    notice_id: int,
    csrf: str = Form(""),
    page: int = Form(1, ge=1, le=1_000_000),
) -> Response:
    token = await _require_admin_post(request, csrf)
    actor = _human_actor(token)
    try:
        async with SessionLocal() as session:
            notice = await session.get(NotificationNotice, notice_id)
            if notice is None:
                raise HTTPException(404, "通知不存在")
            acknowledged = await acknowledge_notice(session, notice_id, actor=actor)
            if acknowledged:
                session.add(
                    AdminAudit(
                        operator=actor,
                        action="notification_ack",
                        target_type="notification",
                        target_id=str(notice_id),
                        detail_json=json.dumps(
                            {"notice_id": notice_id, "punishment_changed": False}
                        ),
                    )
                )
            await session.commit()
    except SQLAlchemyError:
        return _unavailable_response()
    # Display the committed database state, never a query-string claim of success.
    return _private_response(
        RedirectResponse(f"/admin/notifications?page={page}#notice-{notice_id}", status_code=303)
    )
