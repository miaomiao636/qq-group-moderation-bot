"""供全部入站Adapter共用的事件租约与幂等处理。

T-305将实现从QQ官方Adapter上移到中立核心。T-306会在OneBot的
``self_id``契约定稿后，把持久化键扩展为``provider + self_id + message_id``。
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
    provider: str | None = None,
) -> ProcessingClaim:
    """尝试开始处理一条事件。

    T-306：``message_id`` 允许传入组合去重键（如 ``onebot:{self_id}:{message_id}``）；
    ``provider`` 记录事件来源通道（官方路径缺省不变）。
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
                provider=provider or "qq_official",
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
    stmt = (
        update(ProcessedEvent)
        .where(ProcessedEvent.message_id == message_id)
        .where(
            or_(
                ProcessedEvent.status == "PENDING",  # 入队前预写，worker领取
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
    """处理成功后标记PROCESSED；旧令牌不能覆盖新租约。"""
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
    """处理失败后标记状态；永久错误进入DEAD。"""
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


async def reap_stuck_leases(session: AsyncSession) -> tuple[int, int]:
    """启动清理：将过期PROCESSING标记为FAILED，统计遗留PENDING。

    返回 (reaped_stuck, pending_count)。
    T-306审查发现：worker硬退出后租约永久卡在PROCESSING，无人重入即僵尸。
    """
    now = datetime.now(UTC)
    # 过期PROCESSING → FAILED（可重试）
    reaped = await session.execute(
        update(ProcessedEvent)
        .where(ProcessedEvent.status == "PROCESSING")
        .where(
            or_(
                ProcessedEvent.lease_expires_at.is_(None),
                ProcessedEvent.lease_expires_at <= now,
            )
        )
        .values(
            status="FAILED",
            error_kind="lease_expired",
            error_message="租约过期，worker可能硬退出",
            next_retry_at=now,
            lease_expires_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    reaped_count = _rowcount(reaped)
    # 统计遗留PENDING（入队但worker未处理即重启）
    from sqlalchemy import func, select

    pending_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ProcessedEvent)
                .where(ProcessedEvent.status == "PENDING")
            )
        ).scalar_one()
    )
    await session.commit()
    return reaped_count, pending_count


async def mark_pending(
    session: AsyncSession,
    message_id: str,
    event_type: str = "GROUP_MESSAGE_CREATE",
    *,
    provider: str = "onebot",
) -> bool:
    """入队前持久化事件为PENDING状态（防进程重启丢消息）。

    已存在则不重复插入（幂等）。返回True表示新插入。
    """
    existing = await session.get(ProcessedEvent, message_id)
    if existing is not None:
        return False
    try:
        session.add(
            ProcessedEvent(
                message_id=message_id,
                event_type=event_type,
                status="PENDING",
                attempts=0,
                processed_at=datetime.now(UTC),
                provider=provider,
            )
        )
        await session.commit()
        return True
    except IntegrityError:
        await session.rollback()
        return False


def _rowcount(result: object) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


async def check_and_mark(
    session: AsyncSession, message_id: str, event_type: str = "GROUP_MESSAGE_CREATE"
) -> bool:
    """旧接口兼容：仅判断是否可处理（不标记成功状态）。"""
    claim = await begin_processing(session, message_id, event_type)
    return claim.accepted
