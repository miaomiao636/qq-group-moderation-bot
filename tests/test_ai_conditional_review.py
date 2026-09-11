"""R-105-A: real review flow with local media, test SQLite and fake models."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from app.core.contracts import Sender, StandardMessage
from app.db import SessionLocal
from app.moderation.ai import (
    AIModerationRequest,
    AIModerationResult,
    AIProviderError,
    AIQuota,
    AIReviewService,
    ai_cache_key,
)
from app.moderation.decision import ModerationDecision, RuleHit


class FakeVision:
    prompt_version = "test-conditional"

    def __init__(
        self,
        model_id: str,
        category: str | None,
        confidence: float,
        *,
        needs_review: bool = False,
        fails: bool = False,
    ) -> None:
        self.model_id = model_id
        self.category = category
        self.confidence = confidence
        self.needs_review = needs_review
        self.fails = fails
        self.calls = 0
        self.contexts: list[str] = []

    async def moderate_image(self, request: AIModerationRequest) -> AIModerationResult:
        self.calls += 1
        self.contexts.append(request.feedback_context)
        if self.fails:
            raise AIProviderError("provider_timeout")
        return AIModerationResult(
            category=self.category,
            confidence=self.confidence,
            needs_review=self.needs_review,
            model_id=self.model_id,
            prompt_version=self.prompt_version,
            source="vision",
        )


async def review(
    tmp_path: Path,
    primary: FakeVision,
    secondary: FakeVision | None,
    *,
    conflict: bool = False,
    protected: bool = False,
    repeat: bool = False,
    quota: AIQuota | None = None,
    **thresholds: float,
):
    group = f"review-{uuid.uuid4().hex}"
    msg = StandardMessage(
        message_id=f"message-{uuid.uuid4().hex}",
        provider="onebot",
        external_group_id=group,
        sender=Sender(member_openid="member"),
    )
    local = ModerationDecision(
        message_id=msg.message_id,
        external_group_id=group,
        verdict="record_only" if conflict or protected else "allow",
        is_protected_sender=protected,
        rule_hits=[RuleHit(rule_id="DR_ALLOW_1", rule_name="share_source")] if conflict else [],
        reason="允许规则与禁止规则冲突，转人工复核" if conflict else "本地未命中",
    )
    path = tmp_path / f"{uuid.uuid4().hex}.png"
    path.write_bytes(uuid.uuid4().bytes)
    service = AIReviewService(
        enabled=True,
        enabled_groups={group},
        vision_moderator=primary,
        review_vision_moderator=secondary,
        **thresholds,
        quota=quota or AIQuota(),
    )
    async with SessionLocal() as session:
        first = await service.review_message(session, msg, local, media_paths=[path])
        second = (
            await service.review_message(session, msg, local, media_paths=[path])
            if repeat
            else None
        )
    return first, second


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "category,confidence,expected",
    [(None, 0.99, "allow"), ("ad", 0.4, "allow"), ("ad", 0.95, "violation_high")],
)
async def test_routine_samples_use_only_primary(tmp_path, category, confidence, expected):
    primary = FakeVision("primary", category, confidence)
    secondary = FakeVision("review", "ad", 0.99)
    (decision, _), _ = await review(tmp_path, primary, secondary)
    assert decision.verdict == expected
    assert secondary.calls == 0


@pytest.mark.asyncio
async def test_gray_sample_requires_same_category_confirmation(tmp_path):
    primary = FakeVision("primary", "ad", 0.85)
    secondary = FakeVision("review", "ad", 0.95)
    (decision, results), _ = await review(tmp_path, primary, secondary)
    assert secondary.calls == 1
    assert decision.verdict == "violation_high"
    assert [r.review_role for r in results] == ["primary", "secondary"]
    assert results[0].review_reason == "confidence_gray_zone"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "secondary_kind", ["normal", "different", "failed", "missing", "same_model", "uncertain"]
)
async def test_entered_review_never_falls_back_to_single_model_punishment(tmp_path, secondary_kind):
    primary = FakeVision("primary", "ad", 0.95, needs_review=True)
    secondary = {
        "normal": FakeVision("review", None, 0.99),
        "different": FakeVision("review", "fraud", 0.99),
        "failed": FakeVision("review", "ad", 0.99, fails=True),
        "missing": None,
        "same_model": FakeVision("primary", "ad", 0.99),
        "uncertain": FakeVision("review", "ad", 0.99, needs_review=True),
    }[secondary_kind]
    (decision, _), _ = await review(tmp_path, primary, secondary)
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []


@pytest.mark.asyncio
async def test_primary_failure_cannot_promote_secondary_to_primary(tmp_path):
    primary = FakeVision("primary", None, 0.0, fails=True)
    secondary = FakeVision("review", "ad", 0.99)
    (decision, _), _ = await review(tmp_path, primary, secondary)
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []


@pytest.mark.asyncio
async def test_whitelist_conflict_forces_review_even_when_primary_high(tmp_path):
    primary = FakeVision("primary", "ad", 0.99)
    secondary = FakeVision("review", None, 0.99)
    (decision, _), _ = await review(tmp_path, primary, secondary, conflict=True)
    assert secondary.calls == 1
    assert decision.verdict == "record_only"


@pytest.mark.asyncio
@pytest.mark.parametrize("confidence", [0.85, 0.95])
async def test_cold_and_warm_cache_keep_same_decision_and_evidence_role(tmp_path, confidence):
    primary = FakeVision("primary", "ad", confidence)
    secondary = FakeVision("review", "ad", 0.95)
    (cold, cold_results), (warm, warm_results) = await review(
        tmp_path, primary, secondary, repeat=True
    )
    assert cold.verdict == warm.verdict == "violation_high"
    assert primary.calls == 1
    assert [r.source for r in cold_results] == [r.source for r in warm_results]
    assert [r.review_role for r in cold_results] == [r.review_role for r in warm_results]
    assert all(r.cache_hit for r in warm_results)


@pytest.mark.asyncio
async def test_configured_thresholds_control_both_routing_and_final_decision(tmp_path):
    primary = FakeVision("primary", "ad", 0.93)
    secondary = FakeVision("review", "ad", 0.91)
    (decision, _), _ = await review(
        tmp_path,
        primary,
        secondary,
        primary_direct_threshold=0.97,
        secondary_review_low=0.7,
        secondary_review_high=0.92,
    )
    assert secondary.calls == 1
    assert decision.verdict == "record_only"


def test_cache_is_scoped_by_group_and_transport():
    first = AIModerationRequest(message_id="one", group_openid="a", content_kind="image")
    second = AIModerationRequest(message_id="two", group_openid="b", content_kind="image")
    assert ai_cache_key(first, model_id="same", prompt_version="same") != ai_cache_key(
        second, model_id="same", prompt_version="same"
    )
    transported = first.model_copy(update={"message_provider": "onebot"})
    assert ai_cache_key(first, model_id="same", prompt_version="same") != ai_cache_key(
        transported, model_id="same", prompt_version="same"
    )


def test_unrecognized_category_cannot_become_a_confident_normal_result():
    from app.moderation.ai import provider_payload_to_result

    result = provider_payload_to_result(
        {
            "category": "unrecognized_type",
            "confidence": 0.99,
            "evidence": "unknown",
            "needs_review": False,
        },
        model_id="primary",
        prompt_version="v4",
        provider="fake",
        source="vision",
    )
    assert result.category == "other"
    assert result.needs_review is True


class FakeText:
    prompt_version = "test-conditional"

    def __init__(
        self,
        model_id: str,
        category: str | None,
        confidence: float,
        *,
        needs_review: bool = False,
    ) -> None:
        self.model_id = model_id
        self.category = category
        self.confidence = confidence
        self.needs_review = needs_review
        self.calls = 0

    async def moderate_text(self, request: AIModerationRequest) -> AIModerationResult:
        self.calls += 1
        return AIModerationResult(
            category=self.category,
            confidence=self.confidence,
            needs_review=self.needs_review,
            model_id=self.model_id,
            prompt_version=self.prompt_version,
            source="text",
        )


async def review_mixed(
    tmp_path: Path,
    text: FakeText,
    primary: FakeVision,
    secondary: FakeVision | None = None,
    *,
    with_media: bool = True,
):
    """S01 编排回归：同一条消息同时经文字通道与视觉通道。"""
    group = f"review-{uuid.uuid4().hex}"
    msg = StandardMessage(
        message_id=f"message-{uuid.uuid4().hex}",
        provider="onebot",
        external_group_id=group,
        sender=Sender(member_openid="member"),
        text="招兼职，加微信详聊",
    )
    local = ModerationDecision(
        message_id=msg.message_id,
        external_group_id=group,
        verdict="allow",
        reason="本地未命中",
    )
    media: list[Path] = []
    if with_media:
        path = tmp_path / f"{uuid.uuid4().hex}.png"
        path.write_bytes(uuid.uuid4().bytes)
        media.append(path)
    service = AIReviewService(
        enabled=True,
        enabled_groups={group},
        text_moderator=text,
        vision_moderator=primary,
        review_vision_moderator=secondary,
        quota=AIQuota(),
    )
    async with SessionLocal() as session:
        decision, results = await service.review_message(session, msg, local, media_paths=media)
    return decision, results


@pytest.mark.asyncio
async def test_cross_modal_text_ad_vision_normal_writes_no_action(tmp_path):
    """S01 复现场景: 文字判广告 0.99 + 图文判正常 0.99 → 不升罚、无动作建议。"""
    text = FakeText("text-model", "ad", 0.99)
    primary = FakeVision("primary", None, 0.99)
    decision, _ = await review_mixed(tmp_path, text, primary)
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []


@pytest.mark.asyncio
async def test_cross_modal_text_needs_review_vision_ad_writes_no_action(tmp_path):
    """S01: 文字要求人工 + 视觉判广告高置信 → 不升罚、无动作建议。"""
    text = FakeText("text-model", None, 0.80, needs_review=True)
    primary = FakeVision("primary", "ad", 0.97)
    decision, _ = await review_mixed(tmp_path, text, primary)
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []


@pytest.mark.asyncio
async def test_cross_modal_vision_ad_text_normal_writes_no_action(tmp_path):
    """S01 对称方向: 图文判广告 + 文字判正常 → 不升罚、无动作建议。"""
    text = FakeText("text-model", None, 0.95)
    primary = FakeVision("primary", "ad", 0.97)
    decision, _ = await review_mixed(tmp_path, text, primary)
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []


@pytest.mark.asyncio
async def test_single_modal_text_ad_without_media_still_upgrades(tmp_path):
    """不误伤: 纯文字广告（无媒体、视觉未参与）仍可升罚。"""
    text = FakeText("text-model", "ad", 0.99)
    primary = FakeVision("primary", None, 0.99)
    decision, _ = await review_mixed(tmp_path, text, primary, with_media=False)
    assert primary.calls == 0
    assert decision.verdict == "violation_high"
    assert decision.recommended_actions == ["recall", "mute", "warn"]


def test_supplied_secondary_disagreement_is_a_veto_even_without_a_reason_marker():
    from app.moderation.ai import merge_ai_evidence

    local = ModerationDecision(message_id="local")
    primary = AIModerationResult(
        category="ad",
        confidence=0.99,
        model_id="primary",
        source="vision",
        needs_review=False,
        review_role="primary",
    )
    secondary = AIModerationResult(
        category=None,
        confidence=0.99,
        model_id="secondary",
        source="vision",
        needs_review=False,
        review_role="secondary",
    )
    decision = merge_ai_evidence(local, [primary, secondary])
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []


@pytest.mark.asyncio
async def test_protected_sender_never_receives_actions(tmp_path):
    (decision, _), _ = await review(
        tmp_path,
        FakeVision("primary", "ad", 0.99),
        FakeVision("review", "ad", 0.99),
        protected=True,
    )
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []


@pytest.mark.asyncio
async def test_call_limit_blocks_secondary_and_preserves_manual_review(tmp_path):
    from app.moderation.ai import summarize_ai_usage

    async with SessionLocal() as session:
        existing = await summarize_ai_usage(session)
    quota = AIQuota(daily_call_limit=existing["provider_calls"] + 1)
    primary = FakeVision("primary", "ad", 0.85)
    secondary = FakeVision("review", "ad", 0.95)
    (decision, results), _ = await review(tmp_path, primary, secondary, quota=quota)
    assert primary.calls == 1
    assert secondary.calls == 0
    assert decision.verdict == "record_only"
    assert results[-1].degraded_reason == "ai_rate_or_budget_limited"


@pytest.mark.asyncio
async def test_usage_separates_primary_review_and_cache(tmp_path):
    from app.moderation.ai import summarize_ai_usage

    async with SessionLocal() as session:
        before = await summarize_ai_usage(session)
    await review(
        tmp_path, FakeVision("primary", "ad", 0.85), FakeVision("review", "ad", 0.95), repeat=True
    )
    async with SessionLocal() as session:
        after = await summarize_ai_usage(session)
    assert after["primary_calls"] - before["primary_calls"] == 1
    assert after["secondary_calls"] - before["secondary_calls"] == 1
    assert after["cache_hits"] - before["cache_hits"] == 2


@pytest.mark.asyncio
async def test_adapter_excludes_feedback_and_records_provider_token_usage():
    import json

    import httpx
    from app.adapters.ai.openai_compatible import SYSTEM_PROMPT, OpenAICompatibleVisionModerator

    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "category": "fraud",
                                    "confidence": 0.99,
                                    "evidence": "标识不覆盖违规",
                                    "needs_review": True,
                                }
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 123, "completion_tokens": 45},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = OpenAICompatibleVisionModerator(
            base_url="https://model.example", api_key="test-only", model_id="vision", client=client
        )
        result = await model.moderate_image(
            AIModerationRequest(
                message_id="fake",
                group_openid="fake",
                content_kind="image",
                media_bytes=b"fixture",
                feedback_context="PRIVATE_UNPUBLISHED_FEEDBACK",
            )
        )
    assert "PRIVATE_UNPUBLISHED_FEEDBACK" not in json.dumps(captured)
    assert "万能校园墙" not in SYSTEM_PROMPT
    assert result.input_tokens == 123
    assert result.output_tokens == 45
    assert result.cost_known is False
