# ruff: noqa: E402, I001, F401
# Reviewer round-2 probe pack (a354d17), promoted verbatim into the repo test suite.
# Isolation asserts intentionally run BEFORE application imports (E402 is by design).
# This header changes no assertion and no logic.
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path

import pytest

assert os.environ["APP_ENV"] == "test"
assert os.environ["ACTION_MODE"] == "SHADOW"
assert all(
    os.environ[key] == "false"
    for key in (
        "AI_ENABLED",
        "ONEBOT_ACTIONS_ENABLED",
        "NOTIFICATIONS_ENABLED",
        "NOTIFICATION_QQ_ENABLED",
        "NOTIFICATION_EMAIL_ENABLED",
        "NOTIFICATION_HEARTBEAT_ENABLED",
    )
)
db = Path(os.environ["DATABASE_URL"].split(":///", 1)[1]).resolve()
assert db.parent.name.startswith("qqbot-test-")
assert db.is_relative_to(Path(tempfile.gettempdir()).resolve())

from fastapi.testclient import TestClient
from sqlalchemy import delete, select, event
from app.db import SessionLocal, engine
from app.models import AllowlistMember, AdminAudit, AdminChangePlan
from app.moderation import allowlist
from app.web import auth, agent_confirm

ENDPOINT = "/admin/allowlist/members/import"


async def clear():
    async with SessionLocal() as session:
        for model in (AllowlistMember, AdminChangePlan, AdminAudit):
            await session.execute(delete(model))
        await session.commit()


@pytest.fixture
def clients():
    from app.main import app

    asyncio.run(clear())
    result = []
    for _ in range(2):
        client = TestClient(app, raise_server_exceptions=False)
        reply = client.post(
            "/admin/login",
            data={"username": "admin", "password": "test-admin-pass"},
            follow_redirects=False,
        )
        assert reply.status_code == 303
        result.append((client, auth.csrf_token(client.cookies[auth.SESSION_COOKIE])))
    yield result
    for client, _ in result:
        auth.logout(client.cookies[auth.SESSION_COOKIE])
        client.close()
    asyncio.run(clear())


def preview(client_csrf, text):
    client, csrf = client_csrf
    response = client.post(ENDPOINT, data={"csrf": csrf, "text": text}, follow_redirects=False)
    assert response.status_code == 200
    return re.search(r'name="plan_id" value="([^"]+)"', response.text)[1]


def confirm(client_csrf, text, plan_id):
    client, csrf = client_csrf
    return client.post(
        ENDPOINT,
        data={"csrf": csrf, "text": text, "plan_id": plan_id, "confirmed": "1", "ack": "1"},
        follow_redirects=False,
    )


async def seed(user="920000001", *, enabled=False, note="initial"):
    async with SessionLocal() as session:
        row, _ = await allowlist.add_member(session, user, operator="synthetic", note=note)
        member_id = row.id
        if not enabled:
            await allowlist.set_member_enabled(session, member_id, False, operator="synthetic")
        return member_id


async def state(plan_id):
    async with SessionLocal() as session:
        rows = (await session.scalars(select(AllowlistMember))).all()
        plan = await session.get(AdminChangePlan, plan_id)
        audit = (
            await session.scalars(
                select(AdminAudit).where(AdminAudit.action == "allowlist_members_import")
            )
        ).all()
        return {
            "members": {r.external_user_id: {"enabled": r.enabled, "note": r.note} for r in rows},
            "plan_status": plan.status,
            "audit": [json.loads(a.detail_json) for a in audit],
        }


def test_cross_session_cannot_write_control(clients):
    content = "920000001\n"
    plan_id = preview(clients[0], content)
    response = confirm(clients[1], content, plan_id)
    actual = asyncio.run(state(plan_id))
    assert not actual["members"], (response.status_code, actual)


def test_cross_session_rejection_must_not_consume_other_session_plan(clients):
    content = "920000001\n"
    plan_id = preview(clients[0], content)
    confirm(clients[1], content, plan_id)
    response = confirm(clients[0], content, plan_id)
    actual = asyncio.run(state(plan_id))
    assert actual["members"].get("920000001", {}).get("enabled") is True, (
        response.status_code,
        actual,
    )


@pytest.mark.parametrize(
    "first_enable", [False, True], ids=["explicit-disable", "enable-then-revoke"]
)
def test_revocation_after_claim_must_not_be_overwritten(clients, monkeypatch, first_enable):
    member_id = asyncio.run(seed())
    content = "920000001\n"
    plan_id = preview(clients[0], content)
    real_claim = agent_confirm.claim_confirmation

    async def claim_then_revoke(*args, **kwargs):
        claimed = await real_claim(*args, **kwargs)
        assert claimed is not None
        # Independent administrator's explicit, audited disable completes after
        # the confirmation check and before apply. It is a supported idempotent
        # revocation, even though the row was already disabled at preview time.
        async with SessionLocal() as other:
            if first_enable:
                await allowlist.set_member_enabled(
                    other, member_id, True, operator="human:synthetic-revoker"
                )
            await allowlist.set_member_enabled(
                other, member_id, False, operator="human:synthetic-revoker"
            )
        return claimed

    monkeypatch.setattr(agent_confirm, "claim_confirmation", claim_then_revoke)
    response = confirm(clients[0], content, plan_id)
    actual = asyncio.run(state(plan_id))
    assert actual["members"]["920000001"]["enabled"] is False, (response.status_code, actual)


def test_delete_after_claim_must_not_report_phantom_enable(clients, monkeypatch):
    member_id = asyncio.run(seed())
    content = "920000001\n"
    plan_id = preview(clients[0], content)
    real_claim = agent_confirm.claim_confirmation

    async def claim_then_delete(*args, **kwargs):
        claimed = await real_claim(*args, **kwargs)
        assert claimed is not None
        async with SessionLocal() as other:
            await allowlist.delete_member(other, member_id, operator="human:synthetic-revoker")
        return claimed

    monkeypatch.setattr(agent_confirm, "claim_confirmation", claim_then_delete)
    response = confirm(clients[0], content, plan_id)
    actual = asyncio.run(state(plan_id))
    assert actual["plan_status"] != "APPLIED", (response.status_code, actual)
    assert not any("920000001" in a["enabled"] for a in actual["audit"]), actual


def test_note_changed_since_preview_requires_new_preview(clients):
    member_id = asyncio.run(seed(enabled=True))
    content = "920000001,approved-note\n"
    plan_id = preview(clients[0], content)
    # A second normal import changes the same row's note, after first preview.
    other_content = "920000001,newer-note\n"
    other_plan = preview(clients[1], other_content)
    assert confirm(clients[1], other_content, other_plan).status_code == 303
    assert asyncio.run(state(plan_id))["members"]["920000001"]["note"] == "newer-note"
    response = confirm(clients[0], content, plan_id)
    actual = asyncio.run(state(plan_id))
    assert actual["members"]["920000001"]["note"] == "newer-note", (
        response.status_code,
        actual,
        member_id,
    )


def test_applied_marker_and_members_are_atomic(clients):
    content = "920000001\n"
    plan_id = preview(clients[0], content)

    def fail_final_marker(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE admin_change_plans") and "APPLIED" in parameters:
            raise RuntimeError("synthetic fault before final plan marker commit")

    event.listen(engine.sync_engine, "before_cursor_execute", fail_final_marker)
    try:
        response = confirm(clients[0], content, plan_id)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", fail_final_marker)
    actual = asyncio.run(state(plan_id))
    assert response.status_code == 500
    assert not actual["members"], actual


def test_import_audit_failure_rolls_back_members_control(clients):
    content = "920000001\n"
    plan_id = preview(clients[0], content)

    def fail_import_audit(connection, cursor, statement, parameters, context, executemany):
        if (
            statement.startswith("INSERT INTO admin_audits")
            and "allowlist_members_import" in parameters
        ):
            raise RuntimeError("synthetic import audit write failure")

    event.listen(engine.sync_engine, "before_cursor_execute", fail_import_audit)
    try:
        response = confirm(clients[0], content, plan_id)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", fail_import_audit)
    actual = asyncio.run(state(plan_id))
    assert response.status_code == 500
    assert not actual["members"], actual


def test_changed_file_is_rejected_control(clients):
    plan_id = preview(clients[0], "920000001\n")
    confirm(clients[0], "920000002\n", plan_id)
    actual = asyncio.run(state(plan_id))
    assert not actual["members"], actual


def test_fresh_stale_preview_with_real_plan_id_is_rejected_control(clients):
    member_id = asyncio.run(seed(enabled=True))
    content = "920000001\n920000002\n"
    plan_id = preview(clients[0], content)

    async def revoke():
        async with SessionLocal() as other:
            await allowlist.set_member_enabled(
                other, member_id, False, operator="human:synthetic-revoker"
            )

    asyncio.run(revoke())
    confirm(clients[0], content, plan_id)
    actual = asyncio.run(state(plan_id))
    assert actual["members"]["920000001"]["enabled"] is False, actual
    assert "920000002" not in actual["members"], actual


def test_normal_plan_once_and_replay_after_revocation_control(clients):
    content = "920000001\n"
    plan_id = preview(clients[0], content)
    assert confirm(clients[0], content, plan_id).status_code == 303
    actual = asyncio.run(state(plan_id))
    assert actual["members"]["920000001"]["enabled"] is True
    assert actual["plan_status"] == "APPLIED"
    assert len(actual["audit"]) == 1

    async def revoke():
        async with SessionLocal() as other:
            member_id = await other.scalar(select(AllowlistMember.id))
            await allowlist.set_member_enabled(
                other, member_id, False, operator="human:synthetic-revoker"
            )

    asyncio.run(revoke())
    confirm(clients[0], content, plan_id)
    actual = asyncio.run(state(plan_id))
    assert actual["members"]["920000001"]["enabled"] is False, actual
    assert len(actual["audit"]) == 1


def test_expired_plan_does_not_write_control(clients):
    from datetime import UTC, datetime, timedelta

    content = "920000001\n"
    plan_id = preview(clients[0], content)

    async def expire():
        async with SessionLocal() as session:
            plan = await session.get(AdminChangePlan, plan_id)
            plan.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

    asyncio.run(expire())
    confirm(clients[0], content, plan_id)
    assert not asyncio.run(state(plan_id))["members"]


def test_large_disabling_requires_ack_control(clients):
    async def seed_many():
        for user in range(920000001, 920000009):
            await seed(str(user), enabled=True)

    asyncio.run(seed_many())
    content = "920000001\n"
    plan_id = preview(clients[0], content)
    client, csrf = clients[0]
    response = client.post(
        ENDPOINT,
        data={"csrf": csrf, "text": content, "plan_id": plan_id, "confirmed": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    actual = asyncio.run(state(plan_id))
    assert all(r["enabled"] for r in actual["members"].values()), actual
    assert actual["plan_status"] == "PENDING", actual
    confirm(clients[0], content, plan_id)
    actual = asyncio.run(state(plan_id))
    assert sum(r["enabled"] for r in actual["members"].values()) == 1, actual
