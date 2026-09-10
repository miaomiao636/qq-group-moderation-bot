"""Human-only notification handoff UI; all data and transports stay local."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
def notification_ui(tmp_path, monkeypatch):
    from app.db import Base
    from app.web import notifications, routes

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'notification-ui.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def setup():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(setup())
    monkeypatch.setattr(notifications, "SessionLocal", factory)
    monkeypatch.setattr(
        notifications,
        "NotificationSettings",
        lambda: SimpleNamespace(enabled=False, email_to="private-recipient@example.invalid"),
    )
    application = FastAPI()
    application.include_router(routes.router)
    application.include_router(notifications.router)
    with TestClient(application) as client:
        yield client, factory
    asyncio.run(engine.dispose())


def _login(client):
    response = client.post(
        "/admin/login",
        data={"username": "admin", "password": "test-admin-pass"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get("/admin/notifications")
    assert page.status_code == 200
    return page


def _csrf(page):
    found = re.search(r'name="?csrf"? value="([^"]+)"', page.text)
    assert found, "acknowledgement form requires a real session CSRF token"
    return found.group(1)


def _seed(
    factory,
    *,
    count=1,
    resolved=False,
    acknowledged=False,
    kind="test-notice",
    severity="page",
    hostile=False,
    delivery_status="SENT",
    channel="email",
    audience="primary",
):
    from app.notifications.models import NotificationDelivery, NotificationNotice

    async def write():
        ids = []
        async with factory() as session:
            for index in range(count):
                notice = NotificationNotice(
                    event_key=f"private-group-identity:{index}",
                    kind=kind,
                    severity=severity,
                    subject='<script>alert("notice")</script>'
                    if hostile
                    else f"测试通知 {index:03d}",
                    body="<img src=x onerror=alert(1)>" if hostile else "仅包含脱敏运行摘要。",
                    created_at=datetime(2026, 9, 10, tzinfo=UTC) + timedelta(seconds=index),
                    resolved_at=datetime.now(UTC) if resolved else None,
                    acknowledged_at=datetime.now(UTC) if acknowledged else None,
                )
                session.add(notice)
                await session.flush()
                ids.append(notice.id)
                session.add(
                    NotificationDelivery(
                        notice_id=notice.id,
                        channel=channel,
                        audience=audience,
                        status=delivery_status,
                        error_code="Traceback private-raw-user-identity" if hostile else "",
                    )
                )
            await session.commit()
        return ids

    return asyncio.run(write())


def test_empty_state_explains_disabled_notifications_without_config_leaks(notification_ui):
    client, _factory = notification_ui
    page = _login(client)
    assert "通知与接手" in page.text
    assert "暂无通知" in page.text
    assert "主动通知未启用" in page.text
    assert "不执行处罚" in page.text
    assert "不是批准踢人" in page.text
    assert "private-recipient" not in page.text
    assert 'name="viewport"' in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert "已记录接手" not in client.get("/admin/notifications?result=acknowledged").text


def test_notifications_are_newest_first_and_paginated_at_fifty(notification_ui):
    client, factory = notification_ui
    _seed(factory, count=51)
    first = _login(client)
    assert first.text.count('class="notification-item"') == 50
    assert first.text.index("测试通知 050") < first.text.index("测试通知 049")
    assert "测试通知 000" not in first.text
    assert 'href="/admin/notifications?page=2"' in first.text
    second = client.get("/admin/notifications?page=2")
    assert second.text.count('class="notification-item"') == 1
    assert "测试通知 000" in second.text
    assert client.get("/admin/notifications?page=0").status_code == 422


@pytest.mark.parametrize("bearer", ["", "test-agent-read", "test-agent-write"])
def test_anonymous_and_agent_bearer_cannot_read_or_acknowledge(
    notification_ui, bearer, monkeypatch
):
    from app.config import get_settings

    client, _factory = notification_ui
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_api_read_token", "test-agent-read")
    monkeypatch.setattr(settings, "agent_api_token", "test-agent-write")
    monkeypatch.setattr(settings, "agent_api_write_scopes", "project:read")
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    if bearer:
        assert client.get("/admin/api/status", headers=headers).status_code == 200
    response = client.get("/admin/notifications", headers=headers, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"
    assert client.post("/admin/notifications/1/ack", headers=headers).status_code == 401


def test_forged_session_cookie_is_not_a_human_login(notification_ui):
    client, _factory = notification_ui
    client.cookies.set("admin_session", "forged-notification-session")
    assert client.get("/admin/notifications", follow_redirects=False).status_code == 303
    assert client.post("/admin/notifications/1/ack").status_code == 401


@pytest.mark.parametrize("csrf", ["", "wrong-csrf"])
def test_acknowledgement_requires_csrf(notification_ui, csrf):
    client, factory = notification_ui
    notice_id = _seed(factory)[0]
    _login(client)
    assert (
        client.post(f"/admin/notifications/{notice_id}/ack", data={"csrf": csrf}).status_code == 403
    )


def test_html_is_escaped_and_private_transport_fields_are_omitted(notification_ui):
    client, factory = notification_ui
    _seed(factory, hostile=True)
    page = _login(client)
    assert "&lt;script&gt;" in page.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in page.text
    assert '<script>alert("notice")</script>' not in page.text
    assert "private-group-identity" not in page.text
    assert "private-raw-user-identity" not in page.text
    assert "Traceback" not in page.text


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("PENDING", "等待投递"),
        ("SENDING", "投递中"),
        ("SENT", "服务已接受"),
        ("FAILED", "投递失败"),
        ("UNKNOWN", "结果未知"),
        ("SKIPPED", "已停止投递"),
    ],
)
def test_delivery_status_does_not_claim_a_human_has_seen_it(notification_ui, status, expected):
    client, factory = notification_ui
    _seed(factory, delivery_status=status)
    page = _login(client)
    assert expected in page.text
    assert "尚未有人接手" in page.text
    assert "不代表人工已收到或接手" in page.text
    assert "确认接手（不执行处罚）" in page.text


@pytest.mark.parametrize(("channel", "label"), [("email", "邮件"), ("qq", "管理员 QQ 群")])
def test_delivery_channels_and_backup_audience_are_labeled(notification_ui, channel, label):
    client, factory = notification_ui
    _seed(factory, channel=channel, audience="backup")
    assert f"{label} · 备份接管人" in _login(client).text


def test_storage_failure_has_an_explicit_safe_error_state(notification_ui, monkeypatch):
    from contextlib import asynccontextmanager

    from app.web import notifications
    from sqlalchemy.exc import OperationalError

    client, factory = notification_ui
    notice_id = _seed(factory)[0]
    csrf = _csrf(_login(client))

    @asynccontextmanager
    async def unavailable():
        raise OperationalError("private-db-path", {}, RuntimeError("private stack trace"))
        yield  # pragma: no cover

    monkeypatch.setattr(notifications, "SessionLocal", unavailable)
    for response in (
        client.get("/admin/notifications"),
        client.post(f"/admin/notifications/{notice_id}/ack", data={"csrf": csrf}),
    ):
        assert response.status_code == 503
        assert "无法确认" in response.text
        assert "private-db-path" not in response.text
        assert "private stack trace" not in response.text


def test_ack_is_human_attributed_idempotent_and_never_changes_punishment(notification_ui):
    from app.actions.orchestrator import ActionIntent
    from app.cases.models import Case
    from app.models import AdminAudit
    from app.notifications.models import NotificationNotice

    client, factory = notification_ui
    notice_id = _seed(factory)[0]

    async def seed_controls():
        async with factory() as session:
            session.add(
                Case(case_no="notification-case", group_openid="private", member_openid="private")
            )
            session.add(
                ActionIntent(
                    idempotency_key="notice-action",
                    action="recall",
                    status="PENDING",
                    group_openid="private",
                )
            )
            await session.commit()

    asyncio.run(seed_controls())
    page = _login(client)
    token = _csrf(page)
    for _ in range(2):
        response = client.post(
            f"/admin/notifications/{notice_id}/ack",
            data={"csrf": token, "actor": "agent:forged", "action": "kick"},
            follow_redirects=False,
        )
        assert response.status_code == 303

    async def verify():
        async with factory() as session:
            notice = await session.get(NotificationNotice, notice_id)
            assert notice.acknowledged_at is not None
            assert notice.acknowledged_by.startswith("human:")
            assert "forged" not in notice.acknowledged_by
            audits = (
                await session.scalars(
                    select(AdminAudit).where(AdminAudit.action == "notification_ack")
                )
            ).all()
            assert len(audits) == 1
            assert audits[0].operator == notice.acknowledged_by
            assert json.loads(audits[0].detail_json)["punishment_changed"] is False
            assert (await session.scalar(select(Case))).status == "PENDING_REVIEW"
            assert (await session.scalar(select(ActionIntent))).status == "PENDING"

    asyncio.run(verify())
    page = client.get("/admin/notifications")
    assert "管理员已接手" in page.text
    assert "确认接手（不执行处罚）" not in page.text
    assert client.post("/admin/notifications/9999/ack", data={"csrf": token}).status_code == 404


@pytest.mark.parametrize("kind", ["review_summary", "recovery", "delivery_problem"])
@pytest.mark.parametrize("acknowledged", [False, True])
def test_ended_ticket_does_not_claim_business_recovery(notification_ui, kind, acknowledged):
    client, factory = notification_ui
    _seed(factory, resolved=True, acknowledged=acknowledged, kind=kind, severity="ticket")
    page = _login(client)
    assert "本条提醒已结束（不代表业务处理完成）" in page.text
    assert "摘要投递后结束提醒不等于完成复核" in page.text
    assert "已恢复" not in page.text
    assert ("管理员已接手" in page.text) is acknowledged
    assert "确认接手（不执行处罚）" not in page.text


def test_recovered_notice_cannot_be_reopened_or_escalated_by_ack(notification_ui):
    from app.notifications.models import NotificationDelivery, NotificationNotice

    client, factory = notification_ui
    recovered_id = _seed(factory, resolved=True, kind="fault")[0]
    page = _login(client)
    assert "已恢复" in page.text
    assert "确认接手（不执行处罚）" not in page.text
    from app.web import auth

    token = auth.csrf_token(client.cookies.get("admin_session"))
    response = client.post(
        f"/admin/notifications/{recovered_id}/ack", data={"csrf": token}, follow_redirects=False
    )
    assert response.status_code == 303

    async def verify():
        async with factory() as session:
            notice = await session.get(NotificationNotice, recovered_id)
            assert notice.resolved_at is not None
            assert notice.acknowledged_at is None
            assert notice.escalated_at is None
            assert len((await session.scalars(select(NotificationDelivery))).all()) == 1

    asyncio.run(verify())
