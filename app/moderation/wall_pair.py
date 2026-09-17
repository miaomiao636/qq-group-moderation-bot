"""校园墙图后广告文字豁免（负责人 2026-09-14 口径；2026-09-17 放宽为口径 C）。

场景：成员先发一张带校园墙特征的图片（视觉确认放行），随后（2 分钟内）发送
广告文字——负责人 2026-09-17 决定（口径 C）：**不再要求文字与图内文案相似**
（也不再要求「紧邻」与「一图一条」），图后 2 分钟内该成员的任意广告文字都
不自动撤回。

规则参数（口径 C）：
- 窗口：图与文字的**消息发送时间**差 <= 120 秒，且图先发（R06：按 sent_at
  而非落库时间——并发 worker 下处理完成时间会颠倒真实顺序；同秒无法定序，
  不授予豁免）；
- 来源：同 provider/群/成员的**视觉确认**校园墙图（R09 结构化校验保留：
  全部 vision 结果 category 为空、未降级、不需人工、evidence 以「校园墙白名单」
  开头且含非空图内文案；带 processing/evidence_vetoes 的行不作来源）；
- 来源图的判定必须为 allow/record_only；
- 范围：仅 ad（广告/引流）类 violation_high 触发；fraud/porn 等不豁免；
- 豁免语义：不撤回、不立案——verdict 降为 record_only 并清空动作建议；
- 无状态：不写绑定、不消耗来源图、窗口内多条文字可共享同一张图；同事件重试
  自然幂等（重新计算得到同一豁免）；
- 图审核未完成时不处罚也不豁免：保守降为 record_only 转人工（R-108 保护保留）。

历史兼容：旧版写入的 wall_paired / wall_paired_message_id 绑定字段不再读写；
历史已绑定过的校园墙图同样是有效来源。

图内文案与 sent_at 由 t204-v11 起的影子判定 detail 提供；缺失时保守不豁免。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contracts import StandardMessage
from app.moderation.decision import ModerationDecision
from app.runtime.models import ShadowDecision

WALL_PAIR_WINDOW_SECONDS = 120
WALL_MARK = "校园墙白名单"
WALL_TEXT_MARK = "文案:"


def extract_wall_text(evidence: str) -> str | None:
    """R09: 仅接受以「校园墙白名单」**开头**的 evidence；返回文案（空串=图无文案）。

    否定表述（如「不符合校园墙白名单|文案:…」）与其它位置出现的子串均不构成许可。
    """
    evidence = evidence.strip()
    if evidence != WALL_MARK and not evidence.startswith(WALL_MARK + "|"):
        return None
    if WALL_TEXT_MARK in evidence:
        return evidence.split(WALL_TEXT_MARK, 1)[1].strip()
    return ""


def _detail_object(detail_json: str) -> dict[str, Any]:
    try:
        detail = json.loads(detail_json)
    except (TypeError, ValueError):
        return {}
    return detail if isinstance(detail, dict) else {}


def _wall_source_from_detail(detail_json: str) -> tuple[str | None, dict[str, Any]]:
    """R09: 从影子判定 detail 取（结构化校验后的）图内文案与完整 detail。

    返回 (文案或 None, detail)。None=不可作为豁免来源（非放行/无文案/未完成）。
    口径 C（2026-09-17）起不再有"已消费"概念：历史绑定字段不影响来源复用。
    """
    detail = _detail_object(detail_json)
    if detail.get("processing") or detail.get("evidence_vetoes"):
        return None, detail
    results = detail.get("ai_results")
    if not isinstance(results, list) or any(not isinstance(r, dict) for r in results):
        return None, detail
    visions = [r for r in results if r.get("source") == "vision"]
    if not visions:
        return None, detail
    # 使用 pipeline 的 AIModerationResult.model_dump 契约。缺字段不是确认正常，
    # 任一视觉结果仍有疑问时，不能挑出另一条白名单证据消掉尚未解决的矛盾。
    if any(
        "category" not in r
        or r["category"] is not None
        or r.get("needs_review") is not False
        or r.get("degraded_reason") != ""
        for r in visions
    ):
        return None, detail
    for result in visions:
        evidence = result.get("evidence")
        if isinstance(evidence, str) and (wall_text := extract_wall_text(evidence)):
            return wall_text, detail
    return None, detail


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
    """图后 2 分钟广告文字豁免（口径 C，见模块 docstring）。

    必须在 orchestrate_actions 之前调用；命中时返回 record_only 版本。
    无状态重算：不写绑定、不消耗来源图，重试幂等。
    """
    if decision.verdict != "violation_high" or decision.category != "ad":
        return decision
    if msg.kind != "text" or not msg.text.strip():
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
    confirmed_wall: str | None = None
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
        if not 0 < delta <= WALL_PAIR_WINDOW_SECONDS:
            continue
        if detail.get("processing"):
            processing_in_window = True
            continue
        if row.verdict not in ("allow", "record_only"):
            continue
        wall_text, _ = _wall_source_from_detail(row.detail_json)
        if wall_text:
            confirmed_wall = wall_text
            break

    if confirmed_wall is None:
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
            if 0 < (sent_at - pending_sent).total_seconds() <= WALL_PAIR_WINDOW_SECONDS:
                processing_in_window = True

    if confirmed_wall is not None:
        return _exempt_decision(decision)
    if processing_in_window:
        return decision.model_copy(
            update={
                "verdict": "record_only",
                "recommended_actions": [],
                "reason": "前图审核未完成，配对资格未知，转人工（未授予白名单豁免）",
            }
        )
    return decision
