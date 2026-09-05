"""数据保留清理（T-402）。

保留期（PROJECT_CONTEXT 既定，阈值来自 settings）：
- 原始消息/媒体（违规记录中的消息快照、事件去重记录）：RAW_RETENTION_DAYS（默认30天）；
- 判断/动作记录（action_logs、违规元数据）：DECISION_RETENTION_DAYS（默认180天）。
违规记录本体保留元数据（类别/置信度/时间），超原始期的消息快照以占位符替换（证据索引不丢）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cases.models import ViolationRecord
from app.config import get_settings
from app.models import ActionLog, ProcessedEvent

_PURGED_SNAPSHOT = '{"purged": true, "reason": "raw_retention_expired"}'


async def purge_expired(session: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    """执行保留期清理，返回各类清理行数。

    SQLite 不保留时区（读取为 naive UTC）， cutoff 一律使用 naive UTC 比较。
    """
    settings = get_settings()
    now = (now or datetime.now(UTC)).replace(tzinfo=None)
    raw_cutoff = now - timedelta(days=settings.raw_retention_days)
    decision_cutoff = now - timedelta(days=settings.decision_retention_days)

    # 1) 事件去重记录：超原始期删除（幂等防线仅在窗口内有意义）
    r1 = await session.execute(
        delete(ProcessedEvent).where(ProcessedEvent.processed_at < raw_cutoff)
    )
    deleted_events = int(getattr(r1, "rowcount", 0) or 0)

    # 2) 违规记录：超原始期的消息快照置为已清理占位（保留元数据供统计）
    stmt = select(ViolationRecord).where(
        ViolationRecord.created_at < raw_cutoff,
        ViolationRecord.message_snapshot_json != _PURGED_SNAPSHOT,
    )
    result = await session.execute(stmt)
    purged_snapshots = 0
    for violation in result.scalars():
        violation.message_snapshot_json = _PURGED_SNAPSHOT
        purged_snapshots += 1

    # 3) 动作日志：超判断期删除
    r3 = await session.execute(delete(ActionLog).where(ActionLog.created_at < decision_cutoff))
    deleted_logs = int(getattr(r3, "rowcount", 0) or 0)

    await session.commit()
    return {
        "processed_events_deleted": deleted_events,
        "violation_snapshots_purged": purged_snapshots,
        "action_logs_deleted": deleted_logs,
    }
