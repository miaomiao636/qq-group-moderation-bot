"""管理后台统计大盘聚合（可视化增强）。

纯服务端 SQL 聚合，不引入前端框架/CDN，符合 D-014 离线隐私原则。
所有数据仅来自本机数据库，不外发。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cases.models import Case, ViolationRecord
from app.moderation.ai import AIUsageLog
from app.moderation.feedback import POSITIVE_LABELS, FeedbackRecord
from app.runtime.models import ShadowDecision


async def _count(session: AsyncSession, stmt: Any) -> int:
    return int((await session.execute(stmt)).scalar_one())


async def _group_counts(
    session: AsyncSession, column: Any, where: Any | None = None
) -> list[tuple[str, int]]:
    """按某列分组计数，返回按数量降序的 (值, 数量) 列表。"""
    stmt = select(column, func.count()).group_by(column)
    if where is not None:
        stmt = stmt.where(where)
    rows = (await session.execute(stmt)).all()
    return sorted(((str(r[0]), int(r[1])) for r in rows), key=lambda x: x[1], reverse=True)


async def build_stats(session: AsyncSession) -> dict[str, Any]:
    """聚合后台大盘所需的全部指标。"""
    total_shadow = await _count(session, select(func.count()).select_from(ShadowDecision))
    total_violations = await _count(
        session, select(func.count()).select_from(ViolationRecord)
    )
    pending_cases = await _count(
        session, select(func.count()).select_from(Case).where(Case.status == "PENDING_REVIEW")
    )
    total_feedback = await _count(
        session, select(func.count()).select_from(FeedbackRecord)
    )
    total_ai = await _count(session, select(func.count()).select_from(AIUsageLog))
    ai_failed = await _count(
        session, select(func.count()).select_from(AIUsageLog).where(AIUsageLog.ok.is_(False))
    )

    verdicts = await _group_counts(session, ShadowDecision.verdict)
    categories = await _group_counts(
        session, ShadowDecision.category, ShadowDecision.verdict == "violation_high"
    )
    feedback_labels = await _group_counts(session, FeedbackRecord.label)
    candidate_status = await _group_counts(session, RuleCandidate_status())

    # 最近 7 天每日影子判定量（按本机 UTC 自然日）
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    last7: list[tuple[str, int]] = []
    for i in range(6, -1, -1):
        start = today - timedelta(days=i)
        end = start + timedelta(days=1)
        cnt = await _count(
            session,
            select(func.count())
            .select_from(ShadowDecision)
            .where(ShadowDecision.created_at >= start, ShadowDecision.created_at < end),
        )
        last7.append((f"{start:%m-%d}", cnt))

    # AI 按模型拆分：调用数、成功数、平均延迟、总费用
    ai_rows = (
        await session.execute(
            select(
                AIUsageLog.model_id,
                func.count(),
                func.sum(case((AIUsageLog.ok.is_(True), 1), else_=0)),
                func.avg(AIUsageLog.latency_ms),
                func.sum(AIUsageLog.cost_cents),
            ).group_by(AIUsageLog.model_id)
        )
    ).all()
    ai_by_model: list[dict[str, Any]] = []
    for model, total, ok_count, avg_ms, cost in ai_rows:
        ai_by_model.append(
            {
                "model": str(model) or "(未知)",
                "calls": int(total),
                "ok": int(ok_count or 0),
                "fail": int(total) - int(ok_count or 0),
                "avg_ms": round(float(avg_ms or 0), 1),
                "cost_cents": int(cost or 0),
            }
        )
    ai_by_model.sort(key=lambda x: x["calls"], reverse=True)

    # AI 与人工标注一致率：以人工反馈为临时真值，比对同消息的影子判定
    agreement = await _agreement(session)

    return {
        "totals": {
            "shadow": total_shadow,
            "violations": total_violations,
            "pending_cases": pending_cases,
            "feedback": total_feedback,
            "ai_calls": total_ai,
            "ai_failed": ai_failed,
        },
        "verdicts": verdicts,
        "categories": categories,
        "feedback_labels": feedback_labels,
        "candidate_status": candidate_status,
        "last7": last7,
        "ai_by_model": ai_by_model,
        "agreement": agreement,
        "generated_at": datetime.now(UTC).isoformat(),
    }


def RuleCandidate_status() -> Any:
    """延迟导入，避免循环依赖；返回 RuleCandidate.status 列。"""
    from app.moderation.feedback import RuleCandidate

    return RuleCandidate.status


async def _agreement(session: AsyncSession) -> dict[str, Any]:
    """以人工反馈为临时真值，计算影子判定与其一致率（仅统计能匹配到影子记录的消息）。"""
    feedbacks = (
        (await session.execute(select(FeedbackRecord))).scalars().all()
    )
    if not feedbacks:
        return {"total": 0, "agree": 0, "rate": 0.0}
    message_ids = {f.message_id for f in feedbacks}
    shadows = (
        (
            await session.execute(
                select(ShadowDecision).where(ShadowDecision.message_id.in_(message_ids))
            )
        )
        .scalars()
        .all()
    )
    shadow_by_msg = {s.message_id: s for s in shadows}
    total = 0
    agree = 0
    for f in feedbacks:
        shadow = shadow_by_msg.get(f.message_id)
        if shadow is None:
            continue
        total += 1
        truth_positive = f.label in POSITIVE_LABELS
        ai_positive = shadow.verdict == "violation_high"
        if truth_positive == ai_positive:
            agree += 1
    rate = round(agree / total, 3) if total else 0.0
    return {"total": total, "agree": agree, "rate": rate}
