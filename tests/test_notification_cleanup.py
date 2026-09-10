"""Scheduled maintenance retains open incidents, but cleans closed metadata."""

from datetime import UTC, datetime, timedelta

import pytest
from app.db import Base
from app.notifications.models import NotificationNotice
from app.notifications.service import acknowledge_notice, create_notice
from app.reports.cleanup import purge_expired
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


@pytest.mark.asyncio
async def test_normal_cleanup_includes_closed_notifications():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            now = datetime.now(UTC)
            closed = await create_notice(
                session,
                event_key="closed-old",
                kind="fault",
                severity="page",
                subject="fixed",
                body="fixed",
                channels=("email",),
                now=now - timedelta(days=181),
            )
            await acknowledge_notice(session, closed.id, actor="synthetic-admin")
            pending = await create_notice(
                session,
                event_key="open-old",
                kind="fault",
                severity="page",
                subject="fixed",
                body="fixed",
                channels=("email",),
                now=now - timedelta(days=181),
            )
            await session.commit()
            result = await purge_expired(session, now)
            assert result["notification_notices_deleted"] == 1
            session.expunge_all()
            assert await session.get(NotificationNotice, closed.id) is None
            assert await session.get(NotificationNotice, pending.id) is not None
    finally:
        await engine.dispose()
