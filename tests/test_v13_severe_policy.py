"""v13 严重类别策略回归（负责人 2026-09-15 方案 B-2 + 场景③小修）。

口径（负责人批准）：
- 严重类别（诈骗/色情/暴力违禁品）不因校园墙特征或办证词豁免；
- 本地规则高置信命中 → 直接违规+处罚建议（场景①，B-2 期望）；
- 高置信确认 + 二审消疑 → 照常进入处罚流程（场景②，B-2 期望）；
- 低置信疑似严重类别 → 保留类别转人工记录，不得静默放行（场景③小修）；
- 纯办证 → 转人工记录不处罚。
"""

from __future__ import annotations

from app.core.contracts import Sender, StandardMessage
from app.moderation.ai import AIModerationResult, merge_ai_evidence
from app.moderation.decision import ModerationDecision
from app.moderation.rules import TextRuleEngine

HIGH_ACTIONS = ["recall", "mute", "warn"]


def _local(text: str, verdict: str = "allow") -> ModerationDecision:
    return ModerationDecision(
        message_id="m-v13",
        provider="onebot",
        external_group_id="G-V13",
        verdict=verdict,
        reason="",
    )


def _msg(text: str) -> StandardMessage:
    return StandardMessage(
        message_id="m-v13",
        provider="onebot",
        external_group_id="G-V13",
        sender=Sender(member_openid="U1"),
        text=text,
        kind="text",
    )


# ---------- 场景①：本地规则高置信命中 → 直接违规+处罚建议（B-2 期望） ----------


def _cert_mixed_text(severe: str) -> str:
    return f"办证 代做学历证书 学信网可查 另外{severe}"


def test_scenario1_local_severe_with_certificate_words_punished() -> None:
    """办证词与严重类别混合：本地规则直接处罚（不因办证词豁免）。"""
    engine = TextRuleEngine()
    cases = [
        ("推广博彩渠道日结", "fraud"),
        ("裸聊服务在线", "porn"),
        ("出售枪支弹药", "violence"),
    ]
    for severe, expected_category in cases:
        decision = engine.evaluate(_msg(_cert_mixed_text(severe)))
        assert decision.verdict == "violation_high", severe
        assert decision.category == expected_category, severe
        assert decision.recommended_actions == HIGH_ACTIONS, severe


def test_scenario1_control_plain_certificate_recorded_only() -> None:
    """纯办证（无严重词）：转人工记录，不处罚（负责人口径）。"""
    engine = TextRuleEngine()
    decision = engine.evaluate(_msg("专业办证 代做学历证书 学信网可查 加我微信"))
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []


def test_scenario1_control_plain_ad_still_punished() -> None:
    """普通广告不受影响（回归：不因本策略削弱既有防线）。"""
    engine = TextRuleEngine()
    decision = engine.evaluate(_msg("刷单兼职日结，加我微信赚外快"))
    assert decision.verdict == "violation_high"
    assert decision.recommended_actions == HIGH_ACTIONS


# ---------- 场景②：高置信确认 + 二审消疑 → 照常处罚（B-2 期望） ----------


def _pair(
    category: str,
    *,
    primary_needs_review: bool = True,
    secondary_needs_review: bool = False,
) -> list[AIModerationResult]:
    return [
        AIModerationResult(
            category=category,  # type: ignore[arg-type]
            confidence=0.99,
            evidence="primary",
            model_id="primary-model",
            source="vision",
            needs_review=primary_needs_review,
            review_group="g1",
        ),
        AIModerationResult(
            category=category,  # type: ignore[arg-type]
            confidence=0.99,
            evidence="secondary",
            model_id="secondary-model",
            source="vision",
            needs_review=secondary_needs_review,
            review_role="secondary",
            review_group="g1",
        ),
    ]


def test_scenario2_secondary_confirmation_restores_punishment() -> None:
    """主模型高置信但标需人工 + 独立二审同类别高置信确认 → 照常处罚。"""
    local = _local("严重类别内容")
    for category in ("fraud", "porn", "violence"):
        decision = merge_ai_evidence(local, _pair(category))
        assert decision.verdict == "violation_high", category
        assert decision.category == category, category
        assert decision.recommended_actions == HIGH_ACTIONS, category


# ---------- 场景③小修：低置信疑似严重 → 保留类别转人工，不静默放行 ----------


def test_scenario3_low_confidence_severe_kept_for_human() -> None:
    """低置信严重疑似（0.40 + 需人工）：转人工记录并保留类别。"""
    for category in ("fraud", "porn", "violence"):
        results = [
            AIModerationResult(
                category=category,  # type: ignore[arg-type]
                confidence=0.40,
                evidence="low-confidence suspect",
                model_id="text-model",
                source="text",
                needs_review=True,
            )
        ]
        decision = merge_ai_evidence(_local("无明显本地信号"), results)
        assert decision.verdict == "record_only", category
        assert decision.category == category, category
        assert decision.recommended_actions == [], category
        assert "严重类别" in decision.reason


def test_scenario3_control_normal_still_allowed() -> None:
    """确定性正常：不受场景③修复影响。"""
    results = [
        AIModerationResult(
            category=None,
            confidence=0.99,
            evidence="normal",
            model_id="text-model",
            source="text",
            needs_review=False,
        )
    ]
    decision = merge_ai_evidence(_local("普通消息"), results)
    assert decision.verdict == "allow"
    assert decision.recommended_actions == []


def test_scenario3_control_high_confidence_needs_review_to_human() -> None:
    """高置信严重但要求人工（无二审）：转人工记录（保持既有安全边界）。"""
    results = [
        AIModerationResult(
            category="fraud",  # type: ignore[arg-type]
            confidence=0.95,
            evidence="needs human",
            model_id="text-model",
            source="text",
            needs_review=True,
        )
    ]
    decision = merge_ai_evidence(_local("无明显本地信号"), results)
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []
