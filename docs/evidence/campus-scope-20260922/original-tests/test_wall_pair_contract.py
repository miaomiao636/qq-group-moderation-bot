"""R-108: wall window exemption uses the real pipeline schema; 口径 C（2026-09-17）.

口径 C：图后 2 分钟内同成员任意广告文字豁免（无相似度/紧邻/一图一条）。
All content is synthetic; the shared pytest fixture supplies an isolated SQLite DB.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from app.core.contracts import Sender, StandardMessage
from app.db import SessionLocal
from app.moderation.ai import AIModerationResult
from app.moderation.decision import ModerationDecision
from app.moderation.wall_pair import _wall_source_from_detail, maybe_wall_text_pairing
from app.runtime import pipeline
from app.runtime.models import ShadowDecision

PROMO = "测试用校园兼职推广文案，详情联系测试管理员，不含真实联系方式"


def _message(group: str, sent_at: datetime) -> StandardMessage:
    return StandardMessage(
        message_id="text-" + uuid.uuid4().hex,
        provider="onebot",
        external_group_id=group,
        external_user_id="synthetic-user",
        sender=Sender(username="synthetic", role="member"),
        kind="text",
        text=PROMO,
        sent_at=sent_at,
    )


def _high(msg: StandardMessage) -> ModerationDecision:
    return ModerationDecision(
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


def _vision(**updates: object) -> AIModerationResult:
    result = AIModerationResult(
        category=None,
        confidence=1.0,
        needs_review=False,
        source="vision",
        model_id="synthetic-vision",
        evidence="校园墙白名单|文案:" + PROMO,
    )
    return result.model_copy(update=updates)


@pytest.mark.parametrize("raw", ["null", "[]", "42", '"text"', "{malformed"])
def test_malformed_source_detail_is_not_a_permission(raw: str) -> None:
    assert _wall_source_from_detail(raw)[0] is None


@pytest.mark.parametrize(
    "results",
    [
        [{"source": "vision", "evidence": "校园墙白名单|文案:" + PROMO}],
        [_vision().model_dump(), _vision(needs_review=True).model_dump()],
        [_vision().model_dump(), "not-a-result-object"],
    ],
    ids=["missing-confirmation-fields", "unresolved-second-result", "invalid-result-entry"],
)
def test_partial_or_conflicting_source_results_do_not_grant_permission(results: list) -> None:
    assert _wall_source_from_detail(json.dumps({"ai_results": results}))[0] is None


async def _row(
    group: str,
    *,
    sent_at: datetime,
    created_at: datetime,
    kind: str = "image",
    result: AIModerationResult | None = None,
) -> None:
    async with SessionLocal() as session:
        session.add(
            ShadowDecision(
                message_id="row-" + uuid.uuid4().hex,
                provider="onebot",
                external_group_id=group,
                external_user_id="synthetic-user",
                group_openid=group,
                member_openid="synthetic-user",
                kind=kind,
                verdict="allow" if kind == "image" else "record_only",
                detail_json=json.dumps(
                    {
                        "sent_at": sent_at.isoformat(),
                        "ai_results": [result.model_dump()] if result else [],
                    },
                    ensure_ascii=False,
                ),
                created_at=created_at.replace(tzinfo=None),
            )
        )
        await session.commit()


async def _pair(msg: StandardMessage) -> ModerationDecision:
    async with SessionLocal() as session:
        result = await maybe_wall_text_pairing(session, msg, _high(msg))
        await session.commit()
        return result


@pytest.mark.parametrize(
    "result",
    [
        _vision(category="ad"),
        _vision(needs_review=True),
        _vision(degraded_reason="synthetic_timeout"),
    ],
    ids=["category", "needs-review", "degraded-reason"],
)
async def test_runtime_result_schema_rejects_unconfirmed_wall(result: AIModerationResult) -> None:
    """The producer writes model_dump, not the cat/nr/deg test-only aliases."""
    now = datetime.now(UTC)
    group = "schema-" + uuid.uuid4().hex
    await _row(
        group,
        sent_at=now - timedelta(seconds=20),
        created_at=now - timedelta(seconds=10),
        result=result,
    )
    assert (await _pair(_message(group, now))).verdict == "violation_high"


async def test_intervening_image_does_not_break_wall_window() -> None:
    now = datetime.now(UTC)
    group = "middle-image-" + uuid.uuid4().hex
    await _row(
        group,
        sent_at=now - timedelta(seconds=30),
        created_at=now - timedelta(seconds=25),
        result=_vision(),
    )
    await _row(
        group,
        sent_at=now - timedelta(seconds=15),
        created_at=now - timedelta(seconds=10),
    )
    result = await _pair(_message(group, now))
    assert result.verdict == "record_only"
    assert result.recommended_actions == []


async def test_intervening_text_does_not_break_wall_window() -> None:
    now = datetime.now(UTC)
    group = "middle-fast-" + uuid.uuid4().hex
    await _row(
        group,
        sent_at=now - timedelta(seconds=30),
        created_at=now - timedelta(seconds=5),
        result=_vision(),
    )
    await _row(
        group,
        sent_at=now - timedelta(seconds=15),
        created_at=now - timedelta(seconds=10),
        kind="text",
    )
    result = await _pair(_message(group, now))
    assert result.verdict == "record_only"
    assert result.recommended_actions == []


async def test_earlier_text_finishing_after_image_still_exempted() -> None:
    now = datetime.now(UTC)
    group = "earlier-slow-" + uuid.uuid4().hex
    await _row(
        group,
        sent_at=now - timedelta(seconds=30),
        created_at=now - timedelta(seconds=15),
        result=_vision(),
    )
    await _row(
        group,
        sent_at=now - timedelta(seconds=60),
        created_at=now - timedelta(seconds=5),
        kind="text",
    )
    result = await _pair(_message(group, now))
    assert result.verdict == "record_only"
    assert result.recommended_actions == []


async def test_offset_timestamp_is_compared_as_an_instant() -> None:
    now = datetime.now(UTC)
    group = "timezone-" + uuid.uuid4().hex
    await _row(
        group,
        sent_at=(now - timedelta(seconds=30)).astimezone(timezone(timedelta(hours=8))),
        created_at=now - timedelta(seconds=20),
        result=_vision(),
    )
    assert (await _pair(_message(group, now))).verdict == "record_only"


async def test_concurrent_messages_both_exempted_within_window() -> None:
    now = datetime.now(UTC)
    group = "atomic-" + uuid.uuid4().hex
    await _row(
        group,
        sent_at=now - timedelta(seconds=30),
        created_at=now - timedelta(seconds=20),
        result=_vision(),
    )
    results = await asyncio.wait_for(
        asyncio.gather(_pair(_message(group, now)), _pair(_message(group, now))), timeout=5
    )
    assert sorted(result.verdict for result in results) == ["record_only", "record_only"]


async def test_same_second_without_ordering_evidence_does_not_grant_exemption() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    group = "same-second-" + uuid.uuid4().hex
    await _row(group, sent_at=now, created_at=now, result=_vision())
    assert (await _pair(_message(group, now))).verdict == "violation_high"


class _Source:
    provider = "onebot"

    def __init__(self, msg: StandardMessage):
        self.msg = msg

    def parse_group_message(self, payload):
        return self.msg


class _SlowImageAI:
    def __init__(self):
        self.image_entered = asyncio.Event()
        self.release_image = asyncio.Event()

    async def review_message(self, session, msg, decision, **kwargs):
        if msg.kind == "image":
            self.image_entered.set()
            await self.release_image.wait()
            return _high(msg).model_copy(
                update={"verdict": "allow", "category": None, "recommended_actions": []}
            ), [_vision()]
        return _high(msg), []


async def test_text_cannot_be_punished_while_preceding_image_is_under_review(monkeypatch):
    """Real pipeline ordering, fake AI: image arrival precedes text, image AI is slow."""
    now = datetime.now(UTC)
    group = "pending-image-" + uuid.uuid4().hex
    image_msg = _message(group, now - timedelta(seconds=1)).model_copy(
        update={"kind": "image", "text": ""}
    )
    text_msg = _message(group, now)
    service = _SlowImageAI()
    decisions = {}

    async def fake_actions(session, msg, decision, **kwargs):
        decisions[msg.message_id] = decision
        return []

    async def run(msg):
        async with SessionLocal() as session:
            return await pipeline.run_pipeline(
                {"message_id": msg.message_id},
                session,
                message_source=_Source(msg),
                ai_service=service,
                dedup_key="onebot:10000001:" + msg.message_id,
            )

    monkeypatch.setattr(pipeline, "orchestrate_actions", fake_actions)
    task = asyncio.create_task(run(image_msg))
    try:
        await asyncio.wait_for(service.image_entered.wait(), timeout=5)
        text_result = await asyncio.wait_for(run(text_msg), timeout=5)
    finally:
        service.release_image.set()
        await asyncio.wait_for(task, timeout=5)
    assert text_result is not None
    assert text_result.verdict == "record_only"
    assert decisions[text_msg.message_id].recommended_actions == []


async def test_source_image_retry_keeps_window_exemption(monkeypatch):
    """口径 C 无状态重算：图片重试覆盖 detail，也不丢失窗口豁免。"""
    now = datetime.now(UTC)
    group = "image-retry-" + uuid.uuid4().hex
    image_msg = _message(group, now - timedelta(seconds=1)).model_copy(
        update={"kind": "image", "text": ""}
    )
    service = _SlowImageAI()
    service.release_image.set()
    original_mark = pipeline.mark_processed
    failed = False

    async def fail_once(session, key, token):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("synthetic failure after source image was persisted")
        return await original_mark(session, key, token)

    async def fake_actions(*args, **kwargs):
        return []

    async def run_image():
        async with SessionLocal() as session:
            return await pipeline.run_pipeline(
                {"message_id": image_msg.message_id},
                session,
                message_source=_Source(image_msg),
                ai_service=service,
                dedup_key="onebot:10000001:" + image_msg.message_id,
            )

    monkeypatch.setattr(pipeline, "orchestrate_actions", fake_actions)
    monkeypatch.setattr(pipeline, "mark_processed", fail_once)
    assert await run_image() is None
    assert (await _pair(_message(group, now))).verdict == "record_only"
    assert await run_image() is not None
    assert (await _pair(_message(group, now))).verdict == "record_only"
