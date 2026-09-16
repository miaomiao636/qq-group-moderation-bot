"""群主/管理员卡片完全放行回归（负责人 2026-09-16 口径）。

口径：所有群主/管理员分享的卡片（小程序码/校园墙等）→ allow（不处罚、不转人工）；
AI 结果与动态规则不得升级；普通成员的卡片行为不变（未知来源仍转人工）。
"""

from __future__ import annotations

import uuid

from app.core.contracts import Sender, ShareCardInfo, StandardMessage
from app.db import SessionLocal
from app.moderation.ai import AIModerationResult, merge_ai_evidence
from app.moderation.decision import PROTECTED_CARD_ALLOW_RULE_ID
from app.moderation.dynamic_rules import (
    add_rule_item,
    create_rule_draft,
    load_active_snapshot,
    publish_rule_version,
)
from app.moderation.rules import TextRuleEngine


def _card_message(role: str = "member") -> StandardMessage:
    return StandardMessage(
        message_id="prot-card-" + uuid.uuid4().hex,
        provider="onebot",
        external_group_id="prot-card-group-" + uuid.uuid4().hex,
        sender=Sender(member_openid="synthetic-member", role=role),  # type: ignore[arg-type]
        kind="share_card",
        text="[卡片消息] 小程序\nsource: 未知小程序\ntitle: 普通分享",
        share_card=ShareCardInfo(source="未知小程序", title="普通分享"),
    )


def test_owner_card_is_allowed_member_card_unchanged() -> None:
    """群主卡片 → allow + 政策标记；普通成员同卡片 → record_only（对照）。"""
    owner = TextRuleEngine().evaluate(_card_message("owner"))
    assert owner.verdict == "allow"
    assert owner.recommended_actions == []
    assert any(h.rule_id == PROTECTED_CARD_ALLOW_RULE_ID for h in owner.rule_hits)

    member = TextRuleEngine().evaluate(_card_message("member"))
    assert member.verdict == "record_only"
    assert member.recommended_actions == []


def test_protected_text_message_scope_unchanged() -> None:
    """范围检查：保护角色的**非卡片**消息行为不变（仍 record_only 不处罚）。"""
    msg = _card_message("admin").model_copy(
        update={"kind": "text", "share_card": None, "text": "招募兼职刷单，日结，加我微信 abc12345"}
    )
    decision = TextRuleEngine().evaluate(msg)
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []


async def test_protected_card_dr_does_not_upgrade() -> None:
    """群主卡片 + 动态规则命中 → 仍 allow（只记录，不升级）。"""
    msg = _card_message("owner")
    async with SessionLocal() as session:
        draft = await create_rule_draft(
            session, scope="group", scope_key=msg.external_group_id, name="prot card dr"
        )
        await add_rule_item(
            session,
            draft.id,
            item_type="keyword",
            pattern="普通分享",
            category="fraud",
            weight=0.95,
        )
        await publish_rule_version(session, draft.id, operator="synthetic-prot-card")
        snapshot = await load_active_snapshot(session, msg.external_group_id)
    result = TextRuleEngine(rule_snapshot=snapshot).evaluate(msg)
    assert result.verdict == "allow"
    assert result.recommended_actions == []
    assert any(h.rule_id.startswith("DR_") for h in result.rule_hits)  # 命中仅记录


def test_protected_card_ai_pair_does_not_upgrade() -> None:
    """群主卡片 + AI 独立二审（ad 0.99）→ 仍 allow（不升级、不转人工）。"""
    local = TextRuleEngine().evaluate(_card_message("admin"))
    assert local.verdict == "allow"
    ai_pair = [
        AIModerationResult(
            model_id=model_id,
            source="vision",
            review_role=role,
            review_group="synthetic-image",
            category="ad",
            confidence=0.99,
            needs_review=False,
            evidence="synthetic independent review",
        )
        for model_id, role in (("synthetic-p", "primary"), ("synthetic-s", "secondary"))
    ]
    decision = merge_ai_evidence(local, ai_pair)
    assert decision.verdict == "allow"
    assert decision.recommended_actions == []
