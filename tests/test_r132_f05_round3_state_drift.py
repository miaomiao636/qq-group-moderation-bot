# ruff: noqa: E402, I001, F401, F811
# Reviewer round-4 probe pack (97d68d1), promoted verbatim into the repo test suite.
# This header changes no assertion and no logic.
from __future__ import annotations

import asyncio
import os
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
DB_PATH = Path(os.environ["DATABASE_URL"].split(":///", 1)[1]).resolve()
assert DB_PATH.parent.name.startswith("qqbot-test-")
assert DB_PATH.is_relative_to(Path(tempfile.gettempdir()).resolve())

from sqlalchemy import event
from app.db import SessionLocal, engine
from app.moderation import allowlist
from app.web import agent_confirm
from tests.test_r132_f05_plan_integrity import clients, preview, confirm, seed, state


@pytest.mark.parametrize("operation", ["delete", "disable", "note"])
def test_unchanged_file_member_changed_after_claim_invalidates_sync(
    clients, monkeypatch, operation
):
    member_id = asyncio.run(seed(enabled=True, note="initial"))
    content = "920000001,initial\n920000002\n"
    plan_id = preview(clients[0], content)
    real_claim = agent_confirm.claim_confirmation
    real_apply = allowlist.apply_member_import
    reached = {"claim": 0, "external_change": 0, "execute_sync": 0}

    async def claim_then_change(*args, **kwargs):
        claimed = await real_claim(*args, **kwargs)
        assert claimed is not None
        reached["claim"] += 1
        async with SessionLocal() as other:
            if operation == "delete":
                await allowlist.delete_member(other, member_id, operator="human:synthetic-other")
            elif operation == "disable":
                await allowlist.set_member_enabled(
                    other, member_id, False, operator="human:synthetic-other"
                )
            else:
                other_plan = await allowlist.plan_member_import(other, "920000001,newer-note\n")
                await real_apply(other, other_plan, operator="human:synthetic-other")
        reached["external_change"] += 1
        return claimed

    async def counted_apply(*args, **kwargs):
        assert kwargs["sync_file_text"] == content
        reached["execute_sync"] += 1
        return await real_apply(*args, **kwargs)

    monkeypatch.setattr(agent_confirm, "claim_confirmation", claim_then_change)
    monkeypatch.setattr(allowlist, "apply_member_import", counted_apply)
    response = confirm(clients[0], content, plan_id)
    actual = asyncio.run(state(plan_id))
    assert reached == {"claim": 1, "external_change": 1, "execute_sync": 1}, (reached, actual)
    # Preserve the second administrator's legitimate change. A stale full sync
    # must request another preview instead of asserting the old target is applied.
    if operation == "delete":
        assert "920000001" not in actual["members"], actual
    elif operation == "disable":
        assert actual["members"]["920000001"]["enabled"] is False, actual
    else:
        assert actual["members"]["920000001"]["note"] == "newer-note", actual
    assert actual["plan_status"] != "APPLIED", (response.status_code, actual)
    assert "920000002" not in actual["members"], actual
    assert not actual["audit"] if operation != "note" else len(actual["audit"]) == 1


@pytest.mark.parametrize("operation", ["add-outside", "enable-outside", "add-other-provider"])
def test_new_set_guard_and_provider_scope_controls(clients, monkeypatch, operation):
    asyncio.run(seed(enabled=True))
    outside_id = (
        asyncio.run(seed("920000003", enabled=False)) if operation == "enable-outside" else None
    )
    content = "920000001\n920000002\n"
    plan_id = preview(clients[0], content)
    real_claim = agent_confirm.claim_confirmation
    reached = []

    async def claim_then_change(*args, **kwargs):
        claimed = await real_claim(*args, **kwargs)
        assert claimed is not None
        async with SessionLocal() as other:
            if operation == "enable-outside":
                await allowlist.set_member_enabled(
                    other, outside_id, True, operator="human:synthetic-other"
                )
            else:
                provider = "other-provider" if operation == "add-other-provider" else "onebot"
                await allowlist.add_member(
                    other, "920000003", operator="human:synthetic-other", provider=provider
                )
        reached.append(True)
        return claimed

    monkeypatch.setattr(agent_confirm, "claim_confirmation", claim_then_change)
    response = confirm(clients[0], content, plan_id)
    actual = asyncio.run(state(plan_id))
    assert reached == [True]
    assert actual["members"]["920000003"]["enabled"] is True, actual
    if operation == "add-other-provider":
        assert actual["plan_status"] == "APPLIED", (response.status_code, actual)
        assert actual["members"]["920000002"]["enabled"] is True, actual
    else:
        assert actual["plan_status"] != "APPLIED", (response.status_code, actual)
        assert "920000002" not in actual["members"], actual
        assert not actual["audit"], actual


def test_approved_outside_member_disable_still_succeeds_control(clients):
    asyncio.run(seed(enabled=True))
    asyncio.run(seed("920000003", enabled=True))
    content = "920000001\n920000002\n"
    plan_id = preview(clients[0], content)
    confirm(clients[0], content, plan_id)
    actual = asyncio.run(state(plan_id))
    assert actual["plan_status"] == "APPLIED", actual
    assert actual["members"]["920000003"]["enabled"] is False, actual
    assert actual["members"]["920000002"]["enabled"] is True, actual


def test_combined_enable_and_note_is_one_guarded_update_control(clients):
    asyncio.run(seed(enabled=False, note="initial"))
    content = "920000001,new-note\n"
    plan_id = preview(clients[0], content)
    updates = []

    def count_updates(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE allowlist_members"):
            updates.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", count_updates)
    try:
        confirm(clients[0], content, plan_id)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count_updates)
    actual = asyncio.run(state(plan_id))
    assert len(updates) == 1 and "updated_at = ?" in updates[0], updates
    assert actual["members"]["920000001"] == {"enabled": True, "note": "new-note"}, actual
    assert actual["plan_status"] == "APPLIED" and len(actual["audit"]) == 1, actual
