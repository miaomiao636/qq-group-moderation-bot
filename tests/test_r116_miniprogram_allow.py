# CAMPUS-SCOPE-20260922: synthetic source fixtures adapted; see docs/2026-09-22-campus-source-scope.md.
"""D-039 回归：图片含微信小程序二维码 → 一律通过（负责人 2026-09-18）。

口径：
- 视觉模型结构化字段 ``has_miniprogram_code=true`` → 放行（不撤回、不转人工）；
- **保留的两条例外**：①**色情 / 暴力违禁品**不放行（负责人 2026-09-18 晚修订：
  **诈骗不再是例外**，含小程序码的诈骗内容同样放行）；
  ②本地硬证据（R001 黑名单词 / R003 联系方式 / R006 分享卡片 / DR_）不被图片外观覆盖；
- **只有视觉通道**能置该标记；文字通道的同名字段一律忽略；
- 没有该标记时**行为完全不变**（对照组）。

全部使用合成数据与固定假响应，不外呼任何模型。
"""

from __future__ import annotations

import pytest
from app.moderation.ai import (
    AIModerationResult,
    AIProviderError,
    merge_ai_evidence,
    provider_payload_to_result,
)
from app.moderation.decision import (
    MINIPROGRAM_QR_ALLOW_RULE_ID,
    ModerationDecision,
    RuleHit,
)


def _local(
    verdict: str = "allow",
    category: str | None = None,
    hits: tuple[RuleHit, ...] = (),
) -> ModerationDecision:
    return ModerationDecision(
        message_id="mp-msg",
        provider="onebot",
        external_group_id="mp-group",
        external_user_id="mp-member",
        verdict=verdict,  # type: ignore[arg-type]
        category=category,  # type: ignore[arg-type]
        confidence=0.95 if verdict == "violation_high" else 0.0,
        rule_hits=list(hits),
        recommended_actions=["recall"] if verdict == "violation_high" else [],
    )


def _vision(
    *,
    category: str | None,
    confidence: float = 0.95,
    has_miniprogram_code: bool = False,
    source: str = "vision",
    needs_review: bool = False,
    model_id: str = "synthetic-vision",
) -> AIModerationResult:
    return AIModerationResult(
        category=category,  # type: ignore[arg-type]
        confidence=confidence,
        campus_wall_source="万能校园墙",
        evidence="校园墙白名单|文案:万能校园墙 合成活动",
        model_id=model_id,
        provider="synthetic",
        source=source,  # type: ignore[arg-type]
        needs_review=needs_review,
        has_miniprogram_code=has_miniprogram_code,
    )


# ---------- 供应商 JSON 契约 ----------


def test_payload_accepts_miniprogram_flag_for_vision() -> None:
    result = provider_payload_to_result(
        {"category": None, "confidence": 1.0, "needs_review": False, "has_miniprogram_code": True},
        model_id="synthetic",
        prompt_version="t204-v14",
        provider="synthetic",
        source="vision",
    )
    assert result.has_miniprogram_code is True


def test_payload_ignores_miniprogram_flag_for_text_channel() -> None:
    """文字通道不得凭该字段获得放行（只有视觉有意义）。"""
    result = provider_payload_to_result(
        {"category": "ad", "confidence": 0.9, "needs_review": False, "has_miniprogram_code": True},
        model_id="synthetic",
        prompt_version="t204-v14",
        provider="synthetic",
        source="text",
    )
    assert result.has_miniprogram_code is False


def test_payload_rejects_non_boolean_miniprogram_flag() -> None:
    with pytest.raises(AIProviderError):
        provider_payload_to_result(
            {
                "category": "ad",
                "confidence": 0.9,
                "needs_review": False,
                "has_miniprogram_code": "true",
            },
            model_id="synthetic",
            prompt_version="t204-v14",
            provider="synthetic",
            source="vision",
        )


# ---------- 放行与例外 ----------


def test_miniprogram_code_allows_normal_content() -> None:
    decision = merge_ai_evidence(_local(), [_vision(category=None, has_miniprogram_code=True)])
    assert decision.verdict == "allow"
    assert decision.recommended_actions == []
    assert any(h.rule_id == MINIPROGRAM_QR_ALLOW_RULE_ID for h in decision.rule_hits)


def test_miniprogram_code_overrides_ad_only_local_violation() -> None:
    """本地仅凭广告软信号判的高置信，不因图片带码而被撤回。"""
    local = _local(
        verdict="violation_high",
        category="ad",
        hits=(RuleHit(rule_id="R002", rule_name="soft_signals", category="ad"),),
    )
    decision = merge_ai_evidence(
        local,
        [
            _vision(category=None, has_miniprogram_code=True),
            _vision(category="ad", confidence=0.95, model_id="synthetic-second"),
        ],
    )
    assert decision.verdict == "allow"
    assert decision.recommended_actions == []


def test_miniprogram_code_allows_fraud_after_2026_09_18_revision() -> None:
    """负责人 2026-09-18 晚修订：**诈骗不再例外**——图带小程序码时诈骗内容也放行。"""
    decision = merge_ai_evidence(
        _local(),
        [_vision(category="fraud", confidence=0.95, has_miniprogram_code=True)],
    )
    assert decision.verdict == "allow"
    assert decision.recommended_actions == []
    assert any(h.rule_id == MINIPROGRAM_QR_ALLOW_RULE_ID for h in decision.rule_hits)


@pytest.mark.parametrize("category", ["porn", "violence"])
def test_miniprogram_code_does_not_exempt_porn_or_violence(category: str) -> None:
    """唯二例外：色情 / 暴力违禁品——AI 高置信确认时图带小程序码也不放行。"""
    decision = merge_ai_evidence(
        _local(),
        [_vision(category=category, confidence=0.95, has_miniprogram_code=True)],
    )
    assert decision.verdict != "allow"
    assert not any(h.rule_id == MINIPROGRAM_QR_ALLOW_RULE_ID for h in decision.rule_hits)


def test_miniprogram_code_does_not_exempt_local_severe_category() -> None:
    local = _local(verdict="violation_high", category="porn")
    decision = merge_ai_evidence(local, [_vision(category=None, has_miniprogram_code=True)])
    assert decision.verdict == "violation_high"


def test_miniprogram_code_does_not_override_local_hard_evidence() -> None:
    """本地硬证据（黑名单词 R001）不被图片外观覆盖。"""
    local = _local(
        verdict="violation_high",
        category="ad",
        hits=(RuleHit(rule_id="R001", rule_name="explicit_blacklist", category="ad"),),
    )
    decision = merge_ai_evidence(local, [_vision(category=None, has_miniprogram_code=True)])
    assert decision.verdict == "violation_high"


def test_miniprogram_code_does_not_override_dynamic_rule() -> None:
    local = _local(
        verdict="violation_high",
        category="ad",
        hits=(RuleHit(rule_id="DR_12", rule_name="dynamic", category="ad"),),
    )
    decision = merge_ai_evidence(local, [_vision(category=None, has_miniprogram_code=True)])
    assert decision.verdict == "violation_high"


def test_without_flag_behavior_is_unchanged() -> None:
    """对照组：没有小程序码标记时，原判定链完全不变。"""
    decision = merge_ai_evidence(_local(), [_vision(category="ad", confidence=0.95)])
    assert decision.verdict == "violation_high"
    assert not any(h.rule_id == MINIPROGRAM_QR_ALLOW_RULE_ID for h in decision.rule_hits)
