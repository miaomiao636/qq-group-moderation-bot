"""Independent notification channels keep a bounded share of each send batch."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from app.db import Base
from app.notifications.contracts import DeliveryMessage, DeliveryResult
from app.notifications.models import NotificationDelivery
from app.notifications.runtime import send_batch
from app.notifications.service import claim_delivery, create_notice
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fairness.db'}")
    try:
        async with engine.begin() as connection:
            await connection.execute(text("PRAGMA journal_mode=WAL"))
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def add_notice(session: AsyncSession, key: str, channel: str) -> None:
    await create_notice(
        session,
        event_key=key,
        kind="fault" if channel == "email" else "case",
        severity="page",
        subject="Fixed summary",
        body="Fixed summary",
        channels=(channel,),
    )


@pytest.mark.parametrize("backlog_channel", ["qq", "email"])
async def test_backlog_cannot_block_other_channel_send_in_same_bounded_batch(
    factory: async_sessionmaker[AsyncSession], backlog_channel: str
) -> None:
    other_channel = "email" if backlog_channel == "qq" else "qq"
    async with factory() as session:
        for index in range(8):
            await add_notice(session, f"backlog:{index}", backlog_channel)
        await add_notice(session, "independent:1", other_channel)
        await session.commit()

    blocked, other_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    active = maximum_active = 0
    calls: list[str] = []

    class Backlogged:
        async def send(self, message: DeliveryMessage, audience: str = "primary") -> DeliveryResult:
            nonlocal active, maximum_active
            calls.append(backlog_channel)
            active += 1
            maximum_active = max(maximum_active, active)
            blocked.set()
            try:
                await release.wait()
                return DeliveryResult("FAILED", "transport_not_ready", retryable=True)
            finally:
                active -= 1

    class Independent:
        async def send(self, message: DeliveryMessage, audience: str = "primary") -> DeliveryResult:
            calls.append(other_channel)
            other_started.set()
            return DeliveryResult("SENT")

    batch = asyncio.create_task(
        send_batch(
            factory,
            {backlog_channel: Backlogged(), other_channel: Independent()},
            email_fallback=False,
        )
    )
    try:
        await asyncio.wait_for(blocked.wait(), 2)
        # Actual I/O entry, not merely a reserved SENDING row: QQ/SMTP may still be waiting.
        await asyncio.wait_for(other_started.wait(), 2)
        assert not release.is_set()
    finally:
        release.set()
        await asyncio.wait_for(batch, 2)

    assert calls.count(other_channel) == 1
    assert calls.count(backlog_channel) == 3
    assert maximum_active == 1, "each channel still sends serially"
    async with factory() as session:
        rows = list(await session.scalars(select(NotificationDelivery)))
        assert sum(row.attempts for row in rows) == 4
        assert sum(row.status == "PENDING" for row in rows) == 5
        assert all(row.next_attempt_at is not None for row in rows if row.status == "FAILED")


async def test_channel_claim_is_exact_and_default_still_uses_fifo(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await add_notice(session, "earlier:qq", "qq")
        await add_notice(session, "later:email", "email")
        await session.commit()
        selected = await claim_delivery(session, channel="email")
        assert selected is not None and selected.channel == "email"
        await session.commit()
        assert await claim_delivery(session, channel="email") is None
        assert await claim_delivery(session, channel="missing") is None
        earlier = await claim_delivery(session)
        assert earlier is not None and earlier.channel == "qq" and earlier.id < selected.id
        await session.rollback()
    async with factory() as session:
        earlier = await claim_delivery(session, channel="qq")
        assert earlier is not None and earlier.attempts == 1, "claim never commits for its caller"


@pytest.mark.parametrize("channel", ["", "email,qq", "email;delete", "x" * 33])
async def test_channel_filter_rejects_invalid_identifiers_without_claiming(
    factory: async_sessionmaker[AsyncSession], channel: str
) -> None:
    async with factory() as session:
        await add_notice(session, "untouched:qq", "qq")
        await session.commit()
        with pytest.raises(ValueError, match="invalid channel"):
            await claim_delivery(session, channel=channel)
        row = (await session.scalars(select(NotificationDelivery))).one()
        assert row.status == "PENDING" and row.attempts == 0


async def test_concurrent_filtered_claims_have_one_winner_and_cannot_steal_qq(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await add_notice(session, "untouched:qq", "qq")
        await add_notice(session, "single:email", "email")
        await session.commit()

    async def claim_once() -> NotificationDelivery | None:
        async with factory() as session:
            row = await claim_delivery(session, channel="email")
            await session.commit()
            return row

    claimed = await asyncio.gather(claim_once(), claim_once())
    winners = [row for row in claimed if row is not None]
    assert len(winners) == 1 and winners[0].channel == "email"
    async with factory() as session:
        qq = (
            await session.scalars(
                select(NotificationDelivery).where(NotificationDelivery.channel == "qq")
            )
        ).one()
        assert qq.status == "PENDING" and qq.attempts == 0


async def test_remaining_fifo_slots_still_skip_unconfigured_channels(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        for index in range(3):
            await add_notice(session, f"disabled:{index}", "qq")
        for index in range(6):
            await add_notice(session, f"active:{index}", "email")
        await session.commit()

    class Sender:
        async def send(self, message: DeliveryMessage, audience: str = "primary") -> DeliveryResult:
            return DeliveryResult("SENT")

    await send_batch(factory, {"email": Sender()}, email_fallback=False)
    async with factory() as session:
        rows = list(await session.scalars(select(NotificationDelivery)))
        assert sum(row.status == "SKIPPED" for row in rows) == 3
        assert sum(row.status == "SENT" for row in rows) == 1
        assert sum(row.status == "PENDING" for row in rows) == 5
        assert sum(row.attempts for row in rows) == 4
