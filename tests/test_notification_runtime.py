"""Outbox delivery cannot block moderation or turn acknowledgement into action."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from app.db import Base
from app.notifications.contracts import DeliveryResult
from app.notifications.models import NotificationDelivery, NotificationNotice
from app.notifications.runtime import send_batch
from app.notifications.service import (
    acknowledge_notice,
    create_notice,
    enqueue_escalations,
    purge_notifications,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class Sender:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def send(self, message, audience="primary"):
        self.calls.append((message, audience))
        return self.result


async def add_notice(factory, severity="page"):
    async with factory() as session:
        await create_notice(
            session,
            event_key="case:1",
            kind="case",
            severity=severity,
            subject="安全通知",
            body="记录 #1",
            channels=("qq",),
        )
        await session.commit()


@pytest.mark.asyncio
async def test_qq_unknown_gets_one_email_fallback_and_is_never_replayed(factory):
    await add_notice(factory)
    qq = Sender(DeliveryResult("UNKNOWN", "response_lost"))
    email = Sender(DeliveryResult("SENT"))
    await send_batch(factory, {"qq": qq, "email": email}, email_fallback=True)
    await send_batch(factory, {"qq": qq, "email": email}, email_fallback=True)
    await send_batch(factory, {"qq": qq, "email": email}, email_fallback=True)
    assert len(qq.calls) == len(email.calls) == 1
    async with factory() as session:
        rows = (await session.execute(select(NotificationDelivery))).scalars().all()
        assert sorted(r.status for r in rows) == ["SENT", "UNKNOWN"]


@pytest.mark.asyncio
async def test_removed_channel_is_skipped_not_sent(factory):
    await add_notice(factory)
    await send_batch(factory, {}, email_fallback=False)
    async with factory() as session:
        assert (
            await session.execute(select(NotificationDelivery.status))
        ).scalar_one() == "SKIPPED"


@pytest.mark.asyncio
async def test_sent_ticket_auto_closes_but_page_waits_for_human(factory):
    await add_notice(factory, severity="ticket")
    await send_batch(factory, {"qq": Sender(DeliveryResult("SENT"))}, email_fallback=False)
    async with factory() as session:
        notice = (await session.execute(select(NotificationNotice))).scalar_one()
        assert notice.resolved_at is not None and notice.acknowledged_at is None


@pytest.mark.asyncio
async def test_unexpected_sender_exception_is_unknown_and_sanitized(factory, caplog):
    await add_notice(factory)

    class Broken:
        async def send(self, message, audience="primary"):
            raise RuntimeError("private credentials and email")

    await send_batch(factory, {"qq": Broken()}, email_fallback=False)
    async with factory() as session:
        row = (await session.execute(select(NotificationDelivery))).scalar_one()
        assert row.status == "UNKNOWN" and row.error_code == "sender_exception"
    assert "private" not in caplog.text


@pytest.mark.asyncio
async def test_batch_database_failure_cancels_and_joins_other_sends(factory, monkeypatch):
    from app.notifications import runtime

    async with factory() as session:
        await create_notice(
            session,
            event_key="two-sends",
            kind="case",
            severity="page",
            subject="fixed",
            body="fixed",
            channels=("qq", "email"),
        )
        await session.commit()
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def delivery(factory, row, senders):
        if row.channel == "qq":
            await started.wait()
            raise RuntimeError("database unavailable")
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(runtime, "_delivery", delivery)
    with pytest.raises(ExceptionGroup, match="unhandled errors in a TaskGroup"):
        await runtime.send_batch(factory, {}, email_fallback=False)
    assert stopped.is_set(), "failed batch must not orphan sibling sends"


@pytest.mark.asyncio
async def test_ack_prevents_claimed_but_not_yet_sent_backup(factory):
    async with factory() as session:
        notice = await create_notice(
            session,
            event_key="ack-race",
            kind="case",
            severity="page",
            subject="fixed",
            body="fixed",
            channels=("email",),
            now=datetime.now(UTC) - timedelta(minutes=16),
        )
        await enqueue_escalations(session, channels=("email",))
        await session.commit()
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    class BlockFirst:
        async def send(self, message, audience="primary"):
            calls.append(audience)
            if audience == "primary":
                started.set()
                await release.wait()
            return DeliveryResult("SENT")

    task = asyncio.create_task(send_batch(factory, {"email": BlockFirst()}, email_fallback=False))
    await started.wait()
    try:
        async with factory() as session:
            assert await acknowledge_notice(session, notice.id, actor="synthetic-human")
            await session.commit()
    finally:
        release.set()
        await task
    assert calls == ["primary"]
    async with factory() as session:
        backup = (
            await session.execute(
                select(NotificationDelivery).where(NotificationDelivery.audience == "backup")
            )
        ).scalar_one()
        assert backup.status == "SKIPPED"


@pytest.mark.asyncio
async def test_retention_does_not_repeat_successful_fallback(factory):
    await add_notice(factory)
    qq, email = Sender(DeliveryResult("UNKNOWN", "response_lost")), Sender(DeliveryResult("SENT"))
    await send_batch(factory, {"qq": qq, "email": email}, email_fallback=True)
    await send_batch(factory, {"qq": qq, "email": email}, email_fallback=True)
    async with factory() as session:
        assert (await purge_notifications(session, before=datetime.now(UTC) + timedelta(days=181)))[
            "notices_deleted"
        ] == 1
        await session.commit()
    await send_batch(factory, {"qq": qq, "email": email}, email_fallback=True)
    assert len(email.calls) == 1
