# ruff: noqa: E402, I001, F401, F811
# Reviewer round-6 probe pack (85b0c0b), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
# ruff: noqa: I001 -- This probe is delivered outside the repository package root.
"""Independent r132 window-export probes; synthetic ORM tables only.

Run from reviewed checkout with PYTHONPATH=.:tests and pytest -p conftest.
No production environment/database is read; script ROOT points to tmp_path.
Failing assertions encode the evidence-export requirements, not runtime bugs.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.actions.orchestrator import ActionIntent
from app.models import ActionLog, ProviderGroupSettings, SystemSetting
from app.runtime.models import ShadowDecision
from scripts import window_stats as stats


START = datetime(2026, 9, 18, 17, 0, 0, tzinfo=UTC)
END = datetime(2026, 9, 18, 18, 0, 0, tzinfo=UTC)
INSIDE = datetime(2026, 9, 18, 17, 30, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("ONEBOT_SELF_ID=10000001\n", encoding="utf-8")
    path = tmp_path / "synthetic.db"
    engine = create_engine(f"sqlite:///{path}")
    for model in (ProviderGroupSettings, ShadowDecision, ActionIntent, ActionLog, SystemSetting):
        model.__table__.create(engine)
    with Session(engine) as session:
        session.add(
            ProviderGroupSettings(provider="onebot", external_group_id="1001", action_enabled=True)
        )
        session.add(
            ProviderGroupSettings(provider="onebot", external_group_id="2002", action_enabled=False)
        )
        session.commit()
    yield path, engine
    engine.dispose()


def decision(
    session, key, group="1001", created=INSIDE, provider="onebot", verdict="violation_high"
):
    session.add(
        ShadowDecision(
            message_id=key,
            external_message_id=key.rsplit(":", 1)[-1],
            group_openid=group,
            member_openid="3003",
            provider=provider,
            external_group_id=group,
            external_user_id="3003",
            kind="text",
            verdict=verdict,
            created_at=created,
        )
    )


def action(session, key, group="1001", created=INSIDE, provider="onebot", attempts=1, ok=True):
    session.add(
        ActionLog(
            action="recall",
            group_openid=group,
            message_id=key,
            provider=provider,
            external_group_id=group,
            external_user_id="3003",
            external_message_id=key.rsplit(":", 1)[-1],
            ok=ok,
            attempts=attempts,
            created_at=created,
        )
    )


def collect(path):
    return stats.collect(db=path, start=START, end=END)


def test_half_open_utc_boundaries_are_correct(db):
    path, engine = db
    with Session(engine) as session:
        decision(session, "onebot:10000001:start", created=START)
        decision(session, "onebot:10000001:end", created=END)
        decision(session, "onebot:10000001:inside")
        session.commit()
    assert collect(path)["decisions_by_verdict"] == [("violation_high", 2)]


def test_export_does_not_write_database(db):
    path, _ = db
    before = path.read_bytes()
    collect(path)
    assert path.read_bytes() == before


def test_boundary_result_must_detect_action_target_switch(db):
    path, engine = db
    with Session(engine) as session:
        decision(session, "onebot:10000001:1")
        decision(session, "onebot:10000001:2", group="2002")
        action(session, "onebot:10000001:1")
        session.commit()
    allowed_report = collect(path)
    with sqlite3.connect(path) as con:
        con.execute(
            "UPDATE action_logs SET external_group_id='2002', group_openid='2002', message_id='onebot:10000001:2', external_message_id='2'"
        )
    unauthorized_report = collect(path)
    assert allowed_report != unauthorized_report, (
        "All exported fields stay identical after an external recall moves to an unauthorized group"
    )


def test_other_provider_cannot_supply_onebot_authorization(db):
    path, engine = db
    with Session(engine) as session:
        session.add(
            ProviderGroupSettings(
                provider="qq_official", external_group_id="2002", action_enabled=True
            )
        )
        decision(session, "onebot:10000001:2", group="2002")
        session.commit()
    assert "2002" in collect(path)["boundary_check"]["groups_seen_outside_authorized"]


def test_account_label_must_not_include_other_account_decisions(db):
    path, engine = db
    with Session(engine) as session:
        decision(session, "onebot:10000001:1")
        decision(session, "onebot:90000009:2")
        session.commit()
    report = collect(path)
    assert report["account"] == ["10000001"]
    assert report["decisions_by_verdict"] == [("violation_high", 1)], (
        "Report is labeled one account but counts both accounts"
    )


def test_real_pre_send_skip_must_not_be_request_failure(db):
    path, _ = db

    async def exercise():
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        from app.actions.orchestrator import _execute_intent, _log_action_result

        class NeverCalled:
            async def recall(self, *args, **kwargs):
                raise AssertionError("No external send allowed in this probe")

        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                intent = ActionIntent(
                    idempotency_key="test-only-skip",
                    action="recall",
                    status="PENDING",
                    group_openid="2002",
                    message_id="1",
                    provider="onebot",
                    external_group_id="2002",
                    external_user_id="3003",
                    external_message_id="1",
                    created_at=INSIDE,
                )
                session.add(intent)
                await session.commit()
                result = await _execute_intent(session, NeverCalled(), intent)
                assert result is not None and result.attempts == 0 and intent.status == "SKIPPED"
                await _log_action_result(session, intent, result)
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    with sqlite3.connect(path) as con:
        con.execute(
            "UPDATE action_logs SET created_at=?", (INSIDE.replace(tzinfo=None).isoformat(sep=" "),)
        )
    report = collect(path)
    assert report["action_intents_by_status"] == [("SKIPPED", 1)]
    rendered = stats.render(report, deployment="test-only", prompt_version="test-only", db=path)
    assert "| recall | 失败 |" not in rendered, (
        "Real SKIPPED/attempts=0 was classified as a failed external request"
    )
