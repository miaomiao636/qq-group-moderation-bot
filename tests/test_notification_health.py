"""Readiness must test useful work, not simply process or group traffic."""

from datetime import UTC, datetime, timedelta

import pytest
from app.db import Base
from app.models import SystemMeta
from app.notifications.health import database_checks, evaluate_readiness
from app.notifications.models import NotificationDelivery
from app.notifications.service import create_notice
from app.runtime.inbox import InboxEvent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def test_no_business_traffic_is_not_an_outage() -> None:
    result = evaluate_readiness(
        database={"database": True, "backlog": True},
        onebot_enabled=True,
        onebot_ready=True,
        worker_alive=True,
        notifications_enabled=True,
        notification_worker_alive=True,
    )
    assert result["ready"] is True
    assert "group" not in str(result)


@pytest.mark.parametrize("field", ["onebot_ready", "worker_alive", "notification_worker_alive"])
def test_dead_dependencies_cannot_send_healthy_signal(field: str) -> None:
    flags = dict(onebot_ready=True, worker_alive=True, notification_worker_alive=True)
    flags[field] = False
    result = evaluate_readiness(
        database={"database": True, "backlog": True},
        onebot_enabled=True,
        notifications_enabled=True,
        **flags,
    )
    assert result["ready"] is False


def test_disabled_moderation_is_not_healthy_for_this_monitor() -> None:
    assert (
        evaluate_readiness(
            database={"database": True, "backlog": True},
            onebot_enabled=False,
            onebot_ready=False,
            worker_alive=False,
            notifications_enabled=True,
            notification_worker_alive=True,
        )["ready"]
        is False
    )


@pytest.mark.asyncio
async def test_database_probe_writes_and_detects_old_persisted_backlog() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        assert await database_checks(factory) == {
            "database": True,
            "backlog": True,
            "notification_delivery": True,
        }
        async with factory() as session:
            assert (await session.execute(select(SystemMeta))).scalars().first() is not None
            session.add(
                InboxEvent(
                    event_key="old",
                    self_id="1",
                    group_id="2",
                    payload_hash="x",
                    created_at=datetime.now(UTC) - timedelta(minutes=6),
                )
            )
            await session.commit()
        assert await database_checks(factory) == {
            "database": True,
            "backlog": False,
            "notification_delivery": True,
        }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_database_failure_has_no_raw_exception_in_response() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        result = await database_checks(async_sessionmaker(engine, class_=AsyncSession))
        assert result == {"database": False, "backlog": False, "notification_delivery": False}
        assert "sqlite" not in str(result)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_broken_email_must_be_visible_to_independent_watchdog() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            notice = await create_notice(
                session,
                event_key="mail-failed",
                kind="fault",
                severity="page",
                subject="fault",
                body="fixed",
                channels=("email",),
            )
            delivery = (
                await session.execute(
                    select(NotificationDelivery).where(NotificationDelivery.notice_id == notice.id)
                )
            ).scalar_one()
            delivery.status = "UNKNOWN"
            await session.commit()
        assert (await database_checks(factory))["notification_delivery"] is False
    finally:
        await engine.dispose()
