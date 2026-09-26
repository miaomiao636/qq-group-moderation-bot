"""T-204 remote AI moderation tests.

All provider calls use fake or MockTransport implementations. No real API key or
network request is required.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from app.adapters.ai.openai_compatible import OpenAICompatibleTextModerator
from app.adapters.qq_official.contract import Sender, StandardMessage
from app.db import SessionLocal
from app.moderation.ai import (
    AIModerationRequest,
    AIModerationResult,
    AIProviderError,
    AIReviewService,
    AIUsageLog,
    ai_enabled_for_group,
    merge_ai_evidence,
    provider_payload_to_result,
    sanitize_text,
)
from app.moderation.decision import ModerationDecision, RuleHit
from app.runtime.pipeline import run_pipeline
from sqlalchemy import func, select


class FakeTextModerator:
    model_id = "fake-text"
    prompt_version = "t204-test"

    def __init__(self) -> None:
        self.calls = 0

    async def moderate_text(self, request: AIModerationRequest) -> AIModerationResult:
        self.calls += 1
        return AIModerationResult(
            category="ad",
            confidence=0.99,
            evidence=f"fake hit {request.sanitized_text()[:8]}",
            model_id=self.model_id,
            prompt_version=self.prompt_version,
            provider="fake",
            source="text",
        )


class FailingTextModerator:
    model_id = "fake-fail"
    prompt_version = "t204-test"

    async def moderate_text(self, request: AIModerationRequest) -> AIModerationResult:
        raise AIProviderError("provider_invalid_model_json")


def _allow_decision() -> ModerationDecision:
    return ModerationDecision(
        message_id=f"AI_LOCAL_{uuid.uuid4().hex[:6]}",
        group_openid="G_AI",
        sender_member_openid="M_AI",
        verdict="allow",
        confidence=0.0,
        reason="本地未命中任何规则",
    )


def _msg(text: str, *, group: str = "G_AI") -> StandardMessage:
    return StandardMessage(
        message_id=f"AI_MSG_{uuid.uuid4().hex[:8]}",
        group_openid=group,
        sender=Sender(member_openid="M_AI"),
        text=text,
    )


def test_single_text_ai_result_is_soft_evidence_only() -> None:
    """文字模型单结果只能升到 record_only（软证据），不直接违规。"""
    result = AIModerationResult(
        category="ad",
        confidence=0.99,
        evidence="疑似广告",
        model_id="fake",
        prompt_version="v1",
        source="text",
    )
    decision = merge_ai_evidence(_allow_decision(), [result])
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []


def test_single_vision_ai_high_confidence_ad_upgrades_to_violation() -> None:
    """A confident primary without uncertainty or local conflicts may act."""
    result = AIModerationResult(
        category="ad",
        confidence=0.95,
        evidence="图片含招聘引流广告",
        model_id="mimo-v2.5",
        prompt_version="v1",
        source="vision",
        needs_review=False,
    )
    decision = merge_ai_evidence(_allow_decision(), [result])
    assert decision.verdict == "violation_high"
    assert decision.category == "ad"
    assert decision.recommended_actions == ["recall"]


def test_single_vision_ai_fraud_high_confidence_upgrades() -> None:
    result = AIModerationResult(
        category="fraud",
        confidence=0.90,
        evidence="诈骗图片",
        model_id="mimo-v2.5",
        prompt_version="v1",
        source="vision",
        needs_review=False,
    )
    decision = merge_ai_evidence(_allow_decision(), [result])
    assert decision.verdict == "violation_high"


def test_vision_ai_below_threshold_stays_record_only() -> None:
    """Without an independent reviewer, gray-zone evidence remains record_only."""
    result = AIModerationResult(
        category="ad",
        confidence=0.75,
        evidence="疑似广告",
        model_id="mimo-v2.5",
        prompt_version="v1",
        source="vision",
    )
    decision = merge_ai_evidence(_allow_decision(), [result])
    assert decision.verdict == "record_only"


def test_vision_ai_other_category_stays_record_only() -> None:
    """视觉模型非 ad/fraud 类（如 other）即使高置信也不直接升级。"""
    result = AIModerationResult(
        category="other",
        confidence=0.95,
        evidence="未知内容",
        model_id="mimo-v2.5",
        prompt_version="v1",
        source="vision",
    )
    decision = merge_ai_evidence(_allow_decision(), [result])
    assert decision.verdict == "record_only"


def test_ai_result_rejects_action_like_fields() -> None:
    with pytest.raises(AIProviderError, match="forbidden"):
        provider_payload_to_result(
            {"category": "ad", "confidence": 0.99, "evidence": "x", "kick": True},
            model_id="fake",
            prompt_version="v1",
            provider="fake",
            source="text",
        )


def test_sanitize_text_masks_contacts() -> None:
    sanitized = sanitize_text("加我微信abcde12345，电话13812345678")
    assert "13812345678" not in sanitized
    assert "abcde12345" not in sanitized
    assert "[CONTACT_MASKED]" in sanitized


def test_ai_enabled_requires_explicit_group() -> None:
    from app.config import Settings

    settings = Settings(
        ai_enabled=True,
        ai_enabled_groups="G_ALLOWED",
        ai_base_url="https://ai.example/v1",
        ai_api_key="secret",
        ai_text_model="mimo-text",
        _env_file=None,
    )
    assert ai_enabled_for_group(settings, "G_ALLOWED") is True
    assert ai_enabled_for_group(settings, "G_OTHER") is False


@pytest.mark.asyncio
async def test_group_disabled_does_not_call_provider() -> None:
    fake = FakeTextModerator()
    service = AIReviewService(enabled=True, enabled_groups={"G_OTHER"}, text_moderator=fake)
    async with SessionLocal() as session:
        decision, results = await service.review_message(
            session, _msg("疑似内容", group="G_AI_DISABLED"), _allow_decision()
        )
    assert decision.verdict == "allow"
    assert results == []
    assert fake.calls == 0


@pytest.mark.asyncio
async def test_ai_cache_reuses_result_and_records_usage() -> None:
    group = f"G_AI_CACHE_{uuid.uuid4().hex[:6]}"
    fake = FakeTextModerator()
    service = AIReviewService(
        enabled=True,
        enabled_groups={group},
        text_moderator=fake,
    )
    text = f"缓存命中测试{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        first, first_results = await service.review_message(
            session, _msg(text, group=group), _allow_decision()
        )
        second, second_results = await service.review_message(
            session, _msg(text, group=group), _allow_decision()
        )
        usage_count = (
            await session.execute(
                select(func.count()).select_from(AIUsageLog).where(AIUsageLog.group_openid == group)
            )
        ).scalar_one()

    assert first.verdict == "record_only"
    assert second.verdict == "record_only"
    assert fake.calls == 1
    assert first_results[0].source == "text"
    assert second_results[0].source == "text"
    assert second_results[0].cache_hit is True
    assert usage_count == 2


@pytest.mark.asyncio
async def test_provider_failure_degrades_without_blocking_chain() -> None:
    group = f"G_AI_FAIL_{uuid.uuid4().hex[:6]}"
    service = AIReviewService(
        enabled=True,
        enabled_groups={group},
        text_moderator=FailingTextModerator(),
    )
    async with SessionLocal() as session:
        decision, results = await service.review_message(
            session, _msg("需要AI但供应商失败", group=group), _allow_decision()
        )
    assert decision.verdict == "record_only"
    assert results[0].degraded_reason == "provider_invalid_model_json"


@pytest.mark.asyncio
async def test_openai_compatible_text_adapter_sends_sanitized_payload() -> None:
    requests: list[httpx.Request] = []

    def route(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "category": "fraud",
                                    "confidence": 0.92,
                                    "evidence": "疑似诈骗",
                                    "needs_review": True,
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(route), timeout=10)
    moderator = OpenAICompatibleTextModerator(
        base_url="https://ai.example/v1",
        api_key="test-secret-key",
        model_id="mimo-text",
        client=client,
    )
    result = await moderator.moderate_text(
        AIModerationRequest(
            message_id="AI_OPENAI",
            group_openid="G_AI",
            content_kind="text",
            text="加我微信abcde12345，电话13812345678",
        )
    )
    await client.aclose()

    body = requests[0].content.decode("utf-8")
    assert result.category == "fraud"
    assert requests[0].headers["authorization"] == "Bearer test-secret-key"
    assert "test-secret-key" not in body
    assert "13812345678" not in body
    assert "abcde12345" not in body
    assert "[CONTACT_MASKED]" in body


@pytest.mark.asyncio
async def test_pipeline_records_ai_soft_evidence_without_auto_punish() -> None:
    group = f"G_AI_PIPE_{uuid.uuid4().hex[:6]}"
    service = AIReviewService(
        enabled=True,
        enabled_groups={group},
        text_moderator=FakeTextModerator(),
    )
    payload: dict[str, Any] = {
        "id": f"AI_PIPE_{uuid.uuid4().hex[:8]}",
        "group_openid": group,
        "group_id": group,
        "author": {
            "member_openid": "M_AI_PIPE",
            "member_role": "member",
            "bot": False,
            "username": "tester",
        },
        "content": "这是一条本地规则无法确定但AI认为可疑的内容",
        "attachments": [],
        "timestamp": "2026-09-06T10:00:00+08:00",
    }
    async with SessionLocal() as session:
        record = await run_pipeline(payload, session, ai_service=service)

    assert record is not None
    assert record.verdict == "record_only"
    detail = json.loads(record.detail_json)
    assert detail["recommended_actions"] == []
    assert detail["ai_results"][0]["model_id"] == "fake-text"
    assert "test-secret-key" not in record.detail_json


def _r01_conflict_local() -> ModerationDecision:
    """本地已因规则冲突判转人工。"""
    return ModerationDecision(
        message_id="AI_LOCAL_R01",
        group_openid="G_AI",
        sender_member_openid="M_AI",
        verdict="record_only",
        confidence=0.40,
        reason="与校园墙白名单规则冲突，转人工",
    )


def _r01_allowlist_local() -> ModerationDecision:
    """本地命中 DR_ALLOW_ 白名单规则（AI 给出类别即构成冲突）。"""
    return ModerationDecision(
        message_id="AI_LOCAL_R01B",
        group_openid="G_AI",
        sender_member_openid="M_AI",
        verdict="record_only",
        confidence=0.40,
        reason="白名单来源",
        rule_hits=[
            RuleHit(
                rule_id="DR_ALLOW_CAMPUS_WALL",
                rule_name="campus-wall-allow",
                category="ad",
                confidence_delta=0.0,
                evidence_masked="",
            )
        ],
    )


def _r01_text_ad() -> AIModerationResult:
    return AIModerationResult(
        category="ad",
        confidence=0.99,
        evidence="广告",
        model_id="fake-text",
        prompt_version="t204-v6",
        source="text",
        needs_review=False,
    )


@pytest.mark.parametrize("local", [_r01_conflict_local(), _r01_allowlist_local()])
def test_text_ai_never_overrides_local_rule_conflict(local: ModerationDecision) -> None:
    """R01：本地规则冲突/白名单冲突转人工时，文字AI不得升级为处罚建议。"""
    decision = merge_ai_evidence(local, [_r01_text_ad()])
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []


def test_text_ai_high_confidence_still_promotes_without_conflict() -> None:
    """正向对照：无冲突时高置信文字广告仍升级（召回能力不回退）。"""
    local = ModerationDecision(
        message_id="AI_LOCAL_R01C",
        group_openid="G_AI",
        sender_member_openid="M_AI",
        verdict="allow",
        confidence=0.0,
        reason="本地未命中任何规则",
    )
    decision = merge_ai_evidence(local, [_r01_text_ad()])
    assert decision.verdict == "violation_high"
    assert decision.recommended_actions == ["recall"]


def test_text_ai_local_category_conflict_stays_human_review() -> None:
    """本地已有类别结论且与文字AI不同 → 冲突，不得升级。"""
    local = ModerationDecision(
        message_id="AI_LOCAL_R01D",
        group_openid="G_AI",
        sender_member_openid="M_AI",
        verdict="record_only",
        confidence=0.70,
        category="other",
        reason="本地判定待人工确认",
    )
    decision = merge_ai_evidence(
        local,
        [
            AIModerationResult(
                category="ad",
                confidence=0.85,
                evidence="广告",
                model_id="fake-text",
                prompt_version="t204-v6",
                source="text",
            )
        ],
    )
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []


def _text_ad(confidence: float = 0.99, needs_review: bool = False) -> AIModerationResult:
    return AIModerationResult(
        category="ad",
        confidence=confidence,
        evidence="文字广告",
        model_id="fake-text",
        prompt_version="t204-v6",
        source="text",
        needs_review=needs_review,
    )


def _vision_ad(confidence: float = 0.97, needs_review: bool = False) -> AIModerationResult:
    return AIModerationResult(
        category="ad",
        confidence=confidence,
        evidence="图文广告",
        model_id="fake-vision",
        prompt_version="t204-v6",
        source="vision",
        needs_review=needs_review,
    )


def _vision_normal(confidence: float = 0.99) -> AIModerationResult:
    return AIModerationResult(
        category=None,
        confidence=confidence,
        evidence="图文正常",
        model_id="fake-vision",
        prompt_version="t204-v6",
        source="vision",
        needs_review=False,
    )


def _text_uncertain() -> AIModerationResult:
    return AIModerationResult(
        category=None,
        confidence=0.80,
        evidence="文字不确定，需人工",
        model_id="fake-text",
        prompt_version="t204-v6",
        source="text",
        needs_review=True,
    )


def test_cross_modal_text_ad_vision_normal_stays_human() -> None:
    """S01: 文字判广告（0.99）+ 图文判正常（0.99）不得直接升罚，保留人工。"""
    decision = merge_ai_evidence(_allow_decision(), [_text_ad(), _vision_normal()])
    assert decision.verdict != "violation_high"
    assert decision.recommended_actions == []


def test_cross_modal_vision_ad_text_normal_stays_human() -> None:
    """S01 对称方向: 图文判广告 + 文字判正常同样不得升罚。"""
    text_normal = AIModerationResult(
        category=None,
        confidence=0.95,
        evidence="文字正常",
        model_id="fake-text",
        prompt_version="t204-v6",
        source="text",
        needs_review=False,
    )
    decision = merge_ai_evidence(_allow_decision(), [_vision_ad(), text_normal])
    assert decision.verdict != "violation_high"
    assert decision.recommended_actions == []


def test_cross_modal_text_needs_review_vetoes_vision_ad() -> None:
    """S01: 文字通道要求人工时，视觉广告不得单通道升罚。"""
    decision = merge_ai_evidence(_allow_decision(), [_vision_ad(), _text_uncertain()])
    assert decision.verdict != "violation_high"
    assert decision.recommended_actions == []


def test_cross_modal_vision_needs_review_vetoes_text_ad() -> None:
    """S01: 视觉通道要求人工时，文字广告不得单通道升罚。"""
    vision_uncertain = AIModerationResult(
        category=None,
        confidence=0.80,
        evidence="图文不确定，需人工",
        model_id="fake-vision",
        prompt_version="t204-v6",
        source="vision",
        needs_review=True,
    )
    decision = merge_ai_evidence(_allow_decision(), [_text_ad(), vision_uncertain])
    assert decision.verdict != "violation_high"
    assert decision.recommended_actions == []


def test_single_modal_text_ad_without_vision_still_upgrades() -> None:
    """不误伤: 无图片的纯文字广告（无视觉结果）仍可升罚。"""
    decision = merge_ai_evidence(_allow_decision(), [_text_ad()])
    assert decision.verdict == "violation_high"
    assert decision.recommended_actions == ["recall"]
