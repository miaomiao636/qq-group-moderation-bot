"""R-115 全局白名单回归（负责人 2026-09-16）。

口径：白名单命中且非严重类别 → 完全放行（不处罚、不转人工）；
严重类别（诈骗/色情/暴力）与刷屏不豁免；AI 与动态规则不得升级；
后台保存后下一条消息立即生效（运行时逐消息直读数据库）。
"""

from __future__ import annotations

import json
import uuid

from app.core.contracts import Sender, StandardMessage
from app.db import SessionLocal
from app.moderation.ai import AIModerationResult, AIReviewService, merge_ai_evidence
from app.moderation.allowlist import (
    add_term,
    delete_term,
    load_allowlist_terms,
    set_term_enabled,
)
from app.moderation.decision import ALLOWLIST_ALLOW_RULE_ID
from app.moderation.dynamic_rules import (
    add_rule_item,
    create_rule_draft,
    load_active_snapshot,
    publish_rule_version,
)
from app.moderation.rules import TextRuleEngine
from app.runtime.pipeline import run_pipeline


def _msg(text: str, role: str = "member", group: str | None = None) -> StandardMessage:
    return StandardMessage(
        message_id="allow-" + uuid.uuid4().hex,
        provider="onebot",
        external_group_id=group or ("allow-group-" + uuid.uuid4().hex),
        sender=Sender(member_openid="synthetic-member", role=role),  # type: ignore[arg-type]
        kind="text",
        text=text,
    )


def _engine(terms: tuple[str, ...]) -> TextRuleEngine:
    return TextRuleEngine(allow_terms=frozenset(terms))


# ---------- 引擎：放行与边界 ----------


def test_allowlist_hit_allows_ad_content() -> None:
    """命中白名单的广告文本 → 完全放行 + 政策标记。"""
    decision = _engine(("办证",)).evaluate(_msg("专业办证 代做学历 加微信 13800001234"))
    assert decision.verdict == "allow"
    assert decision.recommended_actions == []
    assert any(h.rule_id == ALLOWLIST_ALLOW_RULE_ID for h in decision.rule_hits)


def test_allowlist_normalization_matches_case_variants() -> None:
    """归一化匹配：大小写与变体（与消息文本同源 apply_variants）。"""
    decision = _engine(("banzheng",)).evaluate(_msg("BANZHENG 广告 加微信 abc12345"))
    assert decision.verdict == "allow"


def test_allowlist_does_not_exempt_severe_categories() -> None:
    """严重类别不豁免：白名单词 + 裸聊/博彩 → 仍违规（B-2 底线）。"""
    for text in (
        "办证并推广裸聊，加我微信 13800001234",
        "办证 博彩渠道 日结 加微信 abc12345",
    ):
        decision = _engine(("办证",)).evaluate(_msg(text))
        assert decision.verdict == "violation_high", text


def test_allowlist_does_not_exempt_flood() -> None:
    """刷屏不豁免：同一群连续第 3 条相同内容仍触发违规（只豁免广告）。"""
    engine = _engine(("日结",))
    group = "flood-group-" + uuid.uuid4().hex
    decisions = [engine.evaluate(_msg("日结 加微信 abc12345", group=group)) for _ in range(3)]
    assert decisions[0].verdict == "allow"
    assert decisions[2].verdict == "violation_high"
    assert any(h.rule_id == "R005" for h in decisions[2].rule_hits)


def test_allowlist_empty_terms_do_not_change_behavior() -> None:
    """空白名单零影响（fail-closed 方向：读不到就不放行）。"""
    decision = TextRuleEngine().evaluate(_msg("专业办证 代做学历 加微信 13800001234"))
    assert decision.verdict == "allow"  # 办证既有口径已是 allow；关键是不引入新行为
    decision2 = TextRuleEngine().evaluate(_msg("刷单兼职日结，加我微信赚外快"))
    assert decision2.verdict == "violation_high"


# ---------- 链路：动态规则 / AI 不得升级 ----------


async def test_allowlist_dr_does_not_upgrade() -> None:
    """白名单放行 + 动态规则高置信命中 → 仍 allow（仅记录）。"""
    msg = _msg("测试白名单词B 加微信 abc12345")
    async with SessionLocal() as session:
        draft = await create_rule_draft(
            session, scope="group", scope_key=msg.external_group_id, name="allowlist dr"
        )
        await add_rule_item(
            session,
            draft.id,
            item_type="keyword",
            pattern="测试白名单词B",
            category="fraud",
            weight=0.95,
        )
        await publish_rule_version(session, draft.id, operator="synthetic-allowlist")
        snapshot = await load_active_snapshot(session, msg.external_group_id)
    engine = TextRuleEngine(rule_snapshot=snapshot, allow_terms=frozenset(("测试白名单词b",)))
    result = engine.evaluate(msg)
    assert result.verdict == "allow"
    assert result.recommended_actions == []


def test_allowlist_ai_pair_does_not_upgrade() -> None:
    """白名单放行 + AI 独立二审确认 fraud 0.99 → 仍 allow。"""
    local = _engine(("办证",)).evaluate(_msg("办证 加微信 synthetic_contact"))
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


# ---------- 服务层：增删启停 + 立即生效 ----------


async def test_service_add_dedupe_toggle_delete() -> None:
    unique = "白名单回归词" + uuid.uuid4().hex[:8]
    async with SessionLocal() as session:
        row, created = await add_term(session, unique, operator="test:op")
        try:
            assert created
            again, created2 = await add_term(session, unique, operator="test:op")
            assert not created2 and again.id == row.id
            terms = await load_allowlist_terms(session)
            assert row.normalized in terms
            updated = await set_term_enabled(session, row.id, False, operator="test:op")
            assert updated.enabled is False
            terms2 = await load_allowlist_terms(session)
            assert row.normalized not in terms2
        finally:
            await delete_term(session, row.id, operator="test:op")


# ---------- 端到端：后台添加 → 下一条消息经完整流水线放行（真实生效） ----------


async def test_end_to_end_add_then_pipeline_allows_next_message(tmp_path, monkeypatch) -> None:
    """真实生效证明：服务层添加 → 完整 run_pipeline（含 AI 判广告）→ allow。"""
    unique = "白名单端到端词" + uuid.uuid4().hex[:8]
    msg = _msg(unique + " 加微信 synthetic_contact")

    class FakeAd:
        model_id = "synthetic-allowlist-ad"
        prompt_version = "synthetic-allowlist-review"

        async def moderate_text(self, request):
            return AIModerationResult(
                model_id=self.model_id,
                prompt_version=self.prompt_version,
                category="ad",
                confidence=0.99,
                needs_review=False,
                evidence="synthetic ad classification",
            )

    class Source:
        provider = "onebot"

        def parse_group_message(self, payload):
            return msg

    ai = AIReviewService(
        enabled=True,
        enabled_groups={msg.external_group_id},
        text_moderator=FakeAd(),
    )
    async with SessionLocal() as session:
        row, created = await add_term(session, unique, operator="test:op")
        assert created
        try:
            record = await run_pipeline(
                {"message_id": msg.message_id},
                session,
                message_source=Source(),
                ai_service=ai,
            )
        finally:
            await delete_term(session, row.id, operator="test:op")
    assert record is not None
    assert record.verdict == "allow"
    detail = json.loads(record.detail_json)
    assert detail["recommended_actions"] == []
    assert any(hit.get("rule_id") == ALLOWLIST_ALLOW_RULE_ID for hit in detail.get("rule_hits", []))
