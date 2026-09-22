"""校园墙图后窗口豁免（CAMPUS-SCOPE-20260922 收窄）。

同成员、同 provider/群/机器人账号，在明确确认的万能校园墙图片后 120 秒内，
保留既有文字/图片记录豁免。来源必须携带新 campus_wall_source 与同条品牌文字
证据；历史「小程序码通过」或仅旧「校园墙白名单」标记不再授予来源资格。
普通小程序恢复正常审核，不能跨附件拼接身份或掩盖未决附件。已确认校园墙的
诈骗窗口豁免仍要求同条来源 has_miniprogram_code 严格为 true。色情/暴力/刷屏、
结构性撤回、同秒定序、在途保护、完整证据 veto 与无状态幂等边界保持不变。
extract_wall_text 保留历史解析兼容；解析成功本身不是来源确认。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contracts import StandardMessage
from app.moderation.campus_source import confirmed_campus_source
from app.moderation.decision import (
    FORWARD_RECORD_RECALL_RULE_ID,
    GROUP_CARD_RECALL_RULE_ID,
    ModerationDecision,
)
from app.runtime.models import ShadowDecision

WALL_PAIR_WINDOW_SECONDS = 120
WALL_MARK = "校园墙白名单"
WALL_TEXT_MARK = "文案:"
# Historical parsing only; _confirmed_sources additionally requires the new campus identity.
MINIPROGRAM_MARK = "小程序码通过"
SOURCE_MARKS = (WALL_MARK, MINIPROGRAM_MARK)

# 窗口豁免的适用范围（负责人 2026-09-18 晚："2 分钟内发的文字和图片都不撤回，
# 色情/暴力的文字不豁免"）：
# - 可豁免的消息类型：文字与图片。合并转发/群名片等**结构性规则**刻意不在此列，
#   它们仍"一律撤回"（2026-09-18 D-038 口径），窗口豁免不得覆盖；
# - 不豁免的类别：色情（porn）/ 暴力违禁品（violence）/ 刷屏（flood）。刷屏是行为
#   规则，豁免它等于关掉刷屏防护；若要一并豁免须负责人明确（改这一处即可）；
#   诈骗（fraud）按负责人两次口径（"严重类别改为色情暴力"）不再列入。
PAIR_EXEMPT_KINDS = frozenset({"text", "image"})
PAIR_BLOCKED_CATEGORIES = frozenset({"porn", "violence", "flood"})
# 结构性确定性规则（D-038：合并转发 / 群名片"一律撤回"）**不受窗口豁免影响**——
# 主审二轮 F04-R：`image + 群卡` 的顶部 kind=image、类别 ad，曾被窗口改成 record_only。
# 按 `decision.py` 导出的常量判断（不猜测 R0xx 段号）。
STRUCTURAL_RULE_IDS = frozenset({FORWARD_RECORD_RECALL_RULE_ID, GROUP_CARD_RECALL_RULE_ID})


def effective_categories(decision: ModerationDecision) -> frozenset[str]:
    """决策里**仍有效的全部类别证据**（主类别 + 规则命中类别）。

    主审 F01/F08：只读 `decision.category` 会漏判——多图消息的主类别取置信度最高者
    （`ai.py::merge_ai_evidence` 用 `max(confidence)`），其余命中（例如 porn/violence）
    只留在 `rule_hits`；刷屏同理（`rules.py` 的 `category = category or "flood"` 会被
    广告主类别抢先）。因此豁免判据必须看**全部有效证据**，而不是单一主类别。
    """
    categories = {decision.category} if decision.category else set()
    categories.update(hit.category for hit in decision.rule_hits if hit.category)
    return frozenset(categories)


def is_pairing_candidate(msg: Any, decision: ModerationDecision) -> bool:
    """当前消息是否值得去查"前图豁免来源"（前置过滤，避免为不可能豁免的消息查库）。

    与 `maybe_wall_text_pairing` 的判定条件保持**同一处定义**，避免 pipeline 的前置
    过滤与豁免函数漂移（历史上两者分别写在两处）。诈骗只有在来源图为小程序码放行图时
    才豁免，故此处不排除诈骗（由豁免函数按来源标记判定）。
    """
    if decision.verdict != "violation_high":
        return False
    if not decision.category:
        return False
    if effective_categories(decision) & PAIR_BLOCKED_CATEGORIES:
        return False
    # F04-R：结构性撤回规则优先于窗口豁免——含合并转发/群名片命中的消息不进入窗口候选。
    if any(hit.rule_id in STRUCTURAL_RULE_IDS for hit in decision.rule_hits):
        return False
    if msg.kind not in PAIR_EXEMPT_KINDS:
        return False
    # 图片消息的 text 常为空（只有 [图片] 占位或无文字），不得据此排除
    if msg.kind == "text":
        return bool(msg.text.strip())
    return True


def extract_wall_text(evidence: str) -> str | None:
    """R09 + D-039: 仅接受以「校园墙白名单」或「小程序码通过」**开头**的 evidence。

    返回图内文案（空串=图无文案；空串不构成豁免来源，见调用方 `if wall_text:`）。
    否定表述（如「不符合校园墙白名单|文案:…」）与其它位置出现的子串均不构成许可。
    """
    evidence = evidence.strip()
    mark = next((m for m in SOURCE_MARKS if evidence == m or evidence.startswith(m + "|")), None)
    if mark is None:
        return None
    if WALL_TEXT_MARK in evidence:
        return evidence.split(WALL_TEXT_MARK, 1)[1].strip()
    if mark == MINIPROGRAM_MARK:
        # D-039 未强制「文案:」标记：取首个分隔符之后的整段作为图内文案
        return evidence.split("|", 1)[1].strip() if "|" in evidence else ""
    return ""


def _detail_object(detail_json: str) -> dict[str, Any]:
    try:
        detail = json.loads(detail_json)
    except (TypeError, ValueError):
        return {}
    return detail if isinstance(detail, dict) else {}


def _source_mark(evidence: str) -> str | None:
    """证据**开头**的来源标记（校园墙 / 小程序码）；子串出现不算（R09 语义保留）。"""
    evidence = evidence.strip()
    for mark in SOURCE_MARKS:
        if evidence == mark or evidence.startswith(mark + "|"):
            return mark
    return None


def _confirmed_sources(detail: dict[str, Any]) -> list[tuple[str, str, bool]]:
    """结构化校验后的**全部**合格来源：[(图内文案, 来源标记, 结构化小程序码布尔)]。

    R09 校验（保持不变）：全部 vision 结果 category 为空、未降级、不需人工，且 evidence
    以「校园墙白名单」或「小程序码通过」开头并含非空图内文案；任一视觉结果仍有疑问时
    整条消息都不作来源（不能挑出另一条白名单证据消掉尚未解决的矛盾）。

    每条来源的标记与 `has_miniprogram_code` 都取自**同一条**视觉结果：
    主审二轮要求"前缀与布尔不能跨结果拼接"；主审 N01-R 进一步要求**同一条消息内的多个
    合格来源必须全部保留**——旧实现只返回第一条命中的 vision 结果，于是"校园墙图排在带码图
    前面"会把后面的合格带码来源丢掉，判定结果依赖附件顺序。
    """
    if detail.get("processing") or detail.get("evidence_vetoes"):
        return []
    results = detail.get("ai_results")
    if not isinstance(results, list) or any(not isinstance(r, dict) for r in results):
        return []
    # 主审 R09-D：来源资格必须基于**完整附件证据**。降级的附件（模型超时/限流/媒体
    # 读取失败 → `source="degraded"`）同样是"尚未定论的复核"，此前先按 `vision` 过滤
    # 会把它整个丢掉，于是"一张未决附件 + 一张带码图"的消息被当成**已确认来源**，
    # 进而在窗口内豁免后续诈骗文字——与"未决附件不处罚也不豁免"的既有边界相反。
    attachments = [r for r in results if r.get("source") in ("vision", "degraded")]
    if not attachments:
        return []
    # 使用 pipeline 的 AIModerationResult.model_dump 契约。缺字段不是确认正常，
    # 任一附件仍有疑问（降级通道 / 需人工 / 类别非空 / 降级原因非空）时，
    # 不能挑出另一条白名单证据消掉尚未解决的矛盾。
    if any(
        "category" not in r
        or r["category"] is not None
        or r.get("source") == "degraded"
        or r.get("needs_review") is not False
        or r.get("degraded_reason") != ""
        for r in attachments
    ):
        return []
    sources: list[tuple[str, str, bool]] = []
    for result in attachments:
        if not confirmed_campus_source(result):
            continue  # Legacy/generic QR evidence is not a confirmed campus source.
        evidence = result.get("evidence")
        if not isinstance(evidence, str):
            continue
        wall_text = extract_wall_text(evidence)
        mark = _source_mark(evidence)
        if wall_text and mark is not None:
            sources.append((wall_text, mark, result.get("has_miniprogram_code") is True))
    return sources


def _confirmed_source(detail: dict[str, Any]) -> tuple[str | None, str | None, bool]:
    """单来源视图（兼容既有调用方）：取第一条合格来源；无来源时 (None, None, False)。"""
    sources = _confirmed_sources(detail)
    return sources[0] if sources else (None, None, False)


def _wall_source_from_detail(detail_json: str) -> tuple[str | None, dict[str, Any]]:
    """R09: 从影子判定 detail 取（结构化校验后的）图内文案与完整 detail。

    返回 (文案或 None, detail)。None=不可作为豁免来源（非放行/无文案/未完成）。
    口径 C（2026-09-17）起不再有"已消费"概念：历史绑定字段不影响来源复用。
    """
    detail = _detail_object(detail_json)
    wall_text, _mark, _qr = _confirmed_source(detail)
    return wall_text, detail


def _parse_naive_iso(value: object) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


def _exempt_decision(decision: ModerationDecision) -> ModerationDecision:
    return decision.model_copy(
        update={
            "verdict": "record_only",
            "recommended_actions": [],
            "reason": (
                "校园墙图后豁免（负责人 2026-09-17 口径 C）：前 2 分钟内有同成员"
                "校园墙确认图，不撤回，转记录"
            ),
        }
    )


async def maybe_wall_text_pairing(
    session: AsyncSession,
    msg: StandardMessage,
    decision: ModerationDecision,
    *,
    pending_messages: tuple[StandardMessage, ...] = (),
    event_key: str | None = None,
) -> ModerationDecision:
    """图后 2 分钟窗口豁免（口径 C；2026-09-18 晚扩展为文字与图片，见模块 docstring）。

    必须在 orchestrate_actions 之前调用；命中时返回 record_only 版本。
    无状态重算：不写绑定、不消耗来源图，重试幂等。
    """
    if not is_pairing_candidate(msg, decision):
        return decision

    scope = [
        ShadowDecision.provider == msg.provider,
        ShadowDecision.external_group_id == msg.external_group_id,
        ShadowDecision.external_user_id == msg.external_user_id,
    ]
    if msg.provider == "onebot" and event_key is not None:
        parts = event_key.split(":")
        if (
            len(parts) != 3
            or parts[0] != "onebot"
            or not parts[1].isascii()
            or not parts[1].isdigit()
            or int(parts[1]) <= 0
            or parts[2] != msg.external_message_id
        ):
            return decision  # 不以未知账号搜索其它账号的历史白名单。
        scope.append(ShadowDecision.message_id.startswith(f"onebot:{parts[1]}:"))

    sent_at = _parse_naive_iso(msg.sent_at)
    if sent_at is None:
        return decision

    rows = (await session.execute(select(ShadowDecision).where(*scope))).scalars().all()
    covered_ids = {row.message_id for row in rows}
    eligible_sources: list[tuple[str, str | None, bool]] = []
    processing_in_window = False
    for row in rows:
        if row.message_id == msg.message_id or row.external_message_id == msg.external_message_id:
            continue  # 当前消息自己的（重试）记录不是来源。
        if row.kind != "image":
            continue
        detail = _detail_object(row.detail_json)
        row_sent = _parse_naive_iso(detail.get("sent_at"))
        if row_sent is None:
            # 无法定位窗口的历史行（旧格式/清理标记）不作为来源，也不阻断；
            # 但也不能把它当作"审核中"。
            continue
        delta = (sent_at - row_sent).total_seconds()
        if not 0 <= delta <= WALL_PAIR_WINDOW_SECONDS:
            continue
        if detail.get("processing"):
            # P1（主审 f08157d）：未完成图片的"审核中"保护必须覆盖同秒（delta=0）——
            # 只转人工、清空处罚建议；不授予白名单豁免（豁免仍要求图严格先发）。
            processing_in_window = True
            continue
        if delta == 0:
            continue  # 已完成图与文字同秒：无法定序，不授予豁免。
        if row.verdict not in ("allow", "record_only"):
            continue
        # 主审 N01 / N01-R：**跨消息行与单条消息内都收集全部合格来源**——
        # "先遇到一个对诈骗不合格的校园墙图"不等于"没有合格的小程序码来源"；
        # 同一条消息里有多张图（校园墙图 + 带码图）时同理，判定不得依赖附件顺序。
        eligible_sources.extend(_confirmed_sources(_detail_object(row.detail_json)))

    # 主审 N01：在途（已入队但未处理完）图片的保护与"是否存在合格来源"**无关**，
    # 必须独立计算——否则"先有一张校园墙图"就会把待审图保护旁路掉。
    if pending_messages:
        for pending in pending_messages:
            if (
                pending.message_id in covered_ids
                or pending.external_message_id == msg.external_message_id
                or pending.provider != msg.provider
                or pending.external_group_id != msg.external_group_id
                or pending.external_user_id != msg.external_user_id
                or pending.kind != "image"
            ):
                continue
            pending_sent = _parse_naive_iso(pending.sent_at)
            if pending_sent is None:
                continue
            if 0 <= (sent_at - pending_sent).total_seconds() <= WALL_PAIR_WINDOW_SECONDS:
                # 同秒（delta=0）同样视为"审核中"——只保护、不授予豁免。
                processing_in_window = True

    if eligible_sources:
        # 负责人 2026-09-18（第三条口径）：窗口内**诈骗豁免仅限"带小程序码"的来源图**。
        # 主审 N01：①按当前类别在**全部合格来源**里筛选（顺序无关）；
        # ②前缀与结构化布尔必须同时成立（只信前缀会被合成样本绕过）；
        # ③即使诈骗不豁免，**待审图保护仍然独立生效**（不能被来源判定旁路）。
        if "fraud" in effective_categories(decision) and not any(
            mark == WALL_MARK and qr_flag for _text, mark, qr_flag in eligible_sources
        ):
            if processing_in_window:
                return _undecided_predecessor_decision(decision)
            return decision
        return _exempt_decision(decision)
    if processing_in_window:
        return _undecided_predecessor_decision(decision)
    return decision


def _undecided_predecessor_decision(decision: ModerationDecision) -> ModerationDecision:
    """前图审核未完成：只转人工、清空处罚建议，**不授予白名单豁免**。"""
    return decision.model_copy(
        update={
            "verdict": "record_only",
            "recommended_actions": [],
            "reason": "前图审核未完成，配对资格未知，转人工（未授予白名单豁免）",
        }
    )
