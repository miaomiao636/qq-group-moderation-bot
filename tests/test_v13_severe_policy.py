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

HIGH_ACTIONS = ["recall"]


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
    """纯办证（无严重词）：**完全放行**（2026-09-16 口径 A：不处罚不转人工）。"""
    engine = TextRuleEngine()
    decision = engine.evaluate(_msg("专业办证 代做学历证书 学信网可查 加我微信"))
    assert decision.verdict == "allow"
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


# ---------- 边界回归（主审关闭验收要求第 2 条） ----------


def test_boundary_plain_ad_mixed_with_severe_punished_as_severe() -> None:
    """普通广告词与严重类别混合：按严重类别处罚（广告词不掩盖严重类别）。"""
    engine = TextRuleEngine()
    cases = [
        ("刷单兼职日结 另外出售枪支弹药", "violence"),
        ("代发一条 加我微信 顺带推广博彩渠道", "fraud"),
    ]
    for text, expected_category in cases:
        decision = engine.evaluate(_msg(text))
        assert decision.verdict == "violation_high", text
        assert decision.category == expected_category, text
        assert decision.recommended_actions == HIGH_ACTIONS, text


def test_boundary_local_violation_not_overridden_by_ai_normal() -> None:
    """本地高置信处罚 + AI 错误判正常：不得覆盖本地处罚（AI 关闭/失败等价路径）。"""
    local = _local("本地硬证据处罚", verdict="violation_high")
    local = local.model_copy(update={"category": "violence"})
    ai_normal = [
        AIModerationResult(
            category=None,
            confidence=0.99,
            evidence="model says normal (erroneous)",
            model_id="text-model",
            source="text",
            needs_review=False,
        )
    ]
    decision = merge_ai_evidence(local, ai_normal)
    assert decision.verdict == "violation_high"
    assert decision.category == "violence"


def test_boundary_single_model_high_confidence_severe_punished() -> None:
    """单模型高置信严重（needs_review=false、无二审）：照常进入处罚流程（B-2）。"""
    for category in ("fraud", "porn"):
        results = [
            AIModerationResult(
                category=category,  # type: ignore[arg-type]
                confidence=0.95,
                evidence="high confidence",
                model_id="text-model",
                source="text",
                needs_review=False,
            )
        ]
        decision = merge_ai_evidence(_local("无明显本地信号"), results)
        assert decision.verdict == "violation_high", category
        assert decision.recommended_actions == HIGH_ACTIONS, category


def test_boundary_protected_sender_never_punished() -> None:
    """受保护成员（群主/管理员）：即使高置信严重类别也只记录不处罚。"""
    local = _local("保护角色").model_copy(update={"is_protected_sender": True})
    results = [
        AIModerationResult(
            category="fraud",  # type: ignore[arg-type]
            confidence=0.99,
            evidence="high confidence",
            model_id="text-model",
            source="text",
            needs_review=False,
        )
    ]
    decision = merge_ai_evidence(local, results)
    assert decision.verdict != "violation_high"
    assert decision.recommended_actions == []


# ---------- P2 修复回归（主审 2026-09-15 复验：严重疑似不得被 ad 类别掩盖） ----------


def _cert_record_local() -> ModerationDecision:
    """本地办证路径（2026-09-16 口径 A）：直接取 engine 真实产出（allow + 办证豁免）。"""
    return TextRuleEngine().evaluate(_msg("专业办证 代做学历证书 学信网可查 加我微信"))


def _severe_suspect(
    category: str,
    confidence: float = 0.40,
    source: str = "text",
    model_id: str = "text-model",
) -> AIModerationResult:
    return AIModerationResult(
        category=category,  # type: ignore[arg-type]
        confidence=confidence,
        evidence="low-confidence severe suspect",
        model_id=model_id,
        source=source,  # type: ignore[arg-type]
        needs_review=True,
    )


def test_p2_local_ad_not_masking_severe_suspect() -> None:
    """组A（本地办证 + AI 低置信严重）：2026-09-16 口径 A——办证**完全放行**，
    AI 疑似不改变判定、不转人工（严重疑似的保留逻辑仍由组 B/场景③
    覆盖非办证路径）。"""
    for category in ("fraud", "porn", "violence"):
        decision = merge_ai_evidence(_cert_record_local(), [_severe_suspect(category)])
        assert decision.verdict == "allow", category
        assert decision.recommended_actions == [], category


def test_p2_text_ad_not_masking_vision_severe() -> None:
    """组B（文字 ad 0.75 先出现 + 视觉严重 0.40 在后）：严重类别胜出。"""
    for category in ("fraud", "porn", "violence"):
        results = [
            AIModerationResult(
                category="ad",
                confidence=0.75,
                evidence="text ad",
                model_id="text-model",
                source="text",
                needs_review=False,
            ),
            _severe_suspect(category, source="vision", model_id="vision-model"),
        ]
        decision = merge_ai_evidence(_local("无明显本地信号"), results)
        assert decision.verdict == "record_only", category
        assert decision.category == category, category
        assert decision.confidence == 0.40, category
        assert decision.recommended_actions == [], category


def test_p2_reverse_order_keeps_severe() -> None:
    """相反顺序（严重在前、ad 在后）：结果与正序一致。"""
    for category in ("fraud", "porn", "violence"):
        results = [
            _severe_suspect(category, source="vision", model_id="vision-model"),
            AIModerationResult(
                category="ad",
                confidence=0.75,
                evidence="text ad",
                model_id="text-model",
                source="text",
                needs_review=False,
            ),
        ]
        decision = merge_ai_evidence(_local("无明显本地信号"), results)
        assert decision.category == category, category
        assert decision.confidence == 0.40, category


def test_p2_multiple_severe_deterministic_selection() -> None:
    """多严重类别：置信高者优先；同置信按固定顺序（fraud<porn<violence）。"""
    decision = merge_ai_evidence(
        _local("无明显本地信号"), [_severe_suspect("fraud"), _severe_suspect("porn", 0.55)]
    )
    assert decision.category == "porn"
    assert decision.confidence == 0.55
    decision = merge_ai_evidence(
        _local("无明显本地信号"), [_severe_suspect("violence"), _severe_suspect("fraud")]
    )
    assert decision.category == "fraud"
    assert decision.confidence == 0.40


def test_p2_control_no_severe_keeps_previous_behavior() -> None:
    """控制：无严重疑似时办证仍为放行（2026-09-16 口径 A：allow、无动作）。"""
    results = [
        AIModerationResult(
            category="ad",
            confidence=0.40,
            evidence="ad",
            model_id="text-model",
            source="text",
            needs_review=True,
        )
    ]
    decision = merge_ai_evidence(_cert_record_local(), results)
    assert decision.verdict == "allow"
    assert decision.recommended_actions == []
