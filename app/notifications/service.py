"""Atomic SQLite notification outbox operations, with caller-owned transactions.

Every function flushes but NEVER commits. The dispatcher must commit a successful
claim before sending, then complete with that token in a new short transaction.
Only fixed collector-controlled summaries belong here, not source message text.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, case, delete, exists, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.notifications.contracts import DeliveryResult
from app.notifications.models import NotificationDelivery, NotificationNotice

LEASE_SECONDS = 120
MAX_ATTEMPTS = 3


def _now(value: datetime | None = None) -> datetime:
    value = value or datetime.now(UTC)
    # SQLite stores UTC without timezone information. Treat its naive values as
    # UTC, while converting an explicitly supplied offset rather than dropping it.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _identifier(value: str, *, maximum: int, name: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or not re.fullmatch(r"[A-Za-z0-9_.:-]+", value)
    ):
        raise ValueError(f"invalid {name}")
    return value


def _channels(channels: Sequence[str]) -> tuple[str, ...]:
    if isinstance(channels, str) or len(channels) > 16:
        raise ValueError("channels must be a bounded sequence")
    return tuple(
        dict.fromkeys(_identifier(channel, maximum=32, name="channel") for channel in channels)
    )


def _pending_filter() -> ColumnElement[bool]:
    return or_(
        NotificationDelivery.status == "PENDING",
        and_(
            NotificationDelivery.status == "FAILED",
            NotificationDelivery.next_attempt_at.is_not(None),
        ),
    )


async def _cancel_unsent(session: AsyncSession, notice_id: int, now: datetime, code: str) -> None:
    await session.execute(
        update(NotificationDelivery)
        .where(
            NotificationDelivery.notice_id == notice_id,
            _pending_filter(),
        )
        .values(
            status="SKIPPED",
            next_attempt_at=None,
            claim_token="",
            lease_expires_at=None,
            error_code=code,
            updated_at=now,
        )
    )


async def _enqueue(
    session: AsyncSession, notice_id: int, channels: tuple[str, ...], audience: str, now: datetime
) -> None:
    for channel in channels:
        await session.execute(
            insert(NotificationDelivery)
            .values(
                notice_id=notice_id,
                channel=channel,
                audience=audience,
                status="PENDING",
                attempts=0,
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["notice_id", "channel", "audience"])
        )


async def create_notice(
    session: AsyncSession,
    *,
    event_key: str,
    kind: str,
    severity: str,
    subject: str,
    body: str,
    channels: Sequence[str],
    now: datetime | None = None,
) -> NotificationNotice:
    """Insert one immutable event and its primary deliveries, atomically/idempotently."""
    event_key = _identifier(event_key, maximum=200, name="event_key")
    kind = _identifier(kind, maximum=64, name="kind")
    selected = _channels(channels)
    if severity not in ("page", "ticket"):
        raise ValueError("invalid notification severity")
    if (
        not isinstance(subject, str)
        or not 1 <= len(subject.strip()) <= 160
        or any(ord(char) < 32 for char in subject)
    ):
        raise ValueError("subject must be a bounded single-line summary")
    if (
        not isinstance(body, str)
        or not 1 <= len(body.strip()) <= 2000
        or any(ord(char) < 32 and char != "\n" for char in body)
    ):
        raise ValueError("body must be a bounded controlled summary")
    timestamp = _now(now)
    await session.flush()
    notice = (
        await session.execute(
            insert(NotificationNotice)
            .values(
                event_key=event_key,
                kind=kind,
                severity=severity,
                subject=subject.strip(),
                body=body.strip(),
                created_at=timestamp,
            )
            .on_conflict_do_nothing(index_elements=["event_key"])
            .returning(NotificationNotice)
        )
    ).scalar_one_or_none()
    if notice is None:
        # A repeated event never changes content or adds recipients after ack.
        return (
            await session.scalars(
                select(NotificationNotice).where(NotificationNotice.event_key == event_key)
            )
        ).one()
    await _enqueue(session, notice.id, selected, "primary", timestamp)
    await session.flush()
    return notice


async def acknowledge_notice(
    session: AsyncSession, notice_id: int, *, actor: str, now: datetime | None = None
) -> bool:
    """Acknowledge notification only; cannot mutate cases, rules or punishment."""
    if (
        not isinstance(actor, str)
        or not 1 <= len(actor.strip()) <= 64
        or any(ord(char) < 32 for char in actor)
    ):
        raise ValueError("invalid notification acknowledgement actor")
    timestamp = _now(now)
    changed = await session.scalar(
        update(NotificationNotice)
        .where(
            NotificationNotice.id == notice_id,
            NotificationNotice.acknowledged_at.is_(None),
            NotificationNotice.resolved_at.is_(None),
        )
        .values(acknowledged_at=timestamp, acknowledged_by=actor.strip())
        .returning(NotificationNotice.id)
    )
    if changed is None:
        return False
    await _cancel_unsent(session, notice_id, timestamp, "notice_acknowledged")
    await session.flush()
    return True


async def resolve_notice(
    session: AsyncSession, notice_id: int, *, now: datetime | None = None
) -> bool:
    """End an incident; the collector separately creates a recovery notification."""
    timestamp = _now(now)
    changed = await session.scalar(
        update(NotificationNotice)
        .where(
            NotificationNotice.id == notice_id,
            NotificationNotice.resolved_at.is_(None),
        )
        .values(resolved_at=timestamp)
        .returning(NotificationNotice.id)
    )
    if changed is None:
        return False
    await _cancel_unsent(session, notice_id, timestamp, "notice_resolved")
    await session.flush()
    return True


async def enqueue_escalations(
    session: AsyncSession,
    *,
    channels: Sequence[str],
    after_seconds: int = 900,
    now: datetime | None = None,
) -> int:
    """Escalate each outstanding page once to backup; return the notice count."""
    selected = _channels(channels)
    if not 1 <= after_seconds <= 86400:
        raise ValueError("invalid escalation interval")
    if not selected:
        await session.flush()
        return 0
    timestamp = _now(now)
    identifiers = list(
        (
            await session.scalars(
                update(NotificationNotice)
                .where(
                    NotificationNotice.severity == "page",
                    NotificationNotice.acknowledged_at.is_(None),
                    NotificationNotice.resolved_at.is_(None),
                    NotificationNotice.escalated_at.is_(None),
                    NotificationNotice.created_at <= timestamp - timedelta(seconds=after_seconds),
                )
                .values(escalated_at=timestamp)
                .returning(NotificationNotice.id)
                .execution_options(synchronize_session="fetch")
            )
        ).all()
    )
    for notice_id in identifiers:
        await _enqueue(session, notice_id, selected, "backup", timestamp)
    await session.flush()
    return len(identifiers)


async def claim_delivery(
    session: AsyncSession, *, now: datetime | None = None, channel: str | None = None
) -> NotificationDelivery | None:
    """Atomically claim FIFO work, optionally within one exact channel.

    An empty channel queue never borrows another channel's work. The caller must
    commit before external I/O; omitting the filter preserves global FIFO order.
    """
    timestamp = _now(now)
    if channel is not None:
        channel = _identifier(channel, maximum=32, name="channel")
    eligible = and_(
        _pending_filter(),
        NotificationDelivery.next_attempt_at.is_not(None),
        NotificationDelivery.next_attempt_at <= timestamp,
        NotificationDelivery.attempts < MAX_ATTEMPTS,
    )
    if channel is not None:
        eligible = and_(eligible, NotificationDelivery.channel == channel)
    candidate = (
        select(NotificationDelivery.id)
        .join(NotificationNotice)
        .where(
            eligible,
            NotificationNotice.acknowledged_at.is_(None),
            NotificationNotice.resolved_at.is_(None),
        )
        .order_by(NotificationDelivery.id)
        .limit(1)
        .scalar_subquery()
    )
    delivery = (
        await session.scalars(
            update(NotificationDelivery)
            .where(
                NotificationDelivery.id == candidate,
                eligible,
            )
            .values(
                status="SENDING",
                attempts=NotificationDelivery.attempts + 1,
                next_attempt_at=None,
                lease_expires_at=timestamp + timedelta(seconds=LEASE_SECONDS),
                claim_token=uuid.uuid4().hex,
                error_code="",
                updated_at=timestamp,
            )
            .returning(NotificationDelivery),
            execution_options={"populate_existing": True},
        )
    ).one_or_none()
    await session.flush()
    return delivery


async def complete_delivery(
    session: AsyncSession,
    delivery_id: int,
    *,
    claim_token: str,
    result: DeliveryResult,
    now: datetime | None = None,
) -> bool:
    """Only the active unexpired owner can settle a send, with bounded safe retries."""
    timestamp = _now(now)
    if not claim_token:
        return False
    notice_open = exists().where(
        NotificationNotice.id == NotificationDelivery.notice_id,
        NotificationNotice.acknowledged_at.is_(None),
        NotificationNotice.resolved_at.is_(None),
    )
    retry_at = (
        case(
            (
                and_(NotificationDelivery.attempts == 1, notice_open),
                timestamp + timedelta(seconds=60),
            ),
            (
                and_(NotificationDelivery.attempts == 2, notice_open),
                timestamp + timedelta(seconds=300),
            ),
            else_=None,
        )
        if result.status == "FAILED" and result.retryable
        else None
    )
    changed = await session.scalar(
        update(NotificationDelivery)
        .where(
            NotificationDelivery.id == delivery_id,
            NotificationDelivery.status == "SENDING",
            NotificationDelivery.claim_token == claim_token,
            NotificationDelivery.lease_expires_at > timestamp,
        )
        .values(
            status=result.status,
            error_code=result.error_code,
            claim_token="",
            lease_expires_at=None,
            next_attempt_at=retry_at,
            sent_at=timestamp if result.status == "SENT" else None,
            updated_at=timestamp,
        )
        .returning(NotificationDelivery.id)
        .execution_options(synchronize_session="fetch")
    )
    if changed is None:
        # A late sender cannot turn an expired lease into SENT or a retryable job.
        await session.execute(
            update(NotificationDelivery)
            .where(
                NotificationDelivery.id == delivery_id,
                NotificationDelivery.status == "SENDING",
                NotificationDelivery.claim_token == claim_token,
                NotificationDelivery.lease_expires_at <= timestamp,
            )
            .values(
                status="UNKNOWN",
                error_code="lease_expired",
                claim_token="",
                lease_expires_at=None,
                next_attempt_at=None,
                updated_at=timestamp,
            )
            .execution_options(synchronize_session="fetch")
        )
    await session.flush()
    return changed is not None


async def reap_stale_deliveries(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Expired sends are UNKNOWN: never assume a crash meant nothing was sent."""
    timestamp = _now(now)
    identifiers = list(
        (
            await session.scalars(
                update(NotificationDelivery)
                .where(
                    NotificationDelivery.status == "SENDING",
                    NotificationDelivery.lease_expires_at <= timestamp,
                )
                .values(
                    status="UNKNOWN",
                    error_code="lease_expired",
                    claim_token="",
                    lease_expires_at=None,
                    next_attempt_at=None,
                    updated_at=timestamp,
                )
                .returning(NotificationDelivery.id)
                .execution_options(synchronize_session="fetch")
            )
        ).all()
    )
    await session.flush()
    return len(identifiers)


async def purge_notifications(session: AsyncSession, *, before: datetime) -> dict[str, int]:
    """Delete only aged acknowledged/resolved notices, never open work or watermarks."""
    # Acquire SQLite's writer reservation before selecting targets. Claim/ack/purge
    # cannot interleave a new SENDING delivery between the selection and deletion.
    await session.execute(
        update(NotificationNotice)
        .where(NotificationNotice.id < 0)
        .values(subject=NotificationNotice.subject)
    )
    inflight = exists().where(
        NotificationDelivery.notice_id == NotificationNotice.id,
        NotificationDelivery.status == "SENDING",
    )
    notices_deleted = deliveries_deleted = 0
    while True:
        targets = list(
            (
                await session.scalars(
                    select(NotificationNotice.id)
                    .where(
                        NotificationNotice.created_at < _now(before),
                        or_(
                            NotificationNotice.acknowledged_at.is_not(None),
                            NotificationNotice.resolved_at.is_not(None),
                        ),
                        ~inflight,
                    )
                    .order_by(NotificationNotice.id)
                    .limit(500)
                )
            ).all()
        )
        if not targets:
            break
        # Bound bind parameters; all batches retain the same writer reservation
        # and caller-owned transaction. No partial cleanup commits on failure.
        deliveries = (
            await session.scalars(
                delete(NotificationDelivery)
                .where(NotificationDelivery.notice_id.in_(targets))
                .returning(NotificationDelivery.id)
            )
        ).all()
        notices = (
            await session.scalars(
                delete(NotificationNotice)
                .where(NotificationNotice.id.in_(targets))
                .returning(NotificationNotice.id)
            )
        ).all()
        deliveries_deleted += len(deliveries)
        notices_deleted += len(notices)
    await session.flush()
    return {"notices_deleted": notices_deleted, "deliveries_deleted": deliveries_deleted}
