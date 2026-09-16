"""R-113 事故回归（2026-09-16 口径 A）："办证"动态规则不得升级为违规。

事故链：2026-09-08 反馈把"办证"样本标成 fraud → 反馈挖掘生成"办证→fraud"DR
（DR_167/168）并发布 → 办证广告被判 violation_high 并真实撤回 2 次。
修复：办证/学历类完全放行（allow）；办证类 DR 与 AI 结果均不得升级；
非办证类的显式 DR 与本地严重类别词仍照常生效。
"""

from __future__ import annotations

import uuid

from app.core.contracts import Sender, StandardMessage
from app.db import SessionLocal
from app.moderation.ai import AIModerationResult, merge_ai_evidence
from app.moderation.dynamic_rules import (
    add_rule_item,
    create_rule_draft,
    load_active_snapshot,
    publish_rule_version,
)
from app.moderation.rules import TextRuleEngine


def _msg(text: str) -> StandardMessage:
    return StandardMessage(
        message_id="r113-" + uuid.uuid4().hex,
        provider="onebot",
        external_group_id="r113-group-" + uuid.uuid4().hex,
        sender=Sender(member_openid="synthetic-member"),
        kind="text",
        text=text,
    )


async def test_certificate_own_keyword_dr_does_not_upgrade() -> None:
    """事故复现：'办证'自身触发的 fraud DR（0.95）不得把办证文案升级为违规。"""
    msg = _msg("办证 学历提升(学信网可查) 助理社会工作师 支持淘宝担保交易")
    async with SessionLocal() as session:
        draft = await create_rule_draft(
            session, scope="group", scope_key=msg.external_group_id, name="r113 certificate dr"
        )
        await add_rule_item(
            session,
            draft.id,
            item_type="keyword",
            pattern="办证",
            category="fraud",
            weight=0.95,
        )
        await publish_rule_version(session, draft.id, operator="synthetic-r113")
        snapshot = await load_active_snapshot(session, msg.external_group_id)
    result = TextRuleEngine(rule_snapshot=snapshot).evaluate(msg)
    assert result.verdict == "allow"
    assert result.recommended_actions == []
    assert any(hit.rule_id.startswith("DR_") for hit in result.rule_hits)  # 命中仅记录


async def test_non_certificate_dr_still_blocks() -> None:
    """对照：非办证类的显式 DR（运营发布的其他阻断词）继续拦截，保护不过宽。"""
    msg = _msg("办证 定向合成阻断词")
    async with SessionLocal() as session:
        draft = await create_rule_draft(
            session, scope="group", scope_key=msg.external_group_id, name="r113 control dr"
        )
        await add_rule_item(
            session,
            draft.id,
            item_type="keyword",
            pattern="定向合成阻断词",
            category="fraud",
            weight=0.99,
        )
        await publish_rule_version(session, draft.id, operator="synthetic-r113")
        snapshot = await load_active_snapshot(session, msg.external_group_id)
    result = TextRuleEngine(rule_snapshot=snapshot).evaluate(msg)
    assert result.verdict == "violation_high"


def test_certificate_ai_severe_confirmation_does_not_upgrade() -> None:
    """办证本地豁免 + AI 独立二审确认 fraud 0.99 → 仍放行（口径 A）。"""
    local = TextRuleEngine().evaluate(_msg("办证，加我微信 synthetic_contact"))
    assert local.verdict == "allow"
    ai_pair = [
        AIModerationResult(
            model_id=model_id,
            source="vision",
            review_role=role,
            review_group="synthetic-image",
            category="fraud",
            confidence=0.99,
            needs_review=False,
            evidence="synthetic independent review",
        )
        for model_id, role in (("synthetic-p", "primary"), ("synthetic-s", "secondary"))
    ]
    decision = merge_ai_evidence(local, ai_pair)
    assert decision.verdict == "allow"
    assert decision.recommended_actions == []
