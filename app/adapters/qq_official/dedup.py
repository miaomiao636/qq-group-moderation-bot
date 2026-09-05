"""事件幂等去重（R-102 整改）。

QQ 官方事件可能重复推送（重连、重试），重复处理会造成重复处罚。
最终防线：`processed_events` 表的 message_id + status。
- `PROCESSED`：处理成功完成，重复推送必须跳过；
- `FAILED`：处理失败，**可重试**（下次推送继续处理，覆盖为 PROCESSING）；
- `PROCESSING`：在途（进程崩溃残留），允许重试以恢复。

内存 set 仅作热路径加速，数据库为唯一权威。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ProcessedEvent

_MEMORY_SEEN: set[str] = set()
_MEMORY_MAX = 100_000


async def begin_processing(
    session: AsyncSession, message_id: str, event_type: str = "GROUP_MESSAGE_CREATE"
) -> bool:
    """尝试开始处理一条事件。

    返回 True 表示应继续处理（首次出现，或上次 FAILED/PROCESSING 可重试）；
    返回 False 表示已 PROCESSED，必须跳过。
    """
    if message_id in _MEMORY_SEEN:
        existing = await session.get(ProcessedEvent, message_id)
        if existing is None:
            return False
        return existing.status != "PROCESSED"

    try:
        session.add(
            ProcessedEvent(
                message_id=message_id,
                event_type=event_type,
                status="PROCESSING",
                processed_at=datetime.now(UTC),
            )
        )
        await session.commit()
        _remember(message_id)
        return True
    except IntegrityError:
        await session.rollback()
        existing = await session.get(ProcessedEvent, message_id)
        if existing is None or existing.status == "PROCESSED":
            _remember(message_id)
            return False
        # FAILED / PROCESSING：可重试，标记为在途
        existing.status = "PROCESSING"
        existing.error_message = ""
        existing.processed_at = datetime.now(UTC)
        await session.commit()
        _remember(message_id)
        return True


async def mark_processed(session: AsyncSession, message_id: str) -> None:
    """处理成功后标记 PROCESSED（幂等）。"""
    row = await session.get(ProcessedEvent, message_id)
    if row is None:
        session.add(
            ProcessedEvent(
                message_id=message_id, status="PROCESSED", processed_at=datetime.now(UTC)
            )
        )
    else:
        row.status = "PROCESSED"
        row.error_message = ""
        row.processed_at = datetime.now(UTC)
    await session.commit()


async def mark_failed(session: AsyncSession, message_id: str, error: str) -> None:
    """处理失败标记 FAILED，错误信息落库，允许后续重试。"""
    err = (error or "")[:500]
    row = await session.get(ProcessedEvent, message_id)
    if row is None:
        session.add(
            ProcessedEvent(
                message_id=message_id,
                status="FAILED",
                error_message=err,
                processed_at=datetime.now(UTC),
            )
        )
    else:
        row.status = "FAILED"
        row.error_message = err
        row.processed_at = datetime.now(UTC)
    await session.commit()


def _remember(message_id: str) -> None:
    if len(_MEMORY_SEEN) >= _MEMORY_MAX:
        _MEMORY_SEEN.clear()
    _MEMORY_SEEN.add(message_id)


def reset_memory_cache() -> None:
    """仅供测试使用。"""
    _MEMORY_SEEN.clear()


# 兼容旧调用（R-101 测试仍引用）：成功才标记
async def check_and_mark(
    session: AsyncSession, message_id: str, event_type: str = "GROUP_MESSAGE_CREATE"
) -> bool:
    """旧接口兼容：仅判断是否未处理（不标记成功状态）。

    R-102 后应使用 begin_processing + mark_processed/mark_failed。
    本函数保留以兼容既有测试，语义=begin_processing。
    """
    return await begin_processing(session, message_id, event_type)
