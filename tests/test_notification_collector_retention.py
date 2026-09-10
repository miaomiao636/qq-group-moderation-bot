"""Deleting old handoff metadata must not rediscover an already observed action."""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from app.actions.orchestrator import ActionIntent
from app.db import Base
from app.notifications.collector import collect_notifications
from app.notifications.contracts import DeliveryResult
from app.notifications.models import NotificationNotice, NotificationState
from app.notifications.runtime import send_batch
from app.notifications.service import acknowledge_notice, create_notice, purge_notifications
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def collect(factory, now):
    async with factory() as session:
        await collect_notifications(
            session,
            now=now,
            business_channels=("qq",),
            fault_channels=(),
            health={"onebot": True},
        )
        await session.commit()


class FakeSender:
    def __init__(self):
        self.calls = []

    async def send(self, message, audience="primary"):
        self.calls.append(message.notice_id)
        return DeliveryResult("SENT")


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_notice", [False, True])
async def test_acknowledged_unknown_is_not_resent_after_retention_cleanup(factory, legacy_notice):
    now = datetime.now(UTC)
    old = now - timedelta(days=182)
    await collect(factory, old)
    async with factory() as session:
        intent = ActionIntent(
            idempotency_key="synthetic-retention",
            action="recall",
            group_openid="synthetic-group",
            status="UNKNOWN",
            created_at=old,
            updated_at=old + timedelta(seconds=1),
        )
        session.add(intent)
        await session.flush()
        intent_id = intent.id
        if legacy_notice:
            await create_notice(
                session,
                event_key=f"action:{intent_id}",
                kind="unknown_action",
                severity="page",
                subject="动作结果未知，需要人工核对",
                body="固定历史提醒",
                channels=("qq",),
                now=old + timedelta(seconds=10),
            )
        await session.commit()
    await collect(factory, old + timedelta(seconds=30))
    sender = FakeSender()
    await send_batch(factory, {"qq": sender}, email_fallback=False)
    async with factory() as session:
        notice = (await session.scalars(select(NotificationNotice))).one()
        notice_id = notice.id
        await acknowledge_notice(session, notice_id, actor="synthetic-admin")
        await session.commit()
        counts = await purge_notifications(session, before=now - timedelta(days=180))
        await session.commit()
        assert counts == {"notices_deleted": 1, "deliveries_deleted": 1}
    await collect(factory, now)
    await send_batch(factory, {"qq": sender}, email_fallback=False)
    assert sender.calls == [notice_id], "retention must not revive a handled UNKNOWN alert"
    async with factory() as session:
        assert not (await session.scalars(select(NotificationNotice))).all()
        marker = await session.get(NotificationState, f"seen_action:{intent_id}")
        assert marker is not None and marker.value == "1"
        assert (await session.get(ActionIntent, intent_id)).status == "UNKNOWN"


@pytest.mark.asyncio
async def test_old_action_later_becoming_unknown_gets_one_notice_and_marker(factory):
    now = datetime.now(UTC)
    async with factory() as session:
        intent = ActionIntent(
            idempotency_key="synthetic-later-transition",
            action="recall",
            group_openid="synthetic-group",
            status="EXECUTING",
            created_at=now - timedelta(days=1),
        )
        session.add(intent)
        await session.commit()
        intent_id = intent.id
    await collect(factory, now)
    async with factory() as session:
        intent = await session.get(ActionIntent, intent_id)
        intent.status = "UNKNOWN"
        intent.updated_at = now + timedelta(seconds=1)
        await session.commit()
    await collect(factory, now + timedelta(seconds=30))
    await collect(factory, now + timedelta(seconds=60))
    async with factory() as session:
        notices = (await session.scalars(select(NotificationNotice))).all()
        assert len(notices) == 1 and notices[0].event_key == f"action:{intent_id}"
        marker = await session.get(NotificationState, f"seen_action:{intent_id}")
        assert marker is not None and marker.value == "1"
