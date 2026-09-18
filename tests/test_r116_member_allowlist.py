"""R-116 成员白名单（按QQ号）与合并转发/群名片撤回回归（负责人 2026-09-18）。

负责人口径：
1. 成员白名单**优先级最高**、**全类别完全放行**（明确选择"不守 B-2 底线"：
   诈骗/色情/暴力/刷屏同样放行），且 AI/动态规则/媒体层一律不得升级；
2. **合并转发（聊天记录）一律撤回**、**群名片（分享群卡片）一律撤回**；
3. 群主、管理员、白名单成员**不撤回**；
4. 白名单支持整份文件全量同步：先预览、确认后写入；空文件拒绝；
   单次停用超过 5 条且超过当前启用数 30% 时需二次确认。

全部使用合成数据（如 123456789 / synthetic-*），不写真实 QQ 号与真实样本。
"""

from __future__ import annotations

import json
import re
import uuid
from urllib.parse import unquote

from app.adapters.onebot.parser import OneBotMessageSource
from app.core.contracts import Attachment, Sender, ShareCardInfo, StandardMessage
from app.db import SessionLocal
from app.models import ActionLog, AdminAudit, AllowlistMember
from app.moderation.ai import AIModerationResult, merge_ai_evidence
from app.moderation.allowlist import (
    add_member,
    apply_member_import,
    delete_member,
    list_members,
    load_allowlist_members,
    match_allowlist_member,
    plan_member_import,
    set_member_enabled,
)
from app.moderation.allowlist_members_io import (
    format_member_list,
    parse_member_list,
    validate_member_id,
)
from app.moderation.decision import (
    ALLOWLIST_MEMBER_ALLOW_RULE_ID,
    FORWARD_RECORD_RECALL_RULE_ID,
    GROUP_CARD_RECALL_RULE_ID,
)
from app.moderation.dynamic_rules import (
    add_rule_item,
    create_rule_draft,
    load_active_snapshot,
    publish_rule_version,
)
from app.moderation.rules import TextRuleEngine
from app.runtime.pipeline import run_pipeline
from app.web import auth
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

_SEVERE_TEXT = "推广博彩渠道日结 加我微信"
_MEMBER_QQ = "123456789"


def _msg(
    *,
    text: str = "",
    kind: str = "text",
    role: str = "member",
    user_id: str = "synthetic-member",
    provider: str = "onebot",
    group: str | None = None,
    share_card: ShareCardInfo | None = None,
    attachments: list[Attachment] | None = None,
) -> StandardMessage:
    return StandardMessage(
        message_id="member-" + uuid.uuid4().hex,
        provider=provider,  # type: ignore[arg-type]
        external_group_id=group or ("member-group-" + uuid.uuid4().hex),
        sender=Sender(member_openid=user_id, role=role),  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        text=text,
        share_card=share_card,
        attachments=attachments or [],
    )


def _engine(
    *,
    terms: tuple[str, ...] = (),
    members: tuple[tuple[str, str], ...] = (),
) -> TextRuleEngine:
    return TextRuleEngine(allow_terms=frozenset(terms), allow_members=frozenset(members))


# ---------- 文件解析与格式化（纯函数） ----------


def test_parse_member_list_handles_notes_comments_and_bom() -> None:
    text = "\ufeff# 成员白名单\r\n\r\n123456789\r\n234567890  # 合作方\r\n345678901,备注二\r\n"
    parsed = parse_member_list(text)
    assert [item.user_id for item in parsed.valid] == ["123456789", "234567890", "345678901"]
    assert parsed.valid[0].note == ""
    assert parsed.valid[1].note == "合作方"
    assert parsed.valid[2].note == "备注二"
    assert parsed.invalid == ()


def test_parse_member_list_reports_invalid_and_duplicate_lines() -> None:
    parsed = parse_member_list("123456789\nabc123456789\n123\n123456789\n\n# comment\n")
    assert [item.user_id for item in parsed.valid] == ["123456789"]
    reasons = {(line.line_no, line.reason) for line in parsed.invalid}
    assert any("纯数字" in reason for _no, reason in reasons)
    assert any("长度" in reason for _no, reason in reasons)
    assert any("重复" in reason for _no, reason in reasons)
    # 行号必须指向原始文件行，便于人工按行修正
    assert {line.line_no for line in parsed.invalid} == {2, 3, 4}


def test_validate_member_id_rejects_out_of_range() -> None:
    for bad in ("", "   ", "1234", "1234567890123", "12ab56789"):
        try:
            validate_member_id(bad)
        except ValueError:
            continue
        raise AssertionError(f"应当拒绝：{bad!r}")


def test_format_member_list_roundtrips() -> None:
    text = format_member_list([("123456789", ""), ("234567890", "合作方")])
    parsed = parse_member_list(text)
    assert [(m.user_id, m.note) for m in parsed.valid] == [
        ("123456789", ""),
        ("234567890", "合作方"),
    ]


# ---------- 规则引擎：成员白名单 ----------


def test_member_allowlist_allows_severe_content() -> None:
    """成员白名单为全类别完全放行：诈骗样本也放行并打政策标记。"""
    decision = _engine(members=(("onebot", _MEMBER_QQ),)).evaluate(
        _msg(text=_SEVERE_TEXT, user_id=_MEMBER_QQ)
    )
    assert decision.verdict == "allow"
    assert decision.recommended_actions == []
    assert any(h.rule_id == ALLOWLIST_MEMBER_ALLOW_RULE_ID for h in decision.rule_hits)


def test_member_allowlist_is_provider_scoped() -> None:
    """官方通道 openid 不得命中 QQ 号白名单（跨通道身份隔离）。"""
    decision = _engine(members=(("onebot", _MEMBER_QQ),)).evaluate(
        _msg(text=_SEVERE_TEXT, user_id=_MEMBER_QQ, provider="qq_official")
    )
    assert decision.verdict == "violation_high"


def test_member_allowlist_empty_set_does_not_change_behavior() -> None:
    """空名单零影响（fail-closed 方向：读不到就不放行）。"""
    assert TextRuleEngine().evaluate(_msg(text=_SEVERE_TEXT)).verdict == "violation_high"


def test_member_allowlist_non_numeric_identity_never_matches() -> None:
    """非数字身份（如官方 openid）不会与 QQ 号白名单互串。"""
    members = frozenset({("onebot", _MEMBER_QQ)})
    assert match_allowlist_member("onebot", _MEMBER_QQ, members) == _MEMBER_QQ
    assert match_allowlist_member("onebot", "synthetic-openid", members) is None
    assert match_allowlist_member("qq_official", _MEMBER_QQ, members) is None


def test_member_allowlist_ai_second_review_does_not_upgrade() -> None:
    """成员白名单 + AI 二审确认 fraud 0.99 → 仍放行（全链路保护，防 R-113 复发）。"""
    local = _engine(members=(("onebot", _MEMBER_QQ),)).evaluate(
        _msg(text=_SEVERE_TEXT, user_id=_MEMBER_QQ)
    )
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


async def test_member_allowlist_dynamic_rule_does_not_upgrade() -> None:
    """成员白名单 + 显式动态规则（fraud 0.95）→ 仍放行（全链路保护）。"""
    msg = _msg(text="MEMBERALLOW 广告 加微信 abc12345", user_id=_MEMBER_QQ)
    async with SessionLocal() as session:
        draft = await create_rule_draft(
            session, scope="group", scope_key=msg.external_group_id, name="member allow dr"
        )
        await add_rule_item(
            session,
            draft.id,
            item_type="keyword",
            pattern="MEMBERALLOW",
            category="fraud",
            weight=0.95,
        )
        await publish_rule_version(session, draft.id, operator="synthetic-member")
        snapshot = await load_active_snapshot(session, msg.external_group_id)
    baseline = _engine(members=(("onebot", _MEMBER_QQ),)).evaluate(msg)
    assert baseline.verdict == "allow"
    engine = TextRuleEngine(
        rule_snapshot=snapshot, allow_members=frozenset({("onebot", _MEMBER_QQ)})
    )
    assert engine.evaluate(msg).verdict == "allow"


# ---------- 规则引擎：合并转发与群名片一律撤回 ----------


def test_forward_record_is_recalled() -> None:
    decision = TextRuleEngine().evaluate(_msg(kind="forward_record", text="[合并转发]"))
    assert decision.verdict == "violation_high"
    # 与处罚阶梯 record_violation 实际规划的动作保持一致（撤回+禁言+警告）；
    # OneBot 处于 recall_only 阶段时只会执行 recall。
    assert decision.recommended_actions == ["recall", "mute", "warn"]
    assert any(h.rule_id == FORWARD_RECORD_RECALL_RULE_ID for h in decision.rule_hits)


def test_forward_record_from_protected_role_is_not_recalled() -> None:
    """群主/管理员发的合并转发不撤回（只记录）。"""
    for role in ("owner", "admin"):
        decision = TextRuleEngine().evaluate(_msg(kind="forward_record", role=role))
        assert decision.verdict == "record_only", role
        assert decision.recommended_actions == [], role


def test_forward_record_from_allowlisted_member_is_not_recalled() -> None:
    decision = _engine(members=(("onebot", _MEMBER_QQ),)).evaluate(
        _msg(kind="forward_record", user_id=_MEMBER_QQ)
    )
    assert decision.verdict == "allow"
    assert decision.recommended_actions == []


def test_group_card_is_recalled() -> None:
    decision = TextRuleEngine().evaluate(
        _msg(kind="share_card", share_card=ShareCardInfo(is_group_card=True, title="某交流群"))
    )
    assert decision.verdict == "violation_high"
    assert decision.recommended_actions == ["recall", "mute", "warn"]
    assert any(h.rule_id == GROUP_CARD_RECALL_RULE_ID for h in decision.rule_hits)


def test_group_card_from_protected_role_is_not_recalled() -> None:
    """群主/管理员分享的卡片沿用 D-032 完全放行。"""
    decision = TextRuleEngine().evaluate(
        _msg(kind="share_card", role="admin", share_card=ShareCardInfo(is_group_card=True))
    )
    assert decision.verdict == "allow"
    assert decision.recommended_actions == []


def test_group_card_from_allowlisted_member_is_not_recalled() -> None:
    decision = _engine(members=(("onebot", _MEMBER_QQ),)).evaluate(
        _msg(
            kind="share_card",
            user_id=_MEMBER_QQ,
            share_card=ShareCardInfo(is_group_card=True),
        )
    )
    assert decision.verdict == "allow"
    assert decision.recommended_actions == []


def test_plain_share_card_is_not_recalled_by_group_card_rule() -> None:
    """普通来源卡片（非群名片）不得被新规则牵连撤回（维持既有人工复核）。"""
    decision = TextRuleEngine().evaluate(
        _msg(
            kind="share_card",
            share_card=ShareCardInfo(source="synthetic-miniapp", title="某音乐分享"),
        )
    )
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []
    assert not any(h.rule_id == GROUP_CARD_RECALL_RULE_ID for h in decision.rule_hits)


# ---------- OneBot 解析：群名片识别 ----------


def _onebot_payload(segments: list[dict[str, object]]) -> dict[str, object]:
    return {
        "time": 1789000700,
        "self_id": 10000001,
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": 910000100,
        "user_id": 200000100,
        "group_id": 300000002,
        "anonymous": None,
        "message": segments,
        "raw_message": "",
        "font": 0,
        "sender": {"user_id": 200000100, "nickname": "SANITIZED", "card": "", "role": "member"},
    }


def test_onebot_parser_detects_group_card() -> None:
    card = json.dumps(
        {
            "app": "com.tencent.qun.share",
            "desc": "[群名片]",
            "view": "group",
            "prompt": "[群名片]某交流群",
            "meta": {
                "group": {
                    "groupCode": "900000001",
                    "groupName": "某交流群",
                    "jumpUrl": "https://example.invalid/group",
                }
            },
        },
        ensure_ascii=False,
    )
    msg = OneBotMessageSource().parse_group_message(
        _onebot_payload([{"type": "json", "data": {"data": card}}])
    )
    assert msg.kind == "share_card"
    assert msg.share_card is not None
    assert msg.share_card.is_group_card is True
    assert msg.share_card.title == "某交流群"
    assert "[群名片" in msg.text


def test_onebot_parser_does_not_treat_other_card_as_group_card() -> None:
    card = json.dumps({"app": "com.tencent.music", "title": "某歌曲", "prompt": "[音乐]"})
    msg = OneBotMessageSource().parse_group_message(
        _onebot_payload([{"type": "json", "data": {"data": card}}])
    )
    assert msg.kind == "share_card"
    assert msg.share_card is not None
    assert msg.share_card.is_group_card is False


# ---------- 数据层：单条增删启停 + 文件全量同步 ----------


def _iso_provider() -> str:
    """隔离通道前缀：数据层测试用独立 provider，避免与其它测试互相污染。"""
    return "t-" + uuid.uuid4().hex[:8]


async def _purge_provider(provider: str) -> None:
    async with SessionLocal() as session:
        await session.execute(delete(AllowlistMember).where(AllowlistMember.provider == provider))
        await session.commit()


async def test_member_crud_dedupe_toggle_delete() -> None:
    provider = _iso_provider()
    qq = "800000001"
    try:
        async with SessionLocal() as session:
            row, created = await add_member(session, qq, operator="test:op", provider=provider)
            assert created
            again, created2 = await add_member(session, qq, operator="test:op", provider=provider)
            assert not created2 and again.id == row.id
            loaded = await load_allowlist_members(session)
            assert (provider, qq) in loaded
            updated = await set_member_enabled(session, row.id, False, operator="test:op")
            assert updated.enabled is False
            loaded2 = await load_allowlist_members(session)
            assert (provider, qq) not in loaded2
            removed = await delete_member(session, row.id, operator="test:op")
            assert removed == qq
    finally:
        await _purge_provider(provider)


async def test_plan_member_import_rejects_empty_file() -> None:
    async with SessionLocal() as session:
        try:
            await plan_member_import(session, "# 只有注释\n\n", provider=_iso_provider())
        except ValueError as exc:
            assert "没有有效的QQ号" in str(exc)
            return
        raise AssertionError("空文件必须被拒绝，不能清空白名单")


async def test_plan_member_import_full_sync_and_threshold() -> None:
    provider = _iso_provider()
    try:
        async with SessionLocal() as session:
            for index in range(20):
                await add_member(
                    session, f"8000000{index:02d}", operator="test:op", provider=provider
                )
            # 文件只保留 1 条已存在 + 1 条新增 → 其余 19 条应被停用
            plan = await plan_member_import(
                session, "800000000\n800000099 # 新增成员\n", provider=provider
            )
            assert [m.user_id for m in plan.to_add] == ["800000099"]
            assert len(plan.to_disable) >= 19
            assert plan.needs_confirm is True  # 19 > 5 且 > 30%
            assert plan.has_changes is True

            await apply_member_import(session, plan, operator="test:op")

            # 应用后再评估小幅度变更：只停用 1 条 → 不需要二次确认
            small = await plan_member_import(
                session, format_member_list([("800000000", "")]), provider=provider
            )
            assert [uid for _id, uid in small.to_disable] == ["800000099"]
            assert small.needs_confirm is False
            rows = {
                row.external_user_id: row for row in await list_members(session, provider=provider)
            }
            assert rows["800000099"].enabled is True
            assert rows["800000001"].enabled is False
            audit_count = await session.scalar(
                select(func.count())
                .select_from(AdminAudit)
                .where(AdminAudit.action == "allowlist_members_import")
            )
            assert audit_count and audit_count >= 1
            loaded = await load_allowlist_members(session)
            assert (provider, "800000099") in loaded
            assert (provider, "800000001") not in loaded
    finally:
        await _purge_provider(provider)


async def test_plan_member_import_requires_valid_file_content() -> None:
    async with SessionLocal() as session:
        try:
            await plan_member_import(session, "not-a-qq\n123\n", provider=_iso_provider())
        except ValueError as exc:
            assert "没有有效的QQ号" in str(exc)
            return
        raise AssertionError("没有有效行的文件必须被拒绝")


# ---------- 后台端到端：上传 → 预览 → 确认 → 生效 / 导出 ----------


def _client() -> TestClient:
    from app.main import app

    client = TestClient(app)
    assert (
        client.post(
            "/admin/login",
            data={"username": "admin", "password": "test-admin-pass"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    return client


def test_admin_member_import_preview_then_confirm_and_export() -> None:
    client = _client()
    qq_a, qq_b = "900000011", "900000012"
    content = f"# 成员白名单\n{qq_a}\n{qq_b}  # 测试成员\n"
    try:
        csrf = auth.csrf_token(client.cookies[auth.SESSION_COOKIE])
        preview = client.post(
            "/admin/allowlist/members/import",
            data={"csrf": csrf},
            files={"file": ("白名单.txt", content.encode("utf-8"), "text/plain")},
            follow_redirects=False,
        )
        assert preview.status_code == 200
        assert "导入预览" in preview.text
        assert qq_a in preview.text or qq_b in preview.text

        confirmed = client.post(
            "/admin/allowlist/members/import",
            data={"csrf": csrf, "confirmed": "1", "ack": "1"},
            files={"file": ("白名单.txt", content.encode("utf-8"), "text/plain")},
            follow_redirects=False,
        )
        assert confirmed.status_code == 303

        page = client.get("/admin/allowlist")
        assert page.status_code == 200
        assert qq_a in page.text
        assert qq_b in page.text

        exported = client.get("/admin/allowlist/members/export")
        assert exported.status_code == 200
        assert qq_a in exported.text
        assert "attachment" in exported.headers.get("content-disposition", "")

        # 空文件必须被拒绝：不会清空既有白名单
        empty = client.post(
            "/admin/allowlist/members/import",
            data={"csrf": csrf},
            files={"file": ("白名单.txt", b"", "text/plain")},
            follow_redirects=False,
        )
        assert empty.status_code == 303
        assert "已拒绝导入" in unquote(str(empty.headers.get("location", "")))
        still_there = client.get("/admin/allowlist")
        assert qq_a in still_there.text, "空文件不得清空既有成员白名单"
    finally:
        try:
            csrf = auth.csrf_token(client.cookies[auth.SESSION_COOKIE])
            page = client.get("/admin/allowlist")
            for member_id in sorted(
                set(re.findall(r"/admin/allowlist/members/(\d+)/delete", page.text))
            ):
                client.post(
                    f"/admin/allowlist/members/{member_id}/delete",
                    data={"csrf": csrf},
                    follow_redirects=False,
                )
        finally:
            auth.logout(client.cookies[auth.SESSION_COOKIE])
            client.close()


# ---------- 完整流水线：真实生效 + 影子零外呼 ----------


class _FixedSource:
    """最小入站 seam：固定返回同一条消息，不发任何网络请求。"""

    provider = "onebot"

    def __init__(self, message: StandardMessage) -> None:
        self._message = message

    def parse_group_message(self, payload: dict[str, object]) -> StandardMessage:
        return self._message


async def test_pipeline_member_allowlist_allows_severe_message() -> None:
    """真实生效证明：名单内成员的诈骗消息经完整流水线仍放行、零动作。"""
    qq = "900000123"
    msg = _msg(text=_SEVERE_TEXT, user_id=qq)
    async with SessionLocal() as session:
        row, created = await add_member(session, qq, operator="test:op")
        try:
            record = await run_pipeline(
                {"message_id": msg.message_id}, session, message_source=_FixedSource(msg)
            )
        finally:
            if created:
                await delete_member(session, row.id, operator="test:op")
    assert record is not None
    assert record.verdict == "allow"
    detail = json.loads(record.detail_json)
    assert detail["recommended_actions"] == []
    assert any(hit.get("rule_id") == ALLOWLIST_MEMBER_ALLOW_RULE_ID for hit in detail["rule_hits"])


async def test_pipeline_forward_recall_intent_is_shadow_only() -> None:
    """合并转发经完整流水线 → 高置信+召回意图；影子模式外部动作调用数为 0。"""
    msg = _msg(kind="forward_record", text="[合并转发]")
    async with SessionLocal() as session:
        record = await run_pipeline(
            {"message_id": msg.message_id}, session, message_source=_FixedSource(msg)
        )
        assert record is not None
        assert record.verdict == "violation_high"
        detail = json.loads(record.detail_json)
        assert detail["recommended_actions"] == ["recall", "mute", "warn"]
        # 外部动作结果表为空 = 没有任何真实撤回/禁言/警告被发出
        executed = await session.scalar(
            select(func.count())
            .select_from(ActionLog)
            .where(ActionLog.message_id == msg.message_id)
        )
        assert executed == 0


def test_review_gate_keeps_structural_recall_rules() -> None:
    """结构性规则（R007/R008）是独立硬证据：复核门不得把它们降级为转人工。"""
    from app.moderation.review_gate import ReviewGate

    gate = ReviewGate()
    for msg in (
        _msg(kind="forward_record", text="[合并转发]"),
        _msg(kind="share_card", share_card=ShareCardInfo(is_group_card=True)),
    ):
        primary = TextRuleEngine().evaluate(msg)
        assert primary.verdict == "violation_high"
        reviewed = gate.review(msg, primary)
        assert reviewed.verdict == "violation_high"
        assert reviewed.recommended_actions[0] == "recall"


def test_structural_rule_ids_avoid_builtin_rule_numbering() -> None:
    """回归（2026-09-18 事件）：结构性规则编号不得落在内置规则的 `R0xx` 段。

    曾误用 `R007` —— 与内置 `contextual_ad_terms` 撞号，使复核门把该**内置规则**
    当成独立硬证据，放宽了自动处罚门槛（实测 3 条判定被放宽；因当时处于急停窗口，
    未造成真实动作）。
    """
    for rule_id in (FORWARD_RECORD_RECALL_RULE_ID, GROUP_CARD_RECALL_RULE_ID):
        assert not re.fullmatch(r"R0\d\d", rule_id), rule_id


def test_review_gate_hard_evidence_excludes_builtin_soft_rules() -> None:
    """回归：内置软/上下文规则编号不得出现在复核门硬证据集合里。"""
    from app.moderation.review_gate import _HARD_EVIDENCE_RULES

    assert not (set(_HARD_EVIDENCE_RULES) & {"R002", "R004", "R005", "R007"})
    assert FORWARD_RECORD_RECALL_RULE_ID in _HARD_EVIDENCE_RULES
    assert GROUP_CARD_RECALL_RULE_ID in _HARD_EVIDENCE_RULES


def test_group_card_from_allowed_source_is_still_recalled() -> None:
    """负责人口径为"群名片都要撤回"：允许来源（万能校园墙）的群名片同样撤回。

    这是对既有"允许来源卡片放行"例外的**有意覆盖**（D-038）；普通（非群名片）
    卡片仍沿用允许来源放行，见 test_plain_share_card_is_not_recalled_by_group_card_rule。
    """
    decision = TextRuleEngine().evaluate(
        _msg(
            kind="share_card",
            share_card=ShareCardInfo(source="万能校园墙", title="业务卡片", is_group_card=True),
        )
    )
    assert decision.verdict == "violation_high"
    assert decision.recommended_actions[0] == "recall"
    assert any(h.rule_id == GROUP_CARD_RECALL_RULE_ID for h in decision.rule_hits)


def test_onebot_parser_group_card_flag_survives_other_cards() -> None:
    """同一条消息含多张卡片时：只要有一张群名片，整体即按群名片处理（不得漏撤）。"""
    music = json.dumps({"app": "com.tencent.music", "title": "某歌曲", "prompt": "[音乐]"})
    group = json.dumps(
        {
            "app": "com.tencent.qun.share",
            "view": "group",
            "prompt": "[群名片]某交流群",
            "meta": {"group": {"groupCode": "900000002", "groupName": "某交流群"}},
        },
        ensure_ascii=False,
    )
    msg = OneBotMessageSource().parse_group_message(
        _onebot_payload(
            [
                {"type": "json", "data": {"data": music}},
                {"type": "json", "data": {"data": group}},
            ]
        )
    )
    assert msg.share_card is not None
    assert msg.share_card.is_group_card is True
    assert TextRuleEngine().evaluate(msg).verdict == "violation_high"


class _ViolatingImageEngine:
    """固定替身：任何图片都判违规（用于验证白名单不得被媒体层旁路）。"""

    def analyze(self, path: object) -> object:
        from app.moderation.image_engine import MediaAnalysis

        return MediaAnalysis("violation_high", 0.95, reason="synthetic media violation")


async def test_pipeline_member_allowlist_suppresses_media_violation(monkeypatch, tmp_path) -> None:
    """D-037 全链路：白名单成员的违规图片**不得**被媒体层升级成真实处罚。

    若不拦截（修复前），媒体层 merge 会把 allow 改成 violation_high，编排层随即
    产生撤回/禁言 —— 等于白名单被旁路。对照组：非白名单成员的同一张图仍照常违规。
    """
    import base64

    import app.runtime.pipeline as pipeline

    (tmp_path / "synthetic.png").write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aRZ8AAAAASUVORK5CYII="
        )
    )
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    whitelisted_qq, other_qq = "900000321", "900000322"

    class Source:
        """按 user_id 返回对应消息的最小 seam。"""

        provider = "onebot"

        def __init__(self, message: StandardMessage) -> None:
            self._message = message

        def parse_group_message(self, payload: dict[str, object]) -> StandardMessage:
            return self._message

    def _image_msg(user_id: str) -> StandardMessage:
        # 文本保持"无信号"，确保唯一违规来源是媒体层（否则对照组的违规来自文本，
        # 无法证明"媒体层仍会处罚非白名单成员"）。
        return _msg(
            kind="mixed",
            text="synthetic benign text",
            user_id=user_id,
            attachments=[Attachment(content_type="image/png", filename="synthetic.png")],
        )

    whitelisted = _image_msg(whitelisted_qq)
    control = _image_msg(other_qq)
    async with SessionLocal() as session:
        row, created = await add_member(session, whitelisted_qq, operator="test:op")
        try:
            allowed = await run_pipeline(
                {"message_id": whitelisted.message_id},
                session,
                message_source=Source(whitelisted),
                image_engine=_ViolatingImageEngine(),  # type: ignore[arg-type]
            )
            blocked = await run_pipeline(
                {"message_id": control.message_id},
                session,
                message_source=Source(control),
                image_engine=_ViolatingImageEngine(),  # type: ignore[arg-type]
            )
        finally:
            if created:
                await delete_member(session, row.id, operator="test:op")

    assert allowed is not None
    assert allowed.verdict == "allow", "白名单成员的违规图片不得被媒体层升级为处罚"
    allowed_detail = json.loads(allowed.detail_json)
    assert allowed_detail["recommended_actions"] == []
    assert "成员白名单" in allowed.reason

    # 对照组：非白名单成员同样一张违规图仍照常判违规（不得因本修复放松）
    assert blocked is not None
    assert blocked.verdict == "violation_high"
