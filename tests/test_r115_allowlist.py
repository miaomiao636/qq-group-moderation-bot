"""R-115 全局白名单回归（负责人 2026-09-16）。

口径（R-115 W01 整改后）：白名单命中且非严重类别 → **仅广告放行**（不处罚、
不转人工）；诈骗/色情/暴力/刷屏不豁免；**后续证据层（动态规则/AI/媒体）
逐次复核类别**——出现任何非广告证据即回到既有门槛（高置信确认才升级、
疑似/冲突/不完整保留人工），不得把"免广告"扩大成全类别放行；
后台保存后下一条消息立即生效（运行时逐消息直读数据库）。
"""

from __future__ import annotations

import json
import uuid

import pytest
from app.core.contracts import Sender, StandardMessage
from app.db import SessionLocal
from app.models import AllowlistTerm
from app.moderation.ai import AIModerationResult, AIReviewService, merge_ai_evidence
from app.moderation.allowlist import (
    add_term,
    delete_term,
    load_allowlist_terms,
    normalize_term,
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
    """白名单 + 非广告动态规则（fraud 0.95）→ **不得放行**（R-115 W01 整改）。

    修复前：白名单无条件压制动态规则 → allow（审查认定的 P1 缺陷）；
    修复后：非广告动态规则照常升级（violation_high）。"""
    msg = _msg("BANZHENG 广告 加微信 abc12345")
    async with SessionLocal() as session:
        draft = await create_rule_draft(
            session, scope="group", scope_key=msg.external_group_id, name="allowlist dr"
        )
        await add_rule_item(
            session,
            draft.id,
            item_type="keyword",
            pattern="BANZHENG",
            category="fraud",
            weight=0.95,
        )
        await publish_rule_version(session, draft.id, operator="synthetic-allowlist")
        snapshot = await load_active_snapshot(session, msg.external_group_id)
    # 无动态规则时该样本走白名单放行（确保本回归测的是合并层而非本地层）
    baseline = _engine(("banzheng",)).evaluate(msg)
    assert baseline.verdict == "allow"
    engine = TextRuleEngine(rule_snapshot=snapshot, allow_terms=frozenset(("banzheng",)))
    result = engine.evaluate(msg)
    assert result.verdict == "violation_high"


def test_allowlist_ai_pair_does_not_upgrade() -> None:
    """白名单 + AI 独立二审确认 fraud 0.99 → **不得放行**（R-115 W01 整改）。

    修复前：提前返回 → allow；修复后：落到既有门槛 → violation_high。
    注意：旧版本此测试使用"办证"样本（混入 D-031 独立政策）——本回归改用
    纯白名单词样本，避免政策混淆（审查 §W01 指出的测试污染）。"""
    local = _engine(("banzheng",)).evaluate(_msg("BANZHENG 广告 加微信 abc12345"))
    assert local.verdict == "allow"  # 本地白名单放行（仅广告类命中）
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
    assert decision.verdict == "violation_high"


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


# ---------- 管理面回归：幂等 / 防 ID 复用 / 等价词唯一（R-115 W02-W04） ----------


async def test_toggle_service_is_idempotent_with_explicit_target() -> None:
    """W02：显式目标 + 幂等——重复同目标不翻转（含审计 changed 标记）。"""
    unique = "幂等回归词" + uuid.uuid4().hex[:8]
    async with SessionLocal() as session:
        row, created = await add_term(session, unique, operator="test:op")
        assert created
        try:
            first = await set_term_enabled(session, row.id, False, operator="test:op")
            second = await set_term_enabled(session, row.id, False, operator="test:op")
            assert first.enabled is False and second.enabled is False
            third = await set_term_enabled(session, row.id, True, operator="test:op")
            fourth = await set_term_enabled(session, row.id, True, operator="test:op")
            assert third.enabled is True and fourth.enabled is True
        finally:
            await delete_term(session, row.id, operator="test:op")


def test_toggle_route_requires_target_and_is_idempotent() -> None:
    """W02 路由：缺少目标状态一律拒绝；重复同目标（双击/重试）幂等不翻转。"""
    import asyncio

    from app.main import app
    from fastapi.testclient import TestClient

    from tests.test_admin_web import extract_csrf

    unique = "路由幂等词" + uuid.uuid4().hex[:8]

    async def _create() -> int:
        async with SessionLocal() as s:
            row, _ = await add_term(s, unique, operator="test:op")
            return row.id

    async def _state(term_id: int) -> bool:
        async with SessionLocal() as s:
            row = await s.get(AllowlistTerm, term_id)
            assert row is not None
            return bool(row.enabled)

    term_id = asyncio.run(_create())
    try:
        with TestClient(app) as client:
            client.post(
                "/admin/login",
                data={"username": "admin", "password": "test-admin-pass"},
                follow_redirects=False,
            )
            page = client.get("/admin/allowlist")
            assert page.status_code == 200
            csrf = extract_csrf(page.text)
            # ① 缺目标（旧页面/异常请求）→ 拒绝执行，状态不变（仍启用）
            client.post(
                f"/admin/allowlist/{term_id}/toggle",
                data={"csrf": csrf},
                follow_redirects=False,
            )
            assert asyncio.run(_state(term_id)) is True
            # ② 停用两次（同目标；模拟双击/请求重试）→ 幂等，仍停用
            for _ in range(2):
                client.post(
                    f"/admin/allowlist/{term_id}/toggle",
                    data={"csrf": csrf, "target_enabled": "false"},
                    follow_redirects=False,
                )
            assert asyncio.run(_state(term_id)) is False
            # ③ 启用两次 → 仍启用
            for _ in range(2):
                client.post(
                    f"/admin/allowlist/{term_id}/toggle",
                    data={"csrf": csrf, "target_enabled": "true"},
                    follow_redirects=False,
                )
            assert asyncio.run(_state(term_id)) is True
    finally:

        async def _cleanup() -> None:
            async with SessionLocal() as s:
                await delete_term(s, term_id, operator="test:op")

        asyncio.run(_cleanup())


async def test_delete_then_add_does_not_reuse_id() -> None:
    """W03：删除后新增不复用旧 ID；旧 id 的表单操作报"不存在"（不误伤新词）。"""
    a = "复用词A" + uuid.uuid4().hex[:8]
    b = "复用词B" + uuid.uuid4().hex[:8]
    async with SessionLocal() as session:
        row_a, _ = await add_term(session, a, operator="test:op")
        old_id = row_a.id
        await delete_term(session, row_a.id, operator="test:op")
        row_b, _ = await add_term(session, b, operator="test:op")
        try:
            assert row_b.id > old_id  # AUTOINCREMENT：ID 单调，不复用
            with pytest.raises(ValueError):
                await delete_term(session, old_id, operator="test:op")
            still = await session.get(AllowlistTerm, row_b.id)
            assert still is not None and still.term == b
        finally:
            await delete_term(session, row_b.id, operator="test:op")


async def test_equivalent_terms_collapse_to_single_effective_term() -> None:
    """W04：等价词（大小写）不产生第二行；唯一约束兜底拒绝绕过服务层的重复插入。"""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    base = "等价词" + uuid.uuid4().hex[:8]
    async with SessionLocal() as session:
        row, created = await add_term(session, base, operator="test:op")
        assert created
        term_id = row.id
        try:
            again, created2 = await add_term(session, base.upper(), operator="test:op")
            assert not created2 and again.id == term_id
            # 绕过服务层先查的直接插入（raw SQL）→ normalized 唯一约束拒绝
            # （并发路径的最终保障；两 session 同时通过先查时由此兜底）
            with pytest.raises(IntegrityError):
                await session.execute(
                    text(
                        "INSERT INTO allowlist_terms "
                        "(term, normalized, enabled, created_by, created_at, updated_at) "
                        "VALUES (:term, :norm, 1, 'test:op', "
                        "'2026-09-16 00:00:00', '2026-09-16 00:00:00')"
                    ),
                    {"term": base.upper() + "X", "norm": normalize_term(base)},
                )
            await session.rollback()
        finally:
            await delete_term(session, term_id, operator="test:op")
