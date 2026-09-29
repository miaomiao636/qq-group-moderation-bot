"""R07（ad323b6 主审）：办证/学历类口径必须贯穿本地规则层，与 AI 提示词一致。

主审复现：办证推广文字（含联系方式）命中本地 R001/R003 → 直接 violation_high，
AI 调用为 0，提示词放行口径永远不生效。修复：本地规则层同源实现办证例外
（不撤回、转记录）；动态规则与严重类别不豁免。
"""

from __future__ import annotations

from app.core.contracts import Sender, StandardMessage
from app.moderation.rules import TextRuleEngine


def _msg(text: str) -> StandardMessage:
    return StandardMessage(
        message_id="m-" + text[:8],
        provider="onebot",
        external_group_id="G_CERT",
        sender=Sender(member_openid="U1"),
        text=text,
        kind="text",
    )


def test_certificate_promo_text_not_punished() -> None:
    """主审复现场景：办证推广+联系方式 → **完全放行**（allow、无动作；2026-09-16 口径 A）。"""
    engine = TextRuleEngine()
    decision = engine.evaluate(
        _msg("专业办证，代做学历证书，学信网可查，毕业证定制，加我微信 13800001234")
    )
    assert decision.verdict == "allow"
    assert decision.recommended_actions == []
    assert "办证" in decision.reason


def test_certificate_with_recruitment_words_not_punished() -> None:
    """办证 + 兼职语境词混合：办证口径优先，完全放行（2026-09-16 口径 A）。"""
    engine = TextRuleEngine()
    decision = engine.evaluate(_msg("办证 代做学籍 有兼职渠道 日结，详情加微聊"))
    assert decision.verdict == "allow"
    assert decision.recommended_actions == []


def test_certificate_exception_branch_guards() -> None:
    """例外分支守卫（R-113 / 2026-09-16 口径 A 更新）：
    豁免仅在 category==ad 时生效（保留例外仅"严重类别词"，如博彩/裸聊——见
    v13 场景①回归）；办证词与显式黑名单词（extra_blacklist）同现时按负责人
    口径完全放行。"""
    engine = TextRuleEngine(extra_blacklist=("代考服务",))
    decision = engine.evaluate(_msg("专业代考服务，学信网可查"))
    assert decision.verdict == "allow"
    assert decision.recommended_actions == []


def test_non_certificate_ads_still_punished() -> None:
    """非办证类广告不受影响（回归：不因例外削弱既有防线）。"""
    engine = TextRuleEngine()
    decision = engine.evaluate(_msg("刷单兼职日结，加我微信赚外快"))
    assert decision.verdict == "violation_high"
    assert decision.recommended_actions == ["recall"]


def test_normal_text_still_allowed() -> None:
    """普通含"证书"字样的正常消息（如四六级证书展示）不误伤为办证广告。"""
    engine = TextRuleEngine()
    decision = engine.evaluate(_msg("今天拿到了四级证书，好开心"))
    assert decision.verdict == "allow"
