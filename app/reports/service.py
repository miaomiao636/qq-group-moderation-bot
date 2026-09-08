"""日报/周报构建（T-401）。

发送渠道：管理后台页面 + 结构化日志（负责人此前未确认QQ私聊渠道，默认网页后台，
待确认事项保留）。调度（09:00/21:00、周一09:10）由部署层（Windows计划任务/T-404）驱动，
应用内不内置常驻定时线程（避免与Windows Service监督重叠）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cases.models import Case, ViolationRecord
from app.models import ActionLog, ProcessedEvent


async def _count(session: AsyncSession, stmt: Any) -> int:
    result = await session.execute(stmt)
    return int(result.scalar_one())


async def build_daily(session: AsyncSession, day: datetime | None = None) -> dict[str, Any]:
    """构建日报（默认昨天00:00~24:00 UTC窗口）。"""
    day = day or (datetime.now(UTC) - timedelta(days=1))
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)

    messages = await _count(
        session,
        select(func.count())
        .select_from(ProcessedEvent)
        .where(ProcessedEvent.processed_at >= start, ProcessedEvent.processed_at < end),
    )
    violations = await _count(
        session,
        select(func.count())
        .select_from(ViolationRecord)
        .where(ViolationRecord.created_at >= start, ViolationRecord.created_at < end),
    )
    actions_ok = await _count(
        session,
        select(func.count())
        .select_from(ActionLog)
        .where(ActionLog.created_at >= start, ActionLog.created_at < end, ActionLog.ok.is_(True)),
    )
    actions_fail = await _count(
        session,
        select(func.count())
        .select_from(ActionLog)
        .where(ActionLog.created_at >= start, ActionLog.created_at < end, ActionLog.ok.is_(False)),
    )
    pending_cases = await _count(
        session, select(func.count()).select_from(Case).where(Case.status == "PENDING_REVIEW")
    )
    closed_cases = await _count(
        session,
        select(func.count())
        .select_from(Case)
        .where(Case.closed_at.is_not(None), Case.closed_at >= start, Case.closed_at < end),
    )
    return {
        "report_type": "daily",
        "date": f"{start:%Y-%m-%d}",
        "messages_processed": messages,
        "violations_recorded": violations,
        "actions_ok": actions_ok,
        "actions_failed": actions_fail,
        "cases_pending_review": pending_cases,
        "cases_closed": closed_cases,
        "generated_at": datetime.now(UTC).isoformat(),
    }


async def build_weekly(session: AsyncSession, week_end: datetime | None = None) -> dict[str, Any]:
    """构建周报（默认截至昨天的7天窗口）。"""
    end_day = week_end or (datetime.now(UTC) - timedelta(days=1))
    end = end_day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    start = end - timedelta(days=7)

    violations = await _count(
        session,
        select(func.count())
        .select_from(ViolationRecord)
        .where(ViolationRecord.created_at >= start, ViolationRecord.created_at < end),
    )
    messages = await _count(
        session,
        select(func.count())
        .select_from(ProcessedEvent)
        .where(ProcessedEvent.processed_at >= start, ProcessedEvent.processed_at < end),
    )
    pending_cases = await _count(
        session, select(func.count()).select_from(Case).where(Case.status == "PENDING_REVIEW")
    )
    return {
        "report_type": "weekly",
        "range": f"{start:%Y-%m-%d} ~ {end:%Y-%m-%d}",
        "messages_processed": messages,
        "violations_recorded": violations,
        "cases_pending_review": pending_cases,
        "generated_at": datetime.now(UTC).isoformat(),
    }


async def pending_manual_review(session: AsyncSession) -> list[dict[str, Any]]:
    """待人工处理清单（案件 + 日报引用）。"""
    stmt = select(Case).where(Case.status == "PENDING_REVIEW").order_by(Case.created_at)
    result = await session.execute(stmt)
    return [
        {
            "case_no": c.case_no,
            "group_openid": c.group_openid,
            "member_openid": c.member_openid,
            # T-305 中立身份（与旧镜像并存，contract 阶段旧键移除）
            "provider": c.provider or "qq_official",
            "external_group_id": c.external_group_id or c.group_openid,
            "external_user_id": c.external_user_id or c.member_openid,
            "created_at": c.created_at.isoformat(),
        }
        for c in result.scalars()
    ]
