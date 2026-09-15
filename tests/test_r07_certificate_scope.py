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
    """主审复现场景：办证推广+联系方式 → 不撤回（record_only、无动作）。"""
    engine = TextRuleEngine()
    decision = engine.evaluate(
        _msg("专业办证，代做学历证书，学信网可查，毕业证定制，加我微信 13800001234")
    )
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []
    assert "办证" in decision.reason


def test_certificate_with_recruitment_words_not_punished() -> None:
    """办证 + 兼职黑名单词混合：办证口径优先，不撤回。"""
    engine = TextRuleEngine()
    decision = engine.evaluate(_msg("办证 代做学籍 有兼职渠道 日结，详情加微聊"))
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []


def test_certificate_exception_branch_guards() -> None:
    """例外分支的守卫条件由实现保证（代码审查点）：
    category 必须为 ad、命中含 DR_ 前缀（动态规则）时不豁免——见 rules.py 分支。
    此处验证内置黑名单（extra_blacklist 走 R001）与办证词同现时口径优先。"""
    engine = TextRuleEngine(extra_blacklist=("代考服务",))
    decision = engine.evaluate(_msg("专业代考服务，学信网可查"))
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []


def test_non_certificate_ads_still_punished() -> None:
    """非办证类广告不受影响（回归：不因例外削弱既有防线）。"""
    engine = TextRuleEngine()
    decision = engine.evaluate(_msg("刷单兼职日结，加我微信赚外快"))
    assert decision.verdict == "violation_high"
    assert decision.recommended_actions == ["recall", "mute", "warn"]


def test_normal_text_still_allowed() -> None:
    """普通含"证书"字样的正常消息（如四六级证书展示）不误伤为办证广告。"""
    engine = TextRuleEngine()
    decision = engine.evaluate(_msg("今天拿到了四级证书，好开心"))
    assert decision.verdict == "allow"
