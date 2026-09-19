# ruff: noqa: E402, I001, F401, F811, SIM105
# Reviewer round-8 probe pack (b7d7e78), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
"""Round-7 independent stats probes; synthetic DB and real audit seams only.

No client/action/model/network calls. Run with PYTHONPATH=.:tests pytest
-c pyproject.toml -p conftest from the fixed reviewed checkout.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session

from app.actions.orchestrator import _create_intent, _log_action_result
from app.core.contracts import ActionResult
from scripts import window_stats as stats
from tests.test_r132_review_window_scope_followup import parsed, _normalize_action_times
from tests.test_r132_review_window_stats import INSIDE, START, END, action, collect, db, decision


def _write_audit(path, *, message_id="701", group="1001"):
    async def exercise():
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                msg = parsed("10000001", message_id, group)
                intent = await _create_intent(session, msg, "recall", {}, "synthetic", provider="onebot")
                intent.status = "SUCCEEDED"
                await session.commit()
                await _log_action_result(session, intent, ActionResult(action="recall", ok=True, attempts=1))
        finally:
            await engine.dispose()
    asyncio.run(exercise())
    _normalize_action_times(path)


@pytest.mark.parametrize("other_group,has_own_decision", [
    ("1001", True),  # Same raw id and group; both accounts are candidates.
    ("2002", True),  # Another group's message is not evidence about this action.
    ("2002", False), # Own decision pruned/absent; keep the un-attributable row.
])
def test_raw_id_collision_does_not_prove_action_belongs_to_other_account(db, other_group, has_own_decision):
    path, engine = db
    with Session(engine) as session:
        if has_own_decision:
            decision(session, "onebot:10000001:701", group="1001")
        decision(session, "onebot:90000009:701", group=other_group)
        session.commit()
    _write_audit(path)
    data = collect(path)
    assert data["action_logs_by_action_ok"] == [("recall", 1, None, 1, 1)], (
        "An unrelated or ambiguous same-raw-ID decision must not delete a real audit row"
    )
    assert data["action_intents_by_status"] == [("SUCCEEDED", 1)]
    if other_group == "1001" or not has_own_decision:
        assert data["action_unattributed_rows"] == 1, (
            "Ambiguous/no matching group-scoped decision must remain explicitly un-attributable"
        )


def test_provider_collision_is_not_used_as_an_account_attribution(db):
    path, engine = db
    with Session(engine) as session:
        decision(session, "official:701", provider="qq_official", group="1001")
        session.commit()
    _write_audit(path)
    data = collect(path)
    assert data["action_logs_by_action_ok"] == [("recall", 1, None, 1, 1)]
    assert data["action_intents_by_status"] == [("SUCCEEDED", 1)]
    assert data["action_unattributed_rows"] == 1


@pytest.mark.parametrize("ok,code", [(True, None), (False, None), (False, 1200)])
def test_positive_attempt_targets_include_success_failure_and_timeout(db, ok, code):
    path, engine = db
    with Session(engine) as session:
        action(session, "701", group="2002", attempts=1, ok=ok)
        session.commit()
    with sqlite3.connect(path) as con:
        con.execute("UPDATE action_logs SET err_code=?", (code,))
    data = collect(path)
    assert data["boundary_check"]["action_targets_outside_authorized"] == ["onebot:2002"]
    assert data["action_not_sent_targets"] == []
    assert data["action_unknown_attempt_targets"] == []
    expected = "成功" if ok else ("超时（最终效果未知）" if code == 1200 else "失败（最终效果未知）")
    assert stats._status_label(ok, code, 1) == expected


def test_null_attempts_remains_unknown_in_both_results_and_target_sections(tmp_path, monkeypatch):
    # Minimal legacy/invalid-shape fixture, deliberately NULL to test the newly
    # documented contract. Production ORM's current NOT NULL is not modified.
    monkeypatch.setattr(stats, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("ONEBOT_SELF_ID=10000001\n", encoding="utf-8")
    path = tmp_path / "legacy-null-attempts.db"
    with sqlite3.connect(path) as con:
        con.executescript("""
          CREATE TABLE provider_group_settings(provider TEXT, external_group_id TEXT, action_enabled INTEGER);
          CREATE TABLE shadow_decisions(provider TEXT, external_group_id TEXT, external_message_id TEXT, message_id TEXT, kind TEXT, verdict TEXT, created_at TEXT);
          CREATE TABLE action_intents(status TEXT, provider TEXT, message_id TEXT, created_at TEXT);
          CREATE TABLE action_logs(action TEXT, ok INTEGER, err_code INTEGER, attempts INTEGER, provider TEXT, external_group_id TEXT, message_id TEXT, created_at TEXT);
        """)
        con.execute("INSERT INTO action_logs VALUES ('recall', 0, NULL, NULL, 'onebot', '2002', '701', ?)", (INSIDE.replace(tzinfo=None).isoformat(sep=" "),))
    data = stats.collect(db=path, start=START, end=END)
    assert data["boundary_check"]["action_targets_outside_authorized"] == []
    assert data["action_not_sent_targets"] == []
    assert data["action_unknown_attempt_targets"] == [("onebot", "2002", 1)]
    rendered = stats.render(data, deployment="synthetic", prompt_version="synthetic", db=path)
    assert "| recall | 未发送" not in rendered, (
        "NULL attempts cannot be labelled not-sent in results while the target section correctly labels it unknown"
    )
    assert "无法判定" in stats._status_label(False, None, None)


def test_unattributed_actions_are_not_named_current_account_numerator(db):
    path, _ = db
    _write_audit(path)
    data = collect(path)
    assert data["action_unattributed_rows"] == 1
    rendered = stats.render(data, deployment="synthetic", prompt_version="synthetic", db=path)
    assert "## 动作结果（分子）" not in rendered, (
        "This retained/un-attributable pool is not a numerator for the selected account's decision denominator"
    )
