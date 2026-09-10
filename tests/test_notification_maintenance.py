"""The scheduled entry point accepts the real notification cleanup counters."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from app.db import Base
from app.models import SystemSetting
from app.notifications.models import NotificationNotice
from app.notifications.service import acknowledge_notice, create_notice
from app.reports.maintenance import run_cleanup
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


@pytest.mark.asyncio
async def test_enabled_maintenance_records_real_notification_cleanup_success(tmp_path, monkeypatch):
    # Keep both database deletion and the real media cleanup within test-owned paths.
    monkeypatch.setattr("app.runtime.pipeline.MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(
        "app.reports.cleanup.get_settings",
        lambda: SimpleNamespace(raw_retention_days=30, decision_retention_days=180),
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            old = datetime.now(UTC) - timedelta(days=181)
            closed = await create_notice(
                session,
                event_key="maintenance:closed",
                kind="fault",
                severity="page",
                subject="Fixed summary",
                body="Fixed summary",
                channels=("email",),
                now=old,
            )
            closed_id = closed.id
            await acknowledge_notice(session, closed_id, actor="synthetic-admin", now=old)
            pending = await create_notice(
                session,
                event_key="maintenance:pending",
                kind="fault",
                severity="page",
                subject="Fixed summary",
                body="Fixed summary",
                channels=("email",),
                now=old,
            )
            pending_id = pending.id
            session.add(SystemSetting(key="auto_cleanup_enabled", value="1"))
            await session.commit()

            outcome = await run_cleanup(session)

            assert outcome["status"] == "succeeded"
            assert outcome["error"] == ""
            assert outcome["counts"]["notification_notices_deleted"] == 1
            assert outcome["counts"]["notification_deliveries_deleted"] == 1
        async with AsyncSession(engine) as session:
            assert await session.get(NotificationNotice, closed_id) is None
            assert await session.get(NotificationNotice, pending_id) is not None
            assert (await session.get(SystemSetting, "last_cleanup_status")).value == "succeeded"
            assert (await session.get(SystemSetting, "last_cleanup_error")).value == ""
            saved = await session.get(SystemSetting, "last_cleanup_result")
            assert json.loads(saved.value) == outcome["counts"]
            repeated = await run_cleanup(session)
            assert repeated["status"] == "succeeded"
            assert repeated["counts"]["notification_notices_deleted"] == 0
            assert repeated["counts"]["notification_deliveries_deleted"] == 0
    finally:
        await engine.dispose()
