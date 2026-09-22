"""Short read snapshots for report assembly; no engine-wide transaction changes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession


@asynccontextmanager
async def report_snapshot(session: AsyncSession) -> AsyncIterator[None]:
    """SAVEPOINT starts a SQLite snapshot even under legacy SELECT transaction mode.

    Use a connection-level savepoint: Session.begin_nested would flush pending
    ORM writes. Disabling autoflush keeps report reads from committing those writes
    when the standalone read savepoint is released. An existing caller transaction
    remains owned by the caller. No network, model or filesystem I/O belongs here.
    """
    with session.no_autoflush:
        connection = await session.connection()
        snapshot = await connection.begin_nested()
        try:
            yield
        finally:
            # SQLite ROLLBACK TO leaves the savepoint/read transaction active.
            # This scope contains SELECTs only, so RELEASE is also required on
            # query failure. It never commits the caller's outer transaction.
            if snapshot.is_active:
                await snapshot.commit()
