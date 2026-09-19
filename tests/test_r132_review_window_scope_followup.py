# ruff: noqa: E402, I001, F401, F811, SIM105
# Reviewer round-7 probe pack (2ff2a8a), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
"""Independent round-6 stats checks; synthetic DB, no external actions.

Run from the reviewed checkout with PYTHONPATH=.:tests and pytest -p conftest.
Uses real parser / intent / audit seams. Diagnostic passing tests demonstrate
the limits of available identity and current-state authorization evidence.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.actions.orchestrator import _create_intent, _execute_intent, _log_action_result
from app.adapters.onebot.parser import OneBotMessageSource
from app.core.contracts import ActionResult
from tests.test_r132_review_window_stats import (
    INSIDE,
    action,
    collect,
    db,
    decision,
)


def parsed(self_id: str, msg_id: str, group="1001"):
    return OneBotMessageSource().parse_group_message(
        {
            "post_type": "message",
            "message_type": "group",
            "self_id": self_id,
            "group_id": group,
            "user_id": "3003",
            "message_id": msg_id,
            "message": [{"type": "text", "data": {"text": "synthetic"}}],
        }
    )


def _normalize_action_times(path):
    stamp = INSIDE.replace(tzinfo=None).isoformat(sep=" ")
    with sqlite3.connect(path) as con:
        con.execute("UPDATE action_logs SET created_at=?", (stamp,))
        con.execute("UPDATE action_intents SET created_at=?", (stamp,))


def test_real_pre_send_skip_is_not_an_external_boundary_violation(db):
    path, _ = db

    class NeverCalled:
        calls = 0

        async def recall(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("No external send is allowed")

    client = NeverCalled()

    async def exercise():
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                msg = parsed("10000001", "701", "2002")
                intent = await _create_intent(
                    session, msg, "recall", {}, "synthetic", provider="onebot"
                )
                result = await _execute_intent(session, client, intent)
                assert result is not None and result.attempts == 0
                assert intent.status == "SKIPPED" and client.calls == 0
                await _log_action_result(session, intent, result)
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    _normalize_action_times(path)
    data = collect(path)
    assert data["action_intents_by_status"] == [("SKIPPED", 1)]
    assert data["boundary_check"]["action_targets_outside_authorized"] == [], (
        "A real pre-send SKIPPED audit row is incorrectly counted as an external action target"
    )


@pytest.mark.parametrize("other_provider", ["onebot", "qq_official"])
def test_other_account_or_provider_actions_do_not_enter_current_account_numerator(
    db, other_provider
):
    path, engine = db
    with Session(engine) as session:
        decision(session, "onebot:10000001:701")
        decision(
            session,
            "onebot:90000009:702" if other_provider == "onebot" else "official:702",
            provider=other_provider,
        )
        session.commit()

    async def exercise():
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                for self_id, msg_id, provider in [
                    ("10000001", "701", "onebot"),
                    ("90000009", "702", other_provider),
                ]:
                    msg = parsed(self_id, msg_id)
                    intent = await _create_intent(
                        session, msg, "recall", {}, "synthetic", provider=provider
                    )
                    # Audit seam only: no call to any action client.
                    intent.status = "SUCCEEDED"
                    await session.commit()
                    await _log_action_result(
                        session, intent, ActionResult(action="recall", ok=True, attempts=1)
                    )
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    _normalize_action_times(path)
    data = collect(path)
    assert data["decisions_by_verdict"] == [("violation_high", 1)]
    assert data["other_account_decisions"] == 1
    assert data["action_logs_by_action_ok"] == [("recall", 1, None, 1, 1)], (
        "The current-account denominator excludes the other account/provider, but its action is still in the numerator"
    )
    assert data["action_intents_by_status"] == [("SUCCEEDED", 1)]


def test_diagnostic_raw_action_message_identity_has_no_self_id():
    left = parsed("10000001", "701")
    right = parsed("90000009", "701")
    assert left.message_id == right.message_id == "701"
    assert left.provider == right.provider == "onebot"
    assert left.external_group_id == right.external_group_id == "1001"
    assert left.external_message_id == right.external_message_id == "701"
    # Prefix-filtering action_logs.message_id therefore cannot simply copy
    # the correct ShadowDecision prefix filter. Ambiguous historical rows
    # need explicit un-attributable status or an independently proven join.


def test_diagnostic_boundary_is_export_time_not_event_time_authorization(db):
    path, engine = db
    with Session(engine) as session:
        decision(session, "onebot:10000001:701", group="2002")
        action(session, "701", group="2002", attempts=1)
        session.commit()
    before = collect(path)
    assert before["boundary_check"]["action_targets_outside_authorized"] == ["onebot:2002"]
    # Only the CURRENT setting changes. No event/action evidence is changed.
    with sqlite3.connect(path) as con:
        con.execute(
            "UPDATE provider_group_settings SET action_enabled=1 WHERE provider='onebot' AND external_group_id='2002'"
        )
    after = collect(path)
    assert after["action_logs_by_action_ok"] == before["action_logs_by_action_ok"]
    assert after["boundary_check"]["action_targets_outside_authorized"] == []
    # This diagnostic proves the report cannot establish event-time
    # authorization without a historical snapshot/audit-chain binding.
