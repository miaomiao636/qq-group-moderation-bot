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

R05 事务契约：豁免领取（UPDATE）与判定修改在同一流水线事务内，与
mark_processed 同提交——提交前崩溃则整体回滚（图未消费、文字重试可重新
配对）；提交后进程失败无重试；并发 worker 由条件 UPDATE 的 rowcount 保证
单领取。同一事件重试先查已有绑定直接复用豁免（幂等）。

R09 结构化校验：白名单来源必须是 vision 通道的**确认放行**结果（category
为空、未降级、不需人工），evidence 以「校园墙白名单」**开头**（否定表述如
「不符合校园墙白名单|文案:…」不构成许可）；「紧邻」要求图与文字之间该成员
没有其他消息。

图内文案与 sent_at 由 t204-v11 起的影子判定 detail 提供；缺失时保守不豁免。
"""

from __future__ import annotations

import difflib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text, update
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
    if not evidence or not evidence.strip().startswith(WALL_MARK):
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


def _wall_source_from_detail(detail_json: str) -> tuple[str | None, dict[str, Any]]:
    """R09: 从影子判定 detail 取（结构化校验后的）图内文案与完整 detail。

    返回 (文案或 None, detail)。None=不可作为白名单来源（已消费/非放行/无文案）。
    """
    try:
        detail: dict[str, Any] = json.loads(detail_json)
    except (TypeError, ValueError):
        return None, {}
    if detail.get(PAIRED_KEY):
        return None, detail
    for r in detail.get("ai_results") or []:
        if (r.get("source") or "") != "vision":
            continue
        # 明确放行才可作为白名单来源：无违规类别、未降级、不需人工
        if r.get("cat") not in (None, ""):
            continue
        if r.get("nr") or r.get("deg"):
            continue
        text = extract_wall_text(str(r.get("evidence") or ""))
        if text is None:
            continue
        return text, detail
    return None, detail


def _parse_naive_iso(value: object) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


async def _has_intervening_message(
    session: AsyncSession, msg: StandardMessage, *, after: datetime, before: datetime
) -> bool:
    """R09「紧邻」：图与文字之间该成员不得有其他消息（任意类型）。"""
    stmt = (
        select(ShadowDecision.message_id)
        .where(
            ShadowDecision.provider == msg.provider,
            ShadowDecision.external_group_id == msg.external_group_id,
            ShadowDecision.external_user_id == msg.external_user_id,
            ShadowDecision.kind != "image",
            ShadowDecision.created_at > after,
            ShadowDecision.created_at < before,
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar() is not None


def _exempt_decision(decision: ModerationDecision, ratio: float) -> ModerationDecision:
    return decision.model_copy(
        update={
            "verdict": "record_only",
            "recommended_actions": [],
            "reason": (
                f"校园墙紧邻文字豁免：与2分钟内同成员校园墙图文案相似度{ratio:.0%}"
                "（负责人 2026-09-14 口径），不撤回，转记录"
            ),
        }
    )


async def maybe_wall_text_pairing(
    session: AsyncSession, msg: StandardMessage, decision: ModerationDecision
) -> ModerationDecision:
    """紧邻校园墙图的相似文字豁免（见模块 docstring）。

    必须在 orchestrate_actions 之前调用；命中时返回 record_only 版本并原子
    领取配对图名额（与流水线事务同提交/同回滚）。
    """
    if decision.verdict != "violation_high" or decision.category != "ad":
        return decision
    if msg.kind != "text" or not msg.text.strip():
        return decision

    now = datetime.now(UTC).replace(tzinfo=None)
    sent_at = _parse_naive_iso(msg.sent_at)
    cutoff = now - timedelta(seconds=WALL_PAIR_WINDOW_SECONDS * 6)

    # R05 幂等：同事件重试——已有图绑定本消息则直接复用豁免（不限窗口，绑定即终态）
    bound_row = (
        await session.execute(
            select(ShadowDecision)
            .where(
                ShadowDecision.provider == msg.provider,
                ShadowDecision.external_group_id == msg.external_group_id,
                ShadowDecision.external_user_id == msg.external_user_id,
                ShadowDecision.kind == "image",
                text(f"json_extract(detail_json, '$.{PAIRED_MSG_KEY}') = :pmid").bindparams(
                    pmid=decision.message_id
                ),
            )
            .limit(1)
        )
    ).scalar()
    if bound_row is not None:
        return _exempt_decision(decision, 1.0)

    candidates = (
        (
            await session.execute(
                select(ShadowDecision)
                .where(
                    ShadowDecision.provider == msg.provider,
                    ShadowDecision.external_group_id == msg.external_group_id,
                    ShadowDecision.external_user_id == msg.external_user_id,
                    ShadowDecision.kind == "image",
                    ShadowDecision.verdict.in_(("allow", "record_only")),
                    ShadowDecision.created_at >= cutoff,
                    ShadowDecision.created_at <= now,
                )
                .order_by(ShadowDecision.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    for row in candidates:
        wall_text, detail = _wall_source_from_detail(row.detail_json or "{}")
        if not wall_text:
            continue
        # R06 顺序：按消息发送时间校验「先图后文」，缺失则保守不豁免
        img_sent = _parse_naive_iso(detail.get("sent_at"))
        if img_sent is None or sent_at is None:
            continue
        delta = (sent_at - img_sent).total_seconds()
        if not 0 <= delta <= WALL_PAIR_WINDOW_SECONDS:
            continue
        # R09 紧邻：图与文字之间该成员没有其他消息
        if await _has_intervening_message(session, msg, after=row.created_at, before=now):
            continue
        ratio = similarity(wall_text, msg.text)
        if ratio < WALL_PAIR_SIMILARITY:
            continue
        # R05 原子领取：条件更新，rowcount==1 才算领取成功（并发只赢一个）
        detail[PAIRED_KEY] = True
        detail[PAIRED_MSG_KEY] = decision.message_id
        result = await session.execute(
            update(ShadowDecision)
            .where(
                ShadowDecision.message_id == row.message_id,
                ShadowDecision.external_group_id == msg.external_group_id,
                # 尚未消费（JSON1：字段缺失为 NULL，未消费；=1 为已消费）
                text(f"json_extract(detail_json, '$.{PAIRED_KEY}') IS NOT 1"),
            )
            .values(detail_json=json.dumps(detail, ensure_ascii=False))
        )
        if (getattr(result, "rowcount", 0) or 0) != 1:
            continue  # 被并发领取，尝试更早的候选图
        return _exempt_decision(decision, ratio)
    return decision
