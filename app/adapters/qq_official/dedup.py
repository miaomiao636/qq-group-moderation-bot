"""事件幂等去重（T-102）。

QQ 官方事件可能重复推送（重连、重试），重复处理会造成重复处罚。
以 `processed_events` 表的 message_id 主键为最终防线，插入冲突即视为已处理；
内存 set 作为热路径加速。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ProcessedEvent

_MEMORY_SEEN: set[str] = set()
_MEMORY_MAX = 100_000


async def check_and_mark(
    session: AsyncSession, message_id: str, event_type: str = "GROUP_MESSAGE_CREATE"
) -> bool:
    """标记事件为已处理。

    返回 True 表示首次出现（应继续处理）；False 表示重复事件（必须跳过）。
    """
    if message_id in _MEMORY_SEEN:
        return False
    try:
        await session.execute(
            insert(ProcessedEvent).values(
                message_id=message_id, event_type=event_type, processed_at=datetime.now(UTC)
            )
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        _remember(message_id)
        return False
    _remember(message_id)
    return True


def _remember(message_id: str) -> None:
    if len(_MEMORY_SEEN) >= _MEMORY_MAX:
        # 简单防膨胀：清空重建（数据库仍是最终防线）
        _MEMORY_SEEN.clear()
    _MEMORY_SEEN.add(message_id)


def reset_memory_cache() -> None:
    """仅供测试使用。"""
    _MEMORY_SEEN.clear()
