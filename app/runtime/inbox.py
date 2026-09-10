"""SQLite durable OneBot inbox, separate from moderation/action idempotency.

Only committed events are scheduled. A unique transport event key prevents replay;
workers use renewable leases and bounded retries. Completed raw payloads are erased
immediately, failed raw payloads expire with the configured original-data retention.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import (
    DateTime,
    Integer,
    String,
    Text,
    and_,
    delete,
    func,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

MAX_PAYLOAD_BYTES = 1_048_576
MAX_ATTEMPTS = 3
LEASE_SECONDS = 300


def acquire_runtime_lock(database_url: str) -> BinaryIO:
    """Hold the dedicated runtime's OS lock until close; crashes release it too."""
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise RuntimeError("OneBot durable runtime requires a file-backed SQLite database")
    lock_path = (
        Path(url.database).resolve().with_suffix(Path(url.database).suffix + ".onebot-runtime.lock")
    )
    handle = lock_path.open("a+b")
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("OneBot runtime is already owned by another process") from None
    return handle


async def recover_abandoned_actions(session: AsyncSession) -> int:
    """Caller MUST hold the exclusive runtime lock: an old in-flight send is unknown."""
    from app.actions.orchestrator import ActionIntent

    result = await session.execute(
        update(ActionIntent)
        .where(
            ActionIntent.provider == "onebot",
            ActionIntent.status == "EXECUTING",
        )
        .values(status="UNKNOWN", reason="runtime_restarted_result_unknown", updated_at=_now())
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return int(getattr(result, "rowcount", 0))


def _now() -> datetime:
    return datetime.now(UTC)


class InboxEvent(Base):
    __tablename__ = "onebot_inbox"

    event_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    self_id: Mapped[str] = mapped_column(String(32), index=True)
    group_id: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="")
    payload_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str] = mapped_column(String(64), default="")
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    error_kind: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class InboxFull(RuntimeError):
    """No admission capacity; caller must close/degrade, never silently acknowledge."""


@dataclass(frozen=True)
class InboxClaim:
    event_key: str
    token: str
    payload: dict[str, Any]
    attempts: int


async def enqueue_event(
    session: AsyncSession, payload: dict[str, Any], *, max_pending: int
) -> tuple[str, bool]:
    from app.runtime.onebot_wiring import dedup_key_for

    validate_event(payload)
    key = dedup_key_for(payload, str(payload.get("message_id") or ""))
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("inbox payload exceeds size limit")
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    now = _now()
    values = {
        "event_key": key,
        "self_id": str(payload["self_id"]),
        "group_id": str(payload["group_id"]),
        "payload_json": serialized,
        "payload_hash": digest,
        "status": "PENDING",
        "attempts": 0,
        "lease_token": "",
        "lease_expires_at": None,
        "available_at": now,
        "error_kind": "",
        "created_at": now,
        "updated_at": now,
    }
    pending_count = (
        select(func.count())
        .select_from(InboxEvent)
        .where(InboxEvent.status.in_(("PENDING", "PROCESSING")))
        .scalar_subquery()
    )
    # Capacity check and unique-key admission occur in the same SQLite write statement.
    statement = (
        insert(InboxEvent)
        .from_select(
            list(values),
            select(*(literal(value) for value in values.values())).where(
                pending_count < max_pending
            ),
        )
        .on_conflict_do_nothing(index_elements=[InboxEvent.event_key])
        .returning(InboxEvent.event_key)
    )
    inserted = (await session.execute(statement)).scalar_one_or_none()
    await session.commit()
    if inserted is not None:
        return key, True
    existing = await session.get(InboxEvent, key)
    if existing is not None:
        if existing.payload_hash != digest:
            raise ValueError("duplicate event key with different payload")
        return key, False
    raise InboxFull("OneBot durable inbox is full")


def validate_event(payload: dict[str, Any]) -> None:
    """Validate bounded identity/message shape before any durable admission."""
    if payload.get("post_type") != "message" or payload.get("message_type") != "group":
        raise ValueError("inbox only accepts group message events")
    for key in ("self_id", "message_id", "group_id", "user_id"):
        value = payload.get(key)
        if type(value) not in (int, str):
            raise ValueError("invalid event identity")
        text = str(value)
        digits = text[1:] if key == "message_id" and text.startswith("-") else text
        if not digits or len(text) > 32 or not digits.isascii() or not digits.isdigit():
            raise ValueError("invalid event identity")
        if key != "message_id" and int(text) <= 0:
            raise ValueError("invalid event identity")
    if not isinstance(payload.get("message"), (list, str)):
        raise ValueError("invalid event message shape")


def _due(now: datetime) -> Any:
    return and_(
        InboxEvent.attempts < MAX_ATTEMPTS,
        InboxEvent.payload_json != "",
        or_(
            and_(InboxEvent.status == "PENDING", InboxEvent.available_at <= now),
            and_(InboxEvent.status == "PROCESSING", InboxEvent.lease_expires_at <= now),
        ),
    )


async def due_keys(
    session: AsyncSession, *, limit: int = 100, now: datetime | None = None
) -> list[str]:
    return list(
        (
            await session.execute(
                select(InboxEvent.event_key)
                .where(_due(now or _now()))
                .order_by(InboxEvent.created_at)
                .limit(limit)
            )
        ).scalars()
    )


async def claim_event(
    session: AsyncSession, key: str, *, now: datetime | None = None
) -> InboxClaim | None:
    current = now or _now()
    token = secrets.token_urlsafe(24)
    result = await session.execute(
        update(InboxEvent)
        .where(InboxEvent.event_key == key, _due(current))
        .values(
            status="PROCESSING",
            lease_token=token,
            lease_expires_at=current + timedelta(seconds=LEASE_SECONDS),
            attempts=InboxEvent.attempts + 1,
            updated_at=current,
        )
        .returning(InboxEvent)
        .execution_options(synchronize_session=False)
    )
    row = result.scalar_one_or_none()
    await session.commit()
    if row is None:
        return None
    return InboxClaim(row.event_key, token, json.loads(row.payload_json), row.attempts)


async def renew_event(session: AsyncSession, claim: InboxClaim) -> bool:
    result = await session.execute(
        update(InboxEvent)
        .where(
            InboxEvent.event_key == claim.event_key,
            InboxEvent.lease_token == claim.token,
            InboxEvent.status == "PROCESSING",
        )
        .values(lease_expires_at=_now() + timedelta(seconds=LEASE_SECONDS))
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return bool(getattr(result, "rowcount", 0))


async def finish_event(session: AsyncSession, claim: InboxClaim, *, dead: bool = False) -> bool:
    result = await session.execute(
        update(InboxEvent)
        .where(
            InboxEvent.event_key == claim.event_key,
            InboxEvent.lease_token == claim.token,
            InboxEvent.status == "PROCESSING",
        )
        .values(
            status="DEAD" if dead else "DONE",
            payload_json="",
            lease_token="",
            lease_expires_at=None,
            updated_at=_now(),
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return bool(getattr(result, "rowcount", 0))


async def retry_event(
    session: AsyncSession, claim: InboxClaim, error_kind: str, *, now: datetime | None = None
) -> bool:
    current = now or _now()
    result = await session.execute(
        update(InboxEvent)
        .where(
            InboxEvent.event_key == claim.event_key,
            InboxEvent.lease_token == claim.token,
            InboxEvent.status == "PROCESSING",
        )
        .values(
            status="DEAD" if claim.attempts >= MAX_ATTEMPTS else "PENDING",
            lease_token="",
            lease_expires_at=None,
            available_at=current + timedelta(seconds=5 * (2 ** (claim.attempts - 1))),
            error_kind=error_kind[:64],
            updated_at=current,
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return bool(getattr(result, "rowcount", 0))


async def defer_event(session: AsyncSession, claim: InboxClaim, *, until: datetime) -> None:
    """Waiting for the existing moderation lease is not another processing attempt."""
    await session.execute(
        update(InboxEvent)
        .where(InboxEvent.event_key == claim.event_key, InboxEvent.lease_token == claim.token)
        .values(
            status="PENDING",
            attempts=InboxEvent.attempts - 1,
            lease_token="",
            lease_expires_at=None,
            available_at=until,
            updated_at=_now(),
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def purge_inbox(
    session: AsyncSession,
    now: datetime | None = None,
    *,
    raw_retention_days: int = 30,
    decision_retention_days: int = 180,
) -> dict[str, int]:
    current = now or _now()
    expired = await session.execute(
        update(InboxEvent)
        .where(
            InboxEvent.created_at < current - timedelta(days=raw_retention_days),
            InboxEvent.payload_json != "",
        )
        .values(
            status="DEAD",
            payload_json="",
            lease_token="",
            lease_expires_at=None,
            error_kind="raw_retention_expired",
            updated_at=current,
        )
        .execution_options(synchronize_session=False)
    )
    # Exhausted leases after crashes must not remain permanently PROCESSING.
    await session.execute(
        update(InboxEvent)
        .where(
            InboxEvent.status == "PROCESSING",
            InboxEvent.attempts >= MAX_ATTEMPTS,
            InboxEvent.lease_expires_at <= current,
        )
        .values(
            status="DEAD",
            lease_token="",
            lease_expires_at=None,
            error_kind="retry_limit",
            updated_at=current,
        )
        .execution_options(synchronize_session=False)
    )
    deleted = await session.execute(
        delete(InboxEvent).where(
            InboxEvent.created_at < current - timedelta(days=decision_retention_days),
            InboxEvent.status.in_(("DONE", "DEAD")),
        )
    )
    await session.commit()
    return {
        "inbox_payloads_purged": int(getattr(expired, "rowcount", 0)),
        "inbox_records_deleted": int(getattr(deleted, "rowcount", 0)),
    }
