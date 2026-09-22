"""CAMPUS-SCOPE-20260922: explicit campus identity, never generic QR immunity."""

import json
from datetime import UTC, datetime

import pytest
from app.db import SessionLocal
from app.moderation.ai import AIProviderError, merge_ai_evidence, provider_payload_to_result
from app.moderation.decision import MINIPROGRAM_QR_ALLOW_RULE_ID, ModerationDecision
from app.moderation.wall_pair import _confirmed_sources, maybe_wall_text_pairing

from tests.test_wall_pair import _decision, _insert_wall_image, _msg, _new_group


def vision(**overrides):
    payload = dict(
        category=None,
        confidence=1.0,
        needs_review=False,
        has_miniprogram_code=True,
        campus_wall_source=None,
        evidence="小程序码通过|文案:合成外卖免单推广",
    )
    payload.update(overrides)
    return provider_payload_to_result(
        payload, model_id="synthetic", prompt_version="test", provider="synthetic", source="vision"
    )


def local(category="ad"):
    return ModerationDecision(
        message_id="synthetic",
        verdict="violation_high",
        category=category,
        confidence=0.95,
        recommended_actions=["recall", "mute", "warn"],
    )


@pytest.mark.parametrize("category", ["ad", "fraud"])
def test_generic_miniprogram_never_overrides_violation(category):
    result = merge_ai_evidence(local(category), [vision()])
    assert result.verdict == "violation_high"
    assert "recall" in result.recommended_actions
    assert not any(h.rule_id == MINIPROGRAM_QR_ALLOW_RULE_ID for h in result.rule_hits)


def test_confirmed_campus_code_retains_exemption():
    result = merge_ai_evidence(
        local(),
        [vision(campus_wall_source="万能校园墙", evidence="校园墙白名单|文案:万能校园墙 合成活动")],
    )
    assert result.verdict == "allow"
    assert result.recommended_actions == []
    assert "校园墙" in result.reason


@pytest.mark.parametrize("source", [True, 1, [], {}, "其他小程序", "true"])
def test_invalid_source_contract_rejected(source):
    with pytest.raises(AIProviderError):
        vision(campus_wall_source=source)


@pytest.mark.parametrize(
    "evidence",
    [
        "校园墙白名单|文案:外卖免单",
        "小程序码通过|文案:万能校园墙",
        "不符合校园墙白名单|文案:万能校园墙",
    ],
)
def test_identity_needs_same_result_explicit_brand_evidence(evidence):
    result = merge_ai_evidence(
        local(), [vision(campus_wall_source="万能校园墙", evidence=evidence)]
    )
    assert result.verdict == "violation_high"


def test_campus_image_cannot_launder_another_advertisement():
    result = merge_ai_evidence(
        local(),
        [
            vision(
                campus_wall_source="万能校园墙", evidence="校园墙白名单|文案:万能校园墙 合成活动"
            ),
            vision(category="ad", evidence="合成外卖广告"),
        ],
    )
    assert result.verdict == "violation_high"


@pytest.mark.parametrize(
    "evidence", ["小程序码通过|文案:合成活动", "校园墙白名单|文案:万能校园墙 合成活动"]
)
async def test_legacy_source_cannot_exempt_following_text(evidence):
    group = _new_group()
    from datetime import timedelta

    detail = {
        "sent_at": (datetime.now(UTC) - timedelta(seconds=10)).isoformat(),
        "ai_results": [
            {
                "source": "vision",
                "category": None,
                "needs_review": False,
                "degraded_reason": "",
                "has_miniprogram_code": True,
                "evidence": evidence,
            }
        ],
    }
    await _insert_wall_image(group=group, detail=json.dumps(detail))
    async with SessionLocal() as session:
        result = await maybe_wall_text_pairing(
            session, _msg("合成推广", group=group, sent_at=datetime.now(UTC)), _decision()
        )
    assert result.verdict == "violation_high"


def test_new_campus_identity_is_saved_for_followup():
    result = vision(
        campus_wall_source="万能校园墙", evidence="校园墙白名单|文案:万能校园墙 合成活动"
    )
    sources = _confirmed_sources({"ai_results": [result.model_dump()]})
    assert sources == [("万能校园墙 合成活动", "校园墙白名单", True)]


async def test_new_confirmed_campus_exempts_following_text():
    from datetime import timedelta

    group = _new_group()
    result = vision(
        campus_wall_source="万能校园墙", evidence="校园墙白名单|文案:万能校园墙 合成活动"
    )
    await _insert_wall_image(
        group=group,
        detail=json.dumps(
            {
                "sent_at": (datetime.now(UTC) - timedelta(seconds=10)).isoformat(),
                "ai_results": [result.model_dump()],
            }
        ),
    )
    async with SessionLocal() as session:
        decision = await maybe_wall_text_pairing(
            session, _msg("合成推广", group=group, sent_at=datetime.now(UTC)), _decision()
        )
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []
    assert "豁免" in decision.reason


def test_text_channel_cannot_claim_campus_identity():
    result = provider_payload_to_result(
        {
            "category": None,
            "confidence": 1,
            "needs_review": False,
            "campus_wall_source": "万能校园墙",
            "has_miniprogram_code": True,
            "evidence": "校园墙白名单|文案:万能校园墙",
        },
        model_id="synthetic",
        prompt_version="test",
        provider="synthetic",
        source="text",
    )
    assert result.campus_wall_source is None
    assert not result.has_miniprogram_code
    assert merge_ai_evidence(local(), [result]).verdict == "violation_high"


@pytest.mark.parametrize("category", ["ad", "fraud"])
def test_other_miniprogram_high_confidence_uses_normal_review(category):
    neutral = local().model_copy(
        update={"verdict": "allow", "category": None, "confidence": 0, "recommended_actions": []}
    )
    result = merge_ai_evidence(
        neutral, [vision(category=category, evidence="合成其他小程序违规内容")]
    )
    assert result.verdict == "violation_high"
    assert result.category == category
    assert "recall" in result.recommended_actions


def test_uncertain_miniprogram_stays_manual_review():
    neutral = local().model_copy(
        update={"verdict": "allow", "category": None, "confidence": 0, "recommended_actions": []}
    )
    result = merge_ai_evidence(neutral, [vision(category="ad", confidence=0.65, needs_review=True)])
    assert result.verdict == "record_only"
    assert result.recommended_actions == []
