"""R-105 durable inbox contracts; no real QQ or remote AI calls."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from app.db import SessionLocal


def _event(mid: int) -> dict:
    return {
        "self_id": 10000001,
        "message_id": mid,
        "group_id": 10000002,
        "user_id": 10000003,
        "post_type": "message",
        "message_type": "group",
        "message": [{"type": "text", "data": {"text": "test"}}],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("group_id", {}), ("user_id", True), ("message", 42), ("message_id", "bad:identity")],
)
async def test_invalid_structure_is_not_persisted(field, value) -> None:
    from app.runtime.inbox import enqueue_event

    event = _event(98001 + ["group_id", "user_id", "message", "message_id"].index(field))
    event[field] = value
    async with SessionLocal() as session:
        with pytest.raises(ValueError):
            await enqueue_event(session, event, max_pending=100)


@pytest.mark.asyncio
async def test_persisted_event_is_recoverable_before_worker_starts() -> None:
    from app.runtime.inbox import InboxEvent, claim_event, due_keys, enqueue_event, finish_event

    async with SessionLocal() as session:
        key, created = await enqueue_event(session, _event(9001), max_pending=100)
        assert created
    async with SessionLocal() as restarted:
        assert key in await due_keys(restarted)
        claim = await claim_event(restarted, key)
        assert claim is not None and claim.payload["message_id"] == 9001
        await finish_event(restarted, claim)
    async with SessionLocal() as session:
        row = await session.get(InboxEvent, key)
        assert row.status == "DONE" and row.payload_json == ""
        _, created = await enqueue_event(session, _event(9001), max_pending=100)
        assert not created


@pytest.mark.asyncio
async def test_concurrent_claim_has_one_winner_and_expired_lease_can_recover() -> None:
    from app.runtime.inbox import claim_event, enqueue_event, retry_event

    async with SessionLocal() as session:
        key, _ = await enqueue_event(session, _event(9002), max_pending=100)
    now = datetime.now(UTC)

    async def claim_once():
        async with SessionLocal() as session:
            return await claim_event(session, key, now=now)

    results = await asyncio.gather(claim_once(), claim_once())
    assert sum(c is not None for c in results) == 1
    first = next(c for c in results if c is not None)
    async with SessionLocal() as session:
        replacement = await claim_event(session, key, now=now + timedelta(seconds=301))
        assert replacement is not None
        assert not await retry_event(session, first, "stale", now=now)


@pytest.mark.asyncio
async def test_retry_is_bounded_and_not_due_immediately() -> None:
    from app.runtime.inbox import InboxEvent, claim_event, due_keys, enqueue_event, retry_event

    async with SessionLocal() as session:
        key, _ = await enqueue_event(session, _event(9003), max_pending=100)
        now = datetime.now(UTC)
        for attempt in range(3):
            current = now + timedelta(minutes=attempt)
            claim = await claim_event(session, key, now=current)
            assert claim is not None
            assert await retry_event(session, claim, "temporary", now=current)
            assert key not in await due_keys(session, now=current)
        row = await session.get(InboxEvent, key, populate_existing=True)
        assert row.status == "DEAD" and row.attempts == 3


@pytest.mark.asyncio
async def test_inbox_rejects_duplicate_payload_changes_and_capacity_overflow() -> None:
    from app.runtime.inbox import InboxEvent, InboxFull, enqueue_event
    from sqlalchemy import delete

    async with SessionLocal() as session:
        await session.execute(delete(InboxEvent))
        await session.commit()
        await enqueue_event(session, _event(9004), max_pending=1)
        with pytest.raises(InboxFull):
            await enqueue_event(session, _event(9005), max_pending=1)
        changed = _event(9004)
        changed["message"] = []
        with pytest.raises(ValueError, match="payload"):
            await enqueue_event(session, changed, max_pending=1)


@pytest.mark.asyncio
async def test_inbox_raw_payload_expires_without_replay() -> None:
    from app.runtime.inbox import InboxEvent, due_keys, enqueue_event, purge_inbox

    async with SessionLocal() as session:
        key, _ = await enqueue_event(session, _event(9006), max_pending=100)
        future = datetime.now(UTC) + timedelta(days=31)
        await purge_inbox(session, now=future)
        row = await session.get(InboxEvent, key, populate_existing=True)
        assert row.payload_json == "" and row.status == "DEAD"
        assert key not in await due_keys(session, now=future)


def test_runtime_lock_has_one_owner_and_is_released_on_close(tmp_path) -> None:
    from app.runtime.inbox import acquire_runtime_lock

    url = f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}"
    first = acquire_runtime_lock(url)
    try:
        with pytest.raises(RuntimeError, match="already owned"):
            acquire_runtime_lock(url)
    finally:
        first.close()
    acquire_runtime_lock(url).close()


@pytest.mark.asyncio
async def test_exclusive_restart_marks_executing_onebot_intent_unknown() -> None:
    from app.actions.orchestrator import ActionIntent
    from app.runtime.inbox import recover_abandoned_actions

    async with SessionLocal() as session:
        intent = ActionIntent(
            idempotency_key="inbox-abandoned",
            action="recall",
            status="EXECUTING",
            provider="onebot",
            group_openid="10000002",
        )
        official = ActionIntent(
            idempotency_key="inbox-other-provider",
            action="recall",
            status="EXECUTING",
            provider="qq_official",
            group_openid="10000002",
        )
        session.add_all([intent, official])
        await session.commit()
        await recover_abandoned_actions(session)
        await session.refresh(intent)
        await session.refresh(official)
        assert intent.status == "UNKNOWN"
        assert official.status == "EXECUTING"
