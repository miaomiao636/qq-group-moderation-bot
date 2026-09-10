"""Bounded background collection and delivery, outside moderation transactions."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime

from sqlalchemy import String, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.notifications.config import NotificationSettings
from app.notifications.contracts import DeliveryMessage, DeliveryResult, NotificationSender
from app.notifications.models import NotificationDelivery, NotificationNotice, NotificationState
from app.notifications.service import (
    claim_delivery,
    complete_delivery,
    create_notice,
    enqueue_escalations,
    reap_stale_deliveries,
    resolve_notice,
)

logger = logging.getLogger(__name__)
_task: asyncio.Task[None] | None = None
_last_tick: float | None = None
_poll_seconds = 30


def notification_worker_healthy() -> bool:
    return (
        _task is not None
        and not _task.done()
        and _last_tick is not None
        and time.monotonic() - _last_tick < max(120, _poll_seconds * 3)
    )


async def _delivery(
    factory: async_sessionmaker[AsyncSession],
    row: NotificationDelivery,
    senders: Mapping[str, NotificationSender],
) -> None:
    async with factory() as session:
        notice = await session.get(NotificationNotice, row.notice_id)
        current = await session.get(NotificationDelivery, row.id)
        if (
            notice is None
            or current is None
            or current.status != "SENDING"
            or current.claim_token != row.claim_token
        ):
            return
        message = DeliveryMessage(notice.id, notice.subject, notice.body)
        closed = notice.acknowledged_at is not None or notice.resolved_at is not None
        expired = current.lease_expires_at is None or current.lease_expires_at.replace(
            tzinfo=None
        ) <= datetime.now(UTC).replace(tzinfo=None)
    sender = senders.get(row.channel)
    result = DeliveryResult("SKIPPED", "channel_disabled")
    if expired:
        result = DeliveryResult("UNKNOWN", "lease_expired")
    elif closed:
        result = DeliveryResult("SKIPPED", "notice_closed")
    elif sender is not None:
        try:
            async with asyncio.timeout(25):
                result = await sender.send(message, audience=row.audience)
        except asyncio.CancelledError:
            # Persisted SENDING is later reaped as UNKNOWN; never replay cancellation.
            raise
        except Exception:  # noqa: BLE001 - unknown provider exceptions cannot prove non-send
            result = DeliveryResult("UNKNOWN", "sender_exception")
    async with factory() as session:
        changed = await complete_delivery(
            session, row.id, claim_token=row.claim_token, result=result
        )
        if changed and result.status == "SENT":
            notice = await session.get(NotificationNotice, row.notice_id)
            incomplete = (
                await session.execute(
                    select(NotificationDelivery.id)
                    .where(
                        NotificationDelivery.notice_id == row.notice_id,
                        NotificationDelivery.status.not_in(("SENT", "SKIPPED")),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if notice and notice.severity == "ticket" and incomplete is None:
                await resolve_notice(session, notice.id)
        await session.commit()
    logger.info(
        "notification_delivery notice_id=%d delivery_id=%d channel=%s status=%s",
        row.notice_id,
        row.id,
        row.channel,
        result.status,
    )


async def _fallbacks(session: AsyncSession) -> None:
    # Include crash-reaped UNKNOWN deliveries as well as sender-returned failures.
    rows = (
        await session.execute(
            select(NotificationDelivery.notice_id)
            .join(NotificationNotice)
            .where(
                NotificationDelivery.channel == "qq",
                NotificationDelivery.status.in_(("FAILED", "UNKNOWN")),
                NotificationNotice.acknowledged_at.is_(None),
                NotificationNotice.resolved_at.is_(None),
                ~select(NotificationState.key)
                .where(
                    NotificationState.key
                    == "qq_fallback:" + NotificationDelivery.notice_id.cast(String)
                )
                .correlate(NotificationDelivery)
                .exists(),
            )
            .order_by(NotificationDelivery.id)
            .limit(25)
        )
    ).scalars()
    for notice_id in rows:
        await create_notice(
            session,
            event_key=f"qq_fallback:{notice_id}",
            kind="delivery_problem",
            severity="ticket",
            subject="QQ 提醒未确认送达，请查看后台",
            body=f"通知记录 #{notice_id} 的 QQ 通道失败或结果未知。请到通知页确认接手原事项，勿盲目重发。",
            channels=("email",),
        )
        await session.execute(
            insert(NotificationState)
            .values(key=f"qq_fallback:{notice_id}", value="1")
            .on_conflict_do_nothing(index_elements=[NotificationState.key])
        )


async def send_batch(
    factory: async_sessionmaker[AsyncSession],
    senders: Mapping[str, NotificationSender],
    *,
    email_fallback: bool,
) -> None:
    claims: list[NotificationDelivery] = []
    async with factory() as session:
        await reap_stale_deliveries(session)
        if email_fallback:
            await _fallbacks(session)
        await session.commit()
        for _ in range(4):
            claimed = await claim_delivery(session)
            await session.commit()  # MUST precede any external I/O.
            if claimed is None:
                break
            claims.append(claimed)

    # Each SMTP instance allows only one socket thread. Preserve per-channel
    # ordering without letting a stalled QQ channel block independent email.
    async def channel_batch(channel: str) -> None:
        for row in claims:
            if row.channel == channel:
                await _delivery(factory, row, senders)

    # TaskGroup joins/cancels siblings on failure and application shutdown.
    async with asyncio.TaskGroup() as group:
        for channel in {row.channel for row in claims}:
            group.create_task(channel_batch(channel))


async def _run(settings: NotificationSettings) -> None:
    from app.config import get_settings
    from app.db import SessionLocal
    from app.notifications.collector import collect_notifications
    from app.notifications.health import check_readiness
    from app.notifications.senders import EmailSender, QQSender

    global _last_tick
    senders: dict[str, NotificationSender] = {}
    if settings.qq_enabled:
        senders["qq"] = QQSender(settings, get_settings())
    if settings.email_enabled:
        senders["email"] = EmailSender(settings)
    business = ("qq",) if settings.qq_enabled else (("email",) if settings.email_enabled else ())
    faults = ("email",) if settings.email_enabled else ()
    backup = ("email",) if settings.email_enabled and settings.email_backup_to else ()
    while True:
        try:
            readiness = await check_readiness()
            checks = readiness["checks"]
            assert isinstance(checks, dict)
            async with SessionLocal() as session:
                await collect_notifications(
                    session,
                    business_channels=business,
                    fault_channels=faults,
                    health=checks,
                    summary_seconds=settings.summary_seconds,
                )
                await enqueue_escalations(
                    session,
                    channels=backup,
                    after_seconds=settings.escalation_seconds,
                    now=datetime.now(UTC),
                )
                await session.commit()
            await send_batch(SessionLocal, senders, email_fallback=settings.email_enabled)
            _last_tick = time.monotonic()
        except Exception:  # noqa: BLE001 - keep watcher alive; stale tick turns external signal red
            logger.warning("notification_tick_failed error_code=collection_or_delivery_failed")
        await asyncio.sleep(settings.poll_seconds)


def start_notification_runtime(settings: NotificationSettings) -> None:
    global _task, _poll_seconds, _last_tick
    if not settings.enabled:
        return
    if _task is not None and not _task.done():
        raise RuntimeError("notification runtime already started")
    _poll_seconds = settings.poll_seconds
    _last_tick = None
    _task = asyncio.create_task(_run(settings), name="notification-runtime")


async def stop_notification_runtime() -> None:
    global _task, _last_tick
    if _task is not None:
        _task.cancel()
        with suppress(asyncio.CancelledError):
            await _task
    _task = None
    _last_tick = None
