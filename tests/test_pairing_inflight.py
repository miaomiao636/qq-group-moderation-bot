"""R-108 in-flight image ordering, real OneBot parser/inbox, no external calls."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.adapters.onebot.parser import OneBotMessageSource
from app.core.contracts import StandardMessage
from app.db import SessionLocal
from app.moderation.ai import AIModerationResult
from app.moderation.decision import ModerationDecision
from app.moderation.image_engine import MediaAnalysis
from app.runtime import pipeline
from app.runtime.inbox import InboxEvent, enqueue_event
from app.runtime.models import ShadowDecision
from app.runtime.onebot_wiring import dedup_key_for

PROMO = "合成校园兼职测试文案，详情联系测试管理员，不含真实联系方式"


def _synthetic_id() -> int:
    return 1_000_000_000_000 + int(uuid.uuid4().hex[:10], 16)


def _event(*, group: int, user: int, when: datetime, kind: str) -> dict:
    message = (
        [
            {
                "type": "image",
                "data": {"file": "synthetic.png", "url": "https://example.invalid/image.png"},
            }
        ]
        if kind == "image"
        else [{"type": "text", "data": {"text": PROMO}}]
    )
    return {
        "self_id": 10000001,
        "post_type": "message",
        "message_type": "group",
        "message_id": _synthetic_id(),
        "group_id": group,
        "user_id": user,
        "time": int(when.timestamp()),
        "message": message,
        "sender": {"user_id": user, "role": "member", "nickname": "synthetic-user"},
    }


class _FakeAI:
    async def review_message(self, session, msg: StandardMessage, decision, **kwargs):
        high = ModerationDecision(
            message_id=msg.message_id,
            provider=msg.provider,
            external_group_id=msg.external_group_id,
            external_user_id=msg.external_user_id,
            verdict="violation_high",
            category="ad",
            confidence=0.99,
            reason="synthetic confirmed ad",
            recommended_actions=["recall", "mute", "warn"],
        )
        if msg.kind == "image":
            return high.model_copy(
                update={"verdict": "allow", "category": None, "recommended_actions": []}
            ), [
                AIModerationResult(
                    category=None,
                    confidence=1.0,
                    needs_review=False,
                    source="vision",
                    model_id="synthetic-vision",
                    evidence="校园墙白名单|文案:" + PROMO,
                )
            ]
        return high, []


class _FakeImageEngine:
    def analyze(self, path):
        return MediaAnalysis("allow", 1.0, reason="synthetic image engine")


async def _run(event: dict, *, prepare_payload=None):
    async with SessionLocal() as session:
        return await pipeline.run_pipeline(
            event,
            session,
            message_source=OneBotMessageSource(),
            ai_service=_FakeAI(),
            image_engine=_FakeImageEngine(),
            dedup_key=dedup_key_for(event, str(event["message_id"])),
            prepare_payload=prepare_payload,
        )


async def test_preceding_image_download_in_progress_prevents_premature_text_penalty(
    monkeypatch, tmp_path
) -> None:
    """A completed image verdict does not exist while prepare_payload is downloading."""
    now = datetime.now(UTC)
    group, user = _synthetic_id(), _synthetic_id()
    image_event = _event(group=group, user=user, when=now - timedelta(seconds=2), kind="image")
    text_event = _event(group=group, user=user, when=now, kind="text")
    entered, release = asyncio.Event(), asyncio.Event()
    observed = {}
    image_file = tmp_path / "synthetic.png"
    image_file.write_bytes(b"synthetic data; the fake image engine never decodes it")
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)

    async def prepare(payload):
        entered.set()
        await release.wait()
        payload["_downloaded"] = [image_file.name]

    async def no_external_actions(session, msg, decision, **kwargs):
        observed[msg.message_id] = decision
        return []

    monkeypatch.setattr(pipeline, "orchestrate_actions", no_external_actions)
    image_task = asyncio.create_task(_run(image_event, prepare_payload=prepare))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        text_result = await asyncio.wait_for(_run(text_event), timeout=5)
    finally:
        release.set()
        await asyncio.wait_for(image_task, timeout=5)
    assert text_result is not None
    assert text_result.verdict == "record_only"
    assert observed[str(text_event["message_id"])].recommended_actions == []
    assert "未完成" in text_result.reason


@pytest.mark.parametrize("scope", ["same-member", "other-member", "other-group", "other-account"])
async def test_queued_image_without_worker_registration_is_accounted_for(
    scope, monkeypatch
) -> None:
    """The public inbox admission function commits the actual raw event before workers run."""
    now = datetime.now(UTC)
    group, user = _synthetic_id(), _synthetic_id()
    image_event = _event(
        group=_synthetic_id() if scope == "other-group" else group,
        user=_synthetic_id() if scope == "other-member" else user,
        when=now - timedelta(seconds=2),
        kind="image",
    )
    text_event = _event(group=group, user=user, when=now, kind="text")
    if scope == "other-account":
        image_event["self_id"] = 10000009
    observed = {}

    async def no_external_actions(session, msg, decision, **kwargs):
        observed[msg.message_id] = decision
        return []

    monkeypatch.setattr(pipeline, "orchestrate_actions", no_external_actions)
    async with SessionLocal() as session:
        key, created = await enqueue_event(session, image_event, max_pending=100_000)
        assert created
    # Intentionally do not call claim_event/run_pipeline for the image: no worker has seen it.
    async with SessionLocal() as session:
        row = await session.get(InboxEvent, key)
        assert row is not None and row.status == "PENDING" and row.payload_json
    result = await _run(text_event)
    assert result is not None
    if scope == "same-member":
        assert result.verdict == "record_only"
        assert observed[str(text_event["message_id"])].recommended_actions == []
    else:
        assert result.verdict == "violation_high"


async def test_queued_intervening_text_does_not_break_wall_window(monkeypatch) -> None:
    """口径 C（2026-09-17）：中间排队的文字不再阻断豁免窗口（紧邻要求已取消）。"""
    now = datetime.now(UTC)
    group, user = _synthetic_id(), _synthetic_id()
    old_image = _event(group=group, user=user, when=now - timedelta(seconds=4), kind="image")
    middle_text = _event(group=group, user=user, when=now - timedelta(seconds=2), kind="text")
    target_text = _event(group=group, user=user, when=now, kind="text")
    parsed_image = OneBotMessageSource().parse_group_message(old_image)
    normal = AIModerationResult(
        category=None,
        confidence=1.0,
        needs_review=False,
        source="vision",
        model_id="synthetic-vision",
        evidence="校园墙白名单|文案:" + PROMO,
    )
    async with SessionLocal() as session:
        session.add(
            ShadowDecision(
                message_id=dedup_key_for(old_image, str(old_image["message_id"])),
                external_message_id=str(old_image["message_id"]),
                provider="onebot",
                external_group_id=str(group),
                external_user_id=str(user),
                group_openid=str(group),
                member_openid=str(user),
                kind="image",
                verdict="allow",
                detail_json=json.dumps(
                    {
                        "sent_at": parsed_image.sent_at.isoformat(),
                        "ai_results": [normal.model_dump()],
                    }
                ),
            )
        )
        await session.commit()
        await enqueue_event(session, middle_text, max_pending=100_000)

    async def no_external_actions(*args, **kwargs):
        return []

    monkeypatch.setattr(pipeline, "orchestrate_actions", no_external_actions)
    result = await _run(target_text)
    assert result is not None
    assert result.verdict == "record_only"
    assert "豁免" in (result.reason or "")


async def test_loader_requires_exact_event_account_and_returns_metadata_only() -> None:
    from app.runtime.pairing_context import load_pending_pairing_messages

    now = datetime.now(UTC)
    group, user = _synthetic_id(), _synthetic_id()
    image_event = _event(group=group, user=user, when=now - timedelta(seconds=2), kind="image")
    text_event = _event(group=group, user=user, when=now, kind="text")
    text_msg = OneBotMessageSource().parse_group_message(text_event)
    async with SessionLocal() as session:
        await enqueue_event(session, image_event, max_pending=100_000)
        assert await load_pending_pairing_messages(session, text_msg, event_key="unknown") == ()
        assert (
            await load_pending_pairing_messages(
                session, text_msg, event_key="onebot:10000001:different-message"
            )
            == ()
        )
        pending = await load_pending_pairing_messages(
            session, text_msg, event_key=dedup_key_for(text_event, str(text_event["message_id"]))
        )
    assert len(pending) == 1
    assert pending[0].kind == "image" and pending[0].sent_at is not None
    assert pending[0].text == "" and pending[0].attachments == [] and pending[0].segments == []
    assert pending[0].sender.username == ""
