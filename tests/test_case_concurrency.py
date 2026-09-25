"""Distinct messages must receive one serialized strike sequence on SQLite."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path

import pytest
from app.cases import service
from app.cases.models import Case, ViolationRecord
from app.core.contracts import Sender, StandardMessage
from app.db import Base
from app.moderation.decision import ModerationDecision
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


def _message(message_id: str) -> StandardMessage:
    return StandardMessage(
        message_id=message_id,
        provider="onebot",
        external_group_id="42",
        external_user_id="84",
        sender=Sender(member_openid="84"),
        text="脱敏违规测试",
    )


def _decision(message_id: str) -> ModerationDecision:
    return ModerationDecision(
        message_id=message_id,
        provider="onebot",
        external_group_id="42",
        external_user_id="84",
        verdict="violation_high",
        category="ad",
        confidence=0.99,
    )


async def test_two_sessions_allocate_first_and_second_strikes_without_lost_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'strike-race.db'}")
    original_count = service.count_active_violations
    second_counted = asyncio.Event()
    count_calls = 0

    async def overlapping_count(*args, **kwargs):
        nonlocal count_calls
        count = await original_count(*args, **kwargs)
        count_calls += 1
        if count_calls == 1:
            # Expose the old read/read/write/write race. Under serialization the
            # second reader cannot enter until the first commits, so do not deadlock.
            with suppress(TimeoutError):
                await asyncio.wait_for(second_counted.wait(), timeout=0.2)
        else:
            second_counted.set()
        return count

    monkeypatch.setattr(service, "count_active_violations", overlapping_count)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("PRAGMA journal_mode=WAL"))
            await connection.run_sync(Base.metadata.create_all)
        async with (
            AsyncSession(engine, expire_on_commit=False) as first,
            AsyncSession(engine, expire_on_commit=False) as second,
        ):
            outcomes = await asyncio.gather(
                service.record_violation(first, _message("101"), _decision("101")),
                service.record_violation(second, _message("102"), _decision("102")),
            )
        assert sorted(outcome.strike_no for outcome in outcomes) == [1, 2]
        by_strike = {outcome.strike_no: outcome for outcome in outcomes}
        assert [action.action for action in by_strike[1].planned_actions] == ["recall"]
        assert [action.action for action in by_strike[2].planned_actions] == ["recall"]
        async with AsyncSession(engine) as session:
            violations = (await session.scalars(select(ViolationRecord))).all()
            cases = (await session.scalars(select(Case))).all()
            assert {violation.message_id for violation in violations} == {"101", "102"}
            assert len(violations) == 2
            assert cases == []
    finally:
        await engine.dispose()


async def test_legacy_case_number_does_not_trigger_new_case(tmp_path: Path, monkeypatch) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'case-number-retry.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            session.add(Case(case_no="collision", group_openid="other", member_openid="other"))
            await session.commit()
            await service.record_violation(session, _message("101"), _decision("101"))

            async def case_number_must_not_be_generated(*args, **kwargs):
                raise AssertionError("automatic case creation is disabled")

            monkeypatch.setattr(
                service, "_generate_case_no", case_number_must_not_be_generated, raising=False
            )

            outcome = await service.record_violation(session, _message("102"), _decision("102"))

            violations = (await session.scalars(select(ViolationRecord))).all()
            assert {violation.message_id for violation in violations} == {"101", "102"}
            assert outcome.case is None
            cases = (await session.scalars(select(Case))).all()
            assert len(cases) == 1 and cases[0].case_no == "collision"
    finally:
        await engine.dispose()
