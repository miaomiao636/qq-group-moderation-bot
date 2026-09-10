"""Additive notification storage; no relationship to punishment state machines."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class NotificationNotice(Base):
    __tablename__ = "notification_notices"
    __table_args__ = (
        CheckConstraint("severity IN ('page', 'ticket')", name="ck_notification_severity"),
        Index("ix_notification_notices_created_at", "created_at"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))
    subject: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by: Mapped[str] = mapped_column(String(64), default="", server_default="")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "notice_id", "channel", "audience", name="uq_notification_delivery_target"
        ),
        CheckConstraint(
            "status IN ('PENDING','SENDING','SENT','FAILED','UNKNOWN','SKIPPED')",
            name="ck_notification_delivery_status",
        ),
        CheckConstraint(
            "audience IN ('primary','backup')", name="ck_notification_delivery_audience"
        ),
        CheckConstraint("attempts BETWEEN 0 AND 3", name="ck_notification_delivery_attempts"),
        Index("ix_notification_deliveries_due", "status", "next_attempt_at"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    notice_id: Mapped[int] = mapped_column(
        ForeignKey("notification_notices.id", ondelete="CASCADE")
    )
    channel: Mapped[str] = mapped_column(String(32))
    audience: Mapped[str] = mapped_column(String(16), default="primary", server_default="primary")
    status: Mapped[str] = mapped_column(String(16), default="PENDING", server_default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_token: Mapped[str] = mapped_column(String(64), default="", server_default="")
    error_code: Mapped[str] = mapped_column(String(64), default="", server_default="")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class NotificationState(Base):
    __tablename__ = "notification_state"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
