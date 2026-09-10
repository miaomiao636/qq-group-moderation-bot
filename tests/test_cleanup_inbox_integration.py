"""The scheduled retention cleanup must include the durable transport inbox."""

from datetime import UTC, datetime, timedelta

import pytest
from app.db import Base
from app.reports.cleanup import purge_expired
from app.runtime.inbox import InboxEvent
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


@pytest.mark.asyncio
async def test_scheduled_cleanup_erases_expired_inbox_payload() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine) as session:
            session.add(
                InboxEvent(
                    event_key="cleanup-test",
                    self_id="1",
                    group_id="2",
                    payload_json='{"test":true}',
                    payload_hash="test",
                    status="DEAD",
                    created_at=datetime.now(UTC) - timedelta(days=31),
                )
            )
            await session.commit()
            result = await purge_expired(session)
            row = await session.get(InboxEvent, "cleanup-test", populate_existing=True)
            assert row is not None and row.payload_json == ""
            assert result["inbox_payloads_purged"] == 1
    finally:
        await engine.dispose()
