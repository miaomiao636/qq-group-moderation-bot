"""R-108 worker-lifetime ordering context and event identity contracts."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.core.contracts import Sender, StandardMessage
from app.db import SessionLocal
from app.moderation.decision import ModerationDecision
from app.runtime import pipeline
from app.runtime.models import ShadowDecision
from app.runtime.pairing_context import load_pending_pairing_messages


def _message(group: str, *, kind: str = "image", mid: str | None = None) -> StandardMessage:
    return StandardMessage(
        message_id=mid or str(1_000_000_000_000 + int(uuid.uuid4().hex[:10], 16)),
        provider="onebot",
        external_group_id=group,
        external_user_id="9000000000001",
        sender=Sender(),
        kind=kind,
        sent_at=datetime.now(UTC) - timedelta(seconds=2 if kind == "image" else 0),
        text="另一条完全不同的合成推广消息" if kind == "text" else "",
    )


class _Source:
    provider = "onebot"

    def __init__(self, msg):
        self.msg = msg

    def parse_group_message(self, payload):
        return self.msg


async def _run(msg, *, account="10000001", prepare_payload=None, ai_service=None):
    async with SessionLocal() as session:
        return await pipeline.run_pipeline(
            {"message_id": msg.message_id},
            session,
            message_source=_Source(msg),
            prepare_payload=prepare_payload,
            ai_service=ai_service,
            dedup_key=f"onebot:{account}:{msg.message_id}",
        )


async def _pending(text_msg):
    async with SessionLocal() as session:
        return await load_pending_pairing_messages(
            session, text_msg, event_key=f"onebot:10000001:{text_msg.message_id}"
        )


async def test_cancelled_worker_discards_its_active_context() -> None:
    group = "cancel-" + uuid.uuid4().hex
    image_msg, text_msg = _message(group), _message(group, kind="text")
    entered, release = asyncio.Event(), asyncio.Event()

    async def prepare(payload):
        entered.set()
        await release.wait()

    task = asyncio.create_task(_run(image_msg, prepare_payload=prepare))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert len(await _pending(text_msg)) == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert await _pending(text_msg) == ()


async def test_rejected_duplicate_does_not_discard_first_workers_context() -> None:
    group = "duplicate-" + uuid.uuid4().hex
    image_msg, text_msg = _message(group), _message(group, kind="text")
    entered, release = asyncio.Event(), asyncio.Event()

    async def prepare(payload):
        entered.set()
        await release.wait()

    task = asyncio.create_task(_run(image_msg, prepare_payload=prepare))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert await _run(image_msg) is None
        assert len(await _pending(text_msg)) == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert await _pending(text_msg) == ()


async def test_failed_preparation_discards_active_context() -> None:
    group = "failed-" + uuid.uuid4().hex
    image_msg, text_msg = _message(group), _message(group, kind="text")

    async def prepare(payload):
        raise OSError("synthetic download failure")

    assert await _run(image_msg, prepare_payload=prepare) is None
    assert await _pending(text_msg) == ()


class _HighAI:
    async def review_message(self, session, msg, decision, **kwargs):
        return ModerationDecision(
            message_id=msg.message_id,
            provider=msg.provider,
            external_group_id=msg.external_group_id,
            external_user_id=msg.external_user_id,
            verdict="violation_high",
            category="ad",
            confidence=0.99,
            reason="synthetic high ad",
            recommended_actions=["recall", "mute", "warn"],
        ), []


async def test_old_binding_from_other_bot_account_does_not_exempt_new_event(monkeypatch) -> None:
    """External message IDs are account-scoped, whereas durable event keys include self_id."""
    group = "account-" + uuid.uuid4().hex
    text_msg = _message(group, kind="text")
    image_id = str(1_000_000_000_000 + int(uuid.uuid4().hex[:10], 16))
    async with SessionLocal() as session:
        session.add(
            ShadowDecision(
                message_id="onebot:10000009:" + image_id,
                external_message_id=image_id,
                provider="onebot",
                external_group_id=group,
                external_user_id=text_msg.external_user_id,
                group_openid=group,
                member_openid=text_msg.external_user_id,
                kind="image",
                verdict="allow",
                detail_json=json.dumps(
                    {
                        "wall_paired": True,
                        "wall_paired_message_id": text_msg.message_id,
                        "sent_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                    }
                ),
            )
        )
        await session.commit()

    async def no_external_actions(*args, **kwargs):
        return []

    monkeypatch.setattr(pipeline, "orchestrate_actions", no_external_actions)
    result = await _run(text_msg, ai_service=_HighAI())
    assert result is not None
    assert result.verdict == "violation_high"


async def test_old_missing_timestamp_cannot_bypass_pending_image_safety(monkeypatch) -> None:
    """An old-format row cannot turn unknown ordering back into automatic punishment."""
    group = "legacy-" + uuid.uuid4().hex
    image_msg, text_msg = _message(group), _message(group, kind="text")
    old_id = str(1_000_000_000_000 + int(uuid.uuid4().hex[:10], 16))
    async with SessionLocal() as session:
        session.add(
            ShadowDecision(
                message_id="onebot:10000001:" + old_id,
                external_message_id=old_id,
                provider="onebot",
                external_group_id=group,
                external_user_id=image_msg.external_user_id,
                group_openid=group,
                member_openid=image_msg.external_user_id,
                kind="text",
                verdict="allow",
                detail_json="{}",
                created_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        await session.commit()
    entered, release = asyncio.Event(), asyncio.Event()

    async def prepare(payload):
        entered.set()
        await release.wait()

    async def no_external_actions(*args, **kwargs):
        return []

    monkeypatch.setattr(pipeline, "orchestrate_actions", no_external_actions)
    task = asyncio.create_task(_run(image_msg, prepare_payload=prepare))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        result = await _run(text_msg, ai_service=_HighAI())
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert result is not None
    assert result.verdict == "record_only"
