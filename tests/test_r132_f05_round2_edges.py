# ruff: noqa: E402, I001, F401, F811
# Reviewer round-3 probe pack (7ec5553), promoted verbatim into the repo test suite.
# Isolation asserts intentionally run BEFORE application imports (E402 is by design).
# This header changes no assertion and no logic.
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote

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


@pytest.mark.parametrize(
    "enabled,new_note",
    [(False, "initial"), (True, "changed-note"), (False, "changed-note")],
    ids=["enable-only", "note-only", "enable-and-note"],
)
def test_single_member_approved_change_applies(clients, enabled, new_note):
    asyncio.run(seed(enabled=enabled))
    content = f"920000001,{new_note}\n"
    plan_id = preview(clients[0], content)
    response = confirm(clients[0], content, plan_id)
    actual = asyncio.run(state(plan_id))
    actual["location"] = unquote(response.headers.get("location", ""))
    assert actual["members"]["920000001"] == {"enabled": True, "note": new_note}, actual
    assert actual["plan_status"] == "APPLIED", actual
    assert len(actual["audit"]) == 1, actual


@pytest.mark.parametrize("operation", ["explicit-revoke", "enable-revoke", "delete"])
def test_postclaim_mutation_is_actually_executed_and_rejected(clients, monkeypatch, operation):
    member_id = asyncio.run(seed())
    content = "920000001\n"
    plan_id = preview(clients[0], content)
    real_claim = agent_confirm.claim_confirmation
    real_apply = allowlist.apply_member_import
    reached = {"claim_completed": 0, "mutation_completed": 0, "apply_entered": 0}

    async def claim_then_mutate(*args, **kwargs):
        claimed = await real_claim(*args, **kwargs)
        assert claimed is not None
        reached["claim_completed"] += 1
        async with SessionLocal() as other:
            if operation == "delete":
                await allowlist.delete_member(other, member_id, operator="human:synthetic-other")
            else:
                if operation == "enable-revoke":
                    await allowlist.set_member_enabled(
                        other, member_id, True, operator="human:synthetic-other"
                    )
                await allowlist.set_member_enabled(
                    other, member_id, False, operator="human:synthetic-other"
                )
        reached["mutation_completed"] += 1
        return claimed

    async def counted_apply(*args, **kwargs):
        reached["apply_entered"] += 1
        return await real_apply(*args, **kwargs)

    monkeypatch.setattr(agent_confirm, "claim_confirmation", claim_then_mutate)
    monkeypatch.setattr(allowlist, "apply_member_import", counted_apply)
    response = confirm(clients[0], content, plan_id)
    actual = asyncio.run(state(plan_id))
    assert reached == {"claim_completed": 1, "mutation_completed": 1, "apply_entered": 1}, (
        reached,
        actual,
    )
    assert response.status_code == 303
    assert "名单在确认期间被其它操作改动" in unquote(response.headers.get("location", ""))
    assert not actual["audit"], actual
    if operation == "delete":
        assert not actual["members"], actual
    else:
        assert actual["members"]["920000001"]["enabled"] is False, actual
    assert actual["plan_status"] != "APPLIED", actual


def test_final_plan_marker_failure_hook_reached_and_atomic(clients):
    content = "920000001\n"
    plan_id = preview(clients[0], content)
    reached = []

    def fail_final_marker(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE admin_change_plans") and "APPLIED" in parameters:
            reached.append(True)
            raise RuntimeError("synthetic final marker fault")

    event.listen(engine.sync_engine, "before_cursor_execute", fail_final_marker)
    try:
        response = confirm(clients[0], content, plan_id)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", fail_final_marker)
    actual = asyncio.run(state(plan_id))
    assert reached == [True], actual
    assert response.status_code == 500
    assert not actual["members"] and not actual["audit"], actual


SQLITE_WORKER = """
import sqlite3, sys
con = sqlite3.connect(sys.argv[1], timeout=0.05)
try:
    con.execute("UPDATE allowlist_members SET enabled=0 WHERE external_user_id=?", ('920000001',))
    con.commit()
    print('WRITE_SUCCEEDED')
except sqlite3.OperationalError as exc:
    print(str(exc))
finally:
    con.close()
"""


def _other_process_write():
    return subprocess.run(
        [sys.executable, "-c", SQLITE_WORKER, str(DB_PATH)],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    ).stdout.strip()


def test_executor_begin_immediate_blocks_separate_process_writer(clients, monkeypatch):
    asyncio.run(seed())
    content = "920000001\n"
    plan_id = preview(clients[0], content)
    real_apply = allowlist.apply_member_import
    reached = []

    async def apply_with_contending_process(*args, **kwargs):
        assert kwargs["commit"] is False
        # This hook runs only after the route's executor BEGIN IMMEDIATE.
        reached.append(_other_process_write())
        return await real_apply(*args, **kwargs)

    monkeypatch.setattr(allowlist, "apply_member_import", apply_with_contending_process)
    response = confirm(clients[0], content, plan_id)
    actual = asyncio.run(state(plan_id))
    assert reached == ["database is locked"], (reached, actual)
    assert response.status_code == 303 and actual["plan_status"] == "APPLIED", actual
    assert actual["members"]["920000001"]["enabled"] is True, actual
    assert _other_process_write() == "WRITE_SUCCEEDED"
    assert asyncio.run(state(plan_id))["members"]["920000001"]["enabled"] is False


def test_unrelated_concurrent_member_addition_invalidates_whole_sync(clients, monkeypatch):
    asyncio.run(seed(enabled=True))
    content = "920000001\n920000002\n"
    plan_id = preview(clients[0], content)
    real_claim = agent_confirm.claim_confirmation
    reached = []

    async def claim_then_add(*args, **kwargs):
        claimed = await real_claim(*args, **kwargs)
        assert claimed is not None
        async with SessionLocal() as other:
            await allowlist.add_member(other, "920000003", operator="human:synthetic-other")
        reached.append(True)
        return claimed

    monkeypatch.setattr(agent_confirm, "claim_confirmation", claim_then_add)
    response = confirm(clients[0], content, plan_id)
    actual = asyncio.run(state(plan_id))
    assert reached == [True]
    # Full-table sync must reject changed target membership instead of claiming
    # the approved pre-addition snapshot was applied successfully.
    assert actual["plan_status"] != "APPLIED", (response.status_code, actual)
    assert "920000002" not in actual["members"], actual
