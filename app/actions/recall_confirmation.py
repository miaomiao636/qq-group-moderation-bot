"""Durable, bounded recall evidence, independent of transport response status.

Only an authenticated adapter may supply a RecallNotice. Matching a notice does
not relabel a message, retry an action, or resume an interrupted punishment chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.contracts import StandardMessage
from app.db import Base

CONFIRMATION_WINDOW_SECONDS = 300


async def confirmation_table_available(session: AsyncSession) -> bool:
    """Read-only jobs may run during the additive-migration preparation window."""
    return await session.run_sync(
        lambda sync: inspect(sync.connection()).has_table("recall_confirmations")
    )


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class RecallNotice:
    account_id: str
    group_id: str
    user_id: str
    message_id: str
    operator_id: str
    occurred_at: datetime


class RecallConfirmation(Base):
    __tablename__ = "recall_confirmations"
    __table_args__ = (
        Index("ix_recall_confirmation_match", "account_id", "group_id", "message_id"),
    )

    intent_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("action_intents.id"), primary_key=True
    )
    account_id: Mapped[str] = mapped_column(String(128))
    group_id: Mapped[str] = mapped_column(String(128))
    user_id: Mapped[str] = mapped_column(String(128))
    message_id: Mapped[str] = mapped_column(String(128))
    source_event_key: Mapped[str] = mapped_column(String(128))
    source_sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notice_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    operator_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    notice_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)


def _numeric_id(value: str, *, signed: bool = False) -> bool:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return False
    return str(number) == value and (-(2**63) <= number < 2**63 if signed else 0 < number < 2**63)


async def arm_confirmation(
    session: AsyncSession, intent_id: int, msg: StandardMessage, account_id: str
) -> bool:
    """Commit before network I/O; incomplete legacy identity stays unverified."""
    if (
        msg.provider != "onebot"
        or msg.external_self_id != account_id
        or not all(
            _numeric_id(v) for v in (account_id, msg.external_group_id, msg.external_user_id)
        )
        or not _numeric_id(msg.external_message_id, signed=True)
        or msg.sent_at is None
    ):
        return False
    if await session.get(RecallConfirmation, intent_id) is not None:
        return False  # Never re-arm or extend the original request's window.
    session.add(
        RecallConfirmation(
            intent_id=intent_id,
            account_id=account_id,
            group_id=msg.external_group_id,
            user_id=msg.external_user_id,
            message_id=msg.external_message_id,
            source_event_key=msg.message_id,
            source_sent_at=utc(msg.sent_at),
            requested_at=datetime.now(UTC),
        )
    )
    await session.commit()
    return True


async def accept_notice(
    session: AsyncSession, notice: RecallNotice, *, received_at: datetime | None = None
) -> bool:
    from app.actions.orchestrator import ActionIntent

    if notice.operator_id != notice.account_id:
        return False
    received = utc(received_at or datetime.now(UTC))
    occurred = utc(notice.occurred_at)
    if occurred > received:
        return False
    window = timedelta(seconds=CONFIRMATION_WINDOW_SECONDS)
    fingerprint = sha256(
        "|".join(
            (
                notice.account_id,
                notice.group_id,
                notice.user_id,
                notice.message_id,
                notice.operator_id,
                occurred.isoformat(),
            )
        ).encode()
    ).hexdigest()
    if (
        await session.scalar(
            select(RecallConfirmation.intent_id).where(
                RecallConfirmation.notice_fingerprint == fingerprint
            )
        )
        is not None
    ):
        return False
    # NapCat sets group_recall.time using its local Date.now(), while message
    # time comes from QQ msgTime. The deployed NapCat shares the runtime host.
    # Allow notice's integer-second truncation, not a cross-clock comparison
    # with source_sent_at. A differently clocked remote adapter stays unverified.
    matches = list(
        (
            await session.scalars(
                select(RecallConfirmation.intent_id)
                .join(ActionIntent, ActionIntent.id == RecallConfirmation.intent_id)
                .where(
                    RecallConfirmation.account_id == notice.account_id,
                    RecallConfirmation.group_id == notice.group_id,
                    RecallConfirmation.user_id == notice.user_id,
                    RecallConfirmation.message_id == notice.message_id,
                    RecallConfirmation.requested_at <= received,
                    RecallConfirmation.requested_at >= received - window,
                    RecallConfirmation.requested_at < occurred + timedelta(seconds=1),
                    ActionIntent.provider == "onebot",
                    ActionIntent.action == "recall",
                    ActionIntent.external_group_id == notice.group_id,
                    ActionIntent.external_user_id == notice.user_id,
                    ActionIntent.external_message_id == notice.message_id,
                    ActionIntent.message_id == RecallConfirmation.source_event_key,
                    ActionIntent.status.in_(("EXECUTING", "SUCCEEDED", "UNKNOWN", "FAILED")),
                )
                .limit(2)
            )
        ).all()
    )
    if len(matches) != 1:
        return False  # Including short-ID reuse ambiguity; never guess an owner.
    result = await session.execute(
        update(RecallConfirmation)
        .where(
            RecallConfirmation.intent_id == matches[0],
            RecallConfirmation.confirmed_at.is_(None),
        )
        .values(
            confirmed_at=received,
            notice_at=occurred,
            operator_id=notice.operator_id,
            notice_fingerprint=fingerprint,
        )
    )
    await session.commit()
    return bool(result.rowcount == 1)  # type: ignore[attr-defined]


async def is_confirmed(session: AsyncSession, intent_id: int) -> bool:
    return (
        await session.scalar(
            select(RecallConfirmation.confirmed_at).where(RecallConfirmation.intent_id == intent_id)
        )
    ) is not None


def confirmation_label(record: RecallConfirmation | None, *, now: datetime | None = None) -> str:
    if record is None:
        return "未采集撤回确认（历史记录、身份不足或确认记录已到保留期）"
    if record.confirmed_at is not None:
        return "已收到 QQ 撤回通知"
    if utc(now or datetime.now(UTC)) > utc(record.requested_at) + timedelta(
        seconds=CONFIRMATION_WINDOW_SECONDS
    ):
        return "未收到匹配的撤回通知，需人工核实；不会自动重试"
    return "等待 QQ 撤回通知；接口成功不等于已确认撤回"
