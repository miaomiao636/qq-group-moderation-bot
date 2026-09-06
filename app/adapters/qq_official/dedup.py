"""事件幂等去重（R-103 整改）。

QQ 官方事件可能重复推送（重连、重试），重复处理会造成重复处罚。
最终防线：`processed_events` 表的 message_id + status + lease_token。
- `PROCESSED`：处理成功完成，重复推送必须跳过；
- `FAILED`：临时失败，到 `next_retry_at` 后才可重试；
- `PROCESSING`：租约未过期时不可重复领取，过期后可接管；
- `DEAD`：永久契约错误，不再自动重试。

内存 set 仅作热路径加速，数据库为唯一权威。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ProcessedEvent

_MEMORY_SEEN: set[str] = set()
_MEMORY_MAX = 100_000
DEFAULT_LEASE_SECONDS = 300


@dataclass(frozen=True)
class ProcessingClaim:
    """事件领取结果。"""

    accepted: bool
    token: str
    status: str
    reason: str = ""


async def begin_processing(
    session: AsyncSession,
    message_id: str,
    event_type: str = "GROUP_MESSAGE_CREATE",
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> ProcessingClaim:
    """尝试开始处理一条事件。

    accepted=True 表示调用方持有本次处理租约；后续成功或失败标记必须携带 token。
    """
    if message_id in _MEMORY_SEEN:
        existing = await session.get(ProcessedEvent, message_id)
        if existing is None or existing.status == "PROCESSED":
            return ProcessingClaim(False, "", "PROCESSED", "already processed")

    now = datetime.now(UTC)
    token = secrets.token_urlsafe(24)
    lease_expires_at = now + timedelta(seconds=lease_seconds)
    existing = await session.get(ProcessedEvent, message_id)
    if existing is not None:
        return await _claim_existing(session, message_id, event_type, token, lease_expires_at, now)

    try:
        session.add(
            ProcessedEvent(
                message_id=message_id,
                event_type=event_type,
                status="PROCESSING",
                lease_token=token,
                lease_expires_at=lease_expires_at,
                attempts=1,
                processed_at=datetime.now(UTC),
            )
        )
        await session.commit()
        return ProcessingClaim(True, token, "PROCESSING")
    except IntegrityError:
        await session.rollback()

    return await _claim_existing(session, message_id, event_type, token, lease_expires_at, now)


async def _claim_existing(
    session: AsyncSession,
    message_id: str,
    event_type: str,
    token: str,
    lease_expires_at: datetime,
    now: datetime,
) -> ProcessingClaim:
    """Try to take over an existing retryable or expired event row."""
    stmt = (
        update(ProcessedEvent)
        .where(ProcessedEvent.message_id == message_id)
        .where(
            or_(
                and_(
                    ProcessedEvent.status == "FAILED",
                    or_(
                        ProcessedEvent.next_retry_at.is_(None),
                        ProcessedEvent.next_retry_at <= now,
                    ),
                ),
                and_(
                    ProcessedEvent.status == "PROCESSING",
                    or_(
                        ProcessedEvent.lease_expires_at.is_(None),
                        ProcessedEvent.lease_expires_at <= now,
                    ),
                ),
            )
        )
        .values(
            event_type=event_type,
            status="PROCESSING",
            lease_token=token,
            lease_expires_at=lease_expires_at,
            attempts=ProcessedEvent.attempts + 1,
            next_retry_at=None,
            error_kind="",
            error_message="",
            processed_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(stmt)
    if _rowcount(result) == 1:
        await session.commit()
        return ProcessingClaim(True, token, "PROCESSING")
    await session.commit()

    existing = await session.get(ProcessedEvent, message_id)
    if existing is None:
        return ProcessingClaim(False, "", "UNKNOWN", "claim lost")
    if existing.status == "PROCESSED":
        _remember(message_id)
        return ProcessingClaim(False, "", "PROCESSED", "already processed")
    if existing.status == "DEAD":
        return ProcessingClaim(False, "", "DEAD", existing.error_message)
    if existing.status == "FAILED":
        return ProcessingClaim(False, "", "FAILED", "retry not due")
    return ProcessingClaim(False, "", existing.status, "lease still active")


async def mark_processed(session: AsyncSession, message_id: str, token: str) -> bool:
    """处理成功后标记 PROCESSED；旧令牌不能覆盖新租约。"""
    stmt = (
        update(ProcessedEvent)
        .where(ProcessedEvent.message_id == message_id)
        .where(ProcessedEvent.lease_token == token)
        .values(
            status="PROCESSED",
            error_message="",
            error_kind="",
            next_retry_at=None,
            lease_expires_at=None,
            processed_at=datetime.now(UTC),
        )
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(stmt)
    if _rowcount(result) == 1:
        await session.commit()
        _remember(message_id)
        return True
    await session.rollback()
    return False


async def mark_failed(
    session: AsyncSession,
    message_id: str,
    token: str,
    error: str,
    *,
    error_kind: str = "retryable",
    retry_delay_seconds: int = 0,
) -> bool:
    """处理失败后标记状态；永久错误进入 DEAD，不再自动重试。"""
    err = (error or "")[:500]
    now = datetime.now(UTC)
    permanent = error_kind == "permanent"
    stmt = (
        update(ProcessedEvent)
        .where(ProcessedEvent.message_id == message_id)
        .where(ProcessedEvent.lease_token == token)
        .values(
            status="DEAD" if permanent else "FAILED",
            error_message=err,
            error_kind=error_kind,
            next_retry_at=None if permanent else now + timedelta(seconds=retry_delay_seconds),
            lease_expires_at=None,
            processed_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(stmt)
    if _rowcount(result) == 1:
        await session.commit()
        return True
    await session.rollback()
    return False


def _remember(message_id: str) -> None:
    if len(_MEMORY_SEEN) >= _MEMORY_MAX:
        _MEMORY_SEEN.clear()
    _MEMORY_SEEN.add(message_id)


def reset_memory_cache() -> None:
    """仅供测试使用。"""
    _MEMORY_SEEN.clear()


def _rowcount(result: object) -> int:
    """Return affected rows for SQLAlchemy DML results."""
    return int(getattr(result, "rowcount", 0) or 0)


# 兼容旧调用（R-101 测试仍引用）：成功才标记
async def check_and_mark(
    session: AsyncSession, message_id: str, event_type: str = "GROUP_MESSAGE_CREATE"
) -> bool:
    """旧接口兼容：仅判断是否可处理（不标记成功状态）。"""
    claim = await begin_processing(session, message_id, event_type)
    return claim.accepted
