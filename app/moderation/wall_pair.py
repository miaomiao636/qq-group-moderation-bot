"""校园墙紧邻文字配对豁免（负责人 2026-09-14 口径；R05/R06/R09 加固）。

场景：成员先发一张带校园墙特征的图片（绝对放行），紧接着发与图内文案相似的
文字（即使文字命中广告/引流规则）。负责人决定：此类紧邻文字**不自动撤回**。

规则参数：
- 窗口：图与文字的**消息发送时间**差 <= 120 秒（2 分钟），且图先发（R06：按
  sent_at 而非落库时间——并发 worker 下处理完成时间会颠倒真实顺序）；
- 相似度：difflib.SequenceMatcher >= 60%；
- 配对：每张图只豁免**一条**文字（R05：原子条件领取 + 绑定被豁免消息 ID）；
- 豁免语义：不撤回、不立案——verdict 降为 record_only 并清空动作建议；
- 范围：仅 ad（广告/引流）类 violation_high 触发；fraud/porn 等不豁免。

R05 事务契约：条件 UPDATE 保证单领取，绑定必须在流水线后续提交和图片重试
中保留。同一事件重试先查已有绑定直接复用豁免，不依赖最后处理标记与判定
落库恰好位于同一事务（流水线包含多次提交）。

R09 结构化校验：白名单来源必须是 vision 通道的**确认放行**结果（category
为空、未降级、不需人工），evidence 以「校园墙白名单」**开头**（否定表述如
「不符合校园墙白名单|文案:…」不构成许可）；「紧邻」要求图与文字之间该成员
没有其他消息。

图内文案与 sent_at 由 t204-v11 起的影子判定 detail 提供；缺失时保守不豁免。
"""

from __future__ import annotations

import difflib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contracts import StandardMessage
from app.moderation.decision import ModerationDecision
from app.runtime.models import ShadowDecision

WALL_PAIR_WINDOW_SECONDS = 120
WALL_PAIR_SIMILARITY = 0.60
WALL_MARK = "校园墙白名单"
WALL_TEXT_MARK = "文案:"
PAIRED_KEY = "wall_paired"
PAIRED_MSG_KEY = "wall_paired_message_id"


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


def similarity(a: str, b: str) -> float:
    """归一化相似度（SequenceMatcher，忽略大小写与空白差异）。"""
    if not a or not b:
        return 0.0

    def norm(s: str) -> str:
        return "".join(str(s).lower().split())

    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def _detail_object(detail_json: str) -> dict[str, Any]:
    try:
        detail = json.loads(detail_json)
    except (TypeError, ValueError):
        return {}
    return detail if isinstance(detail, dict) else {}


def _wall_source_from_detail(detail_json: str) -> tuple[str | None, dict[str, Any]]:
    """R09: 从影子判定 detail 取（结构化校验后的）图内文案与完整 detail。

    返回 (文案或 None, detail)。None=不可作为白名单来源（已消费/非放行/无文案）。
    """
    detail = _detail_object(detail_json)
    if detail.get(PAIRED_KEY) or detail.get("processing") or detail.get("evidence_vetoes"):
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


def _exempt_decision(decision: ModerationDecision, ratio: float | None) -> ModerationDecision:
    explanation = (
        f"与2分钟内同成员校园墙图文案相似度{ratio:.0%}"
        if ratio is not None
        else "同事件重试复用已确认配对"
    )
    return decision.model_copy(
        update={
            "verdict": "record_only",
            "recommended_actions": [],
            "reason": (
                f"校园墙紧邻文字豁免：{explanation}（负责人 2026-09-14 口径），不撤回，转记录"
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
    """紧邻校园墙图的相似文字豁免（见模块 docstring）。

    必须在 orchestrate_actions 之前调用；命中时返回 record_only 版本并原子
    领取配对图名额（与流水线事务同提交/同回滚）。
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
    binding_key = event_key or decision.message_id
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
    bound_message = case(
        (
            func.json_valid(ShadowDecision.detail_json),
            func.json_extract(ShadowDecision.detail_json, f"$.{PAIRED_MSG_KEY}"),
        ),
        else_=None,
    )

    # R05 幂等：同事件重试——已有图绑定本消息则直接复用豁免（不限窗口，绑定即终态）
    bound_row = (
        await session.execute(
            select(ShadowDecision)
            .where(
                *scope,
                ShadowDecision.kind == "image",
                # 旧 external ID 绑定仅在同账号 source 范围内兼容；新绑定用完整键。
                or_(bound_message == binding_key, bound_message == decision.message_id),
            )
            .limit(1)
        )
    ).scalar()
    if bound_row is not None:
        return _exempt_decision(decision, None)
    if sent_at is None:
        return decision

    candidates = (await session.execute(select(ShadowDecision).where(*scope))).scalars().all()
    predecessors: list[tuple[datetime, ShadowDecision | None, dict[str, Any], str]] = []
    chronology_unknown = False
    covered_events = {row.message_id for row in candidates}
    for row in candidates:
        if row.message_id == msg.message_id or row.external_message_id == msg.external_message_id:
            continue  # 当前消息自己的 processing/重试记录不是中间消息。
        detail = _detail_object(row.detail_json)
        row_sent = _parse_naive_iso(detail.get("sent_at"))
        if row_sent is None:
            if detail.get("purged") is True and detail.get("reason") == "raw_retention_expired":
                # Older cleanup versions removed this timestamp. Their explicitly
                # expired originals must not poison every future two-minute window.
                continue
            chronology_unknown = True
            continue  # 不猜顺序，也不能在这里旁路已知前图的处理中保护。
        delta = (sent_at - row_sent).total_seconds()
        if 0 <= delta <= WALL_PAIR_WINDOW_SECONDS:
            predecessors.append((row_sent, row, detail, row.kind))
    for pending in pending_messages:
        if (
            pending.message_id in covered_events
            or pending.external_message_id == msg.external_message_id
            or pending.provider != msg.provider
            or pending.external_group_id != msg.external_group_id
            or pending.external_user_id != msg.external_user_id
        ):
            continue
        pending_sent = _parse_naive_iso(pending.sent_at)
        if pending_sent is None:
            chronology_unknown = True
            continue
        if 0 <= (sent_at - pending_sent).total_seconds() <= WALL_PAIR_WINDOW_SECONDS:
            predecessors.append((pending_sent, None, {"processing": True}, pending.kind))
    if not predecessors:
        return decision
    predecessors.sort(key=lambda item: item[0], reverse=True)
    image_sent, source_row, detail, kind = predecessors[0]
    if any(
        stamp == image_sent and candidate_kind == "image" and candidate_detail.get("processing")
        for stamp, _, candidate_detail, candidate_kind in predecessors
    ):
        return decision.model_copy(
            update={
                "verdict": "record_only",
                "recommended_actions": [],
                "reason": "紧邻前图审核未完成，配对资格未知，转人工（未授予白名单豁免）",
            }
        )
    if chronology_unknown:
        return decision  # 不确定的旧记录仍不能作为白名单授权。
    if image_sent == sent_at or (len(predecessors) > 1 and predecessors[1][0] == image_sent):
        return decision  # 同秒且没有可靠序号，不以完成顺序推断先后。
    if kind != "image":
        return decision
    if source_row is None or source_row.verdict not in ("allow", "record_only"):
        return decision
    wall_text, _ = _wall_source_from_detail(source_row.detail_json)
    if not wall_text:
        return decision
    ratio = similarity(wall_text, msg.text)
    if ratio < WALL_PAIR_SIMILARITY:
        return decision
    # 同一原件的资格/绑定自读取以来不得改变。仅原子修改绑定字段，不覆写其它元数据。
    result = await session.execute(
        update(ShadowDecision)
        .where(
            ShadowDecision.id == source_row.id,
            ShadowDecision.detail_json == source_row.detail_json,
        )
        .values(
            detail_json=func.json_set(
                ShadowDecision.detail_json,
                f"$.{PAIRED_KEY}",
                func.json("true"),
                f"$.{PAIRED_MSG_KEY}",
                binding_key,
            )
        )
        .execution_options(synchronize_session=False)
    )
    if (getattr(result, "rowcount", 0) or 0) != 1:
        return decision  # 不越过真正前驱，不能退回更早图片寻找豁免。
    return _exempt_decision(decision, ratio)
