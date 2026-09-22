"""Internal regressions for ORM report snapshots; all databases are synthetic."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from app.db import Base
from app.models import ProcessedEvent
from app.reports import service, stats
from app.runtime.models import ShadowDecision
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

NOW = datetime(2030, 1, 2, 12)


@pytest_asyncio.fixture
async def report_engine(tmp_path):
    path = tmp_path / "report.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield engine, path
    finally:
        await engine.dispose()


def add_event_and_violation(path):
    with closing(sqlite3.connect(path)) as writer:
        writer.execute(
            "INSERT INTO processed_events(message_id,event_type,provider,external_group_id,external_user_id,status,error_message,error_kind,lease_token,attempts,processed_at) VALUES('test-new','synthetic','onebot','','','PROCESSED','','','',0,?)",
            (str(NOW),),
        )
        writer.execute(
            "INSERT INTO violation_records(group_openid,member_openid,provider,external_group_id,external_user_id,message_id,category,confidence,rule_hits_json,message_snapshot_json,action_result_json,revoked,revoke_reason,created_at) VALUES('test-group','test-member','onebot','','','test-new','ad',.99,'[]','{}','[]',0,'',?)",
            (str(NOW),),
        )
        writer.commit()


@pytest.mark.parametrize(
    "builder,trigger",
    [
        (service.build_daily, "FROM violation_records"),
        (service.build_weekly, "FROM processed_events"),
    ],
)
async def test_report_counts_cannot_mix_one_atomic_concurrent_commit(
    report_engine, builder, trigger
):
    engine, path = report_engine
    async with AsyncSession(engine) as session:
        session.add(ProcessedEvent(message_id="test-existing", processed_at=NOW))
        await session.commit()
    fired = False

    def insert_between_queries(_conn, _cursor, statement, _parameters, _context, _many):
        nonlocal fired
        if not fired and trigger in statement:
            fired = True
            add_event_and_violation(path)

    event.listen(engine.sync_engine, "before_cursor_execute", insert_between_queries)
    try:
        async with AsyncSession(engine) as session:
            result = await builder(session, NOW.replace(tzinfo=UTC))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", insert_between_queries)
    assert fired
    assert (result["messages_processed"], result["violations_recorded"]) in {(1, 0), (2, 1)}


def add_shadow(path, mid="test-concurrent"):
    with closing(sqlite3.connect(path)) as writer:
        writer.execute(
            "INSERT INTO shadow_decisions(message_id,external_message_id,group_openid,member_openid,provider,external_group_id,external_user_id,sender_name,kind,verdict,category,confidence,reason,detail_json,created_at) VALUES(?,'','test-group','test-member','onebot','','','','text','allow','',0,'','{}',?)",
            (mid, str(NOW)),
        )
        writer.commit()


async def test_stats_totals_and_groups_share_snapshot_then_release_it(report_engine):
    engine, path = report_engine
    add_shadow(path, "test-initial")
    fired = False

    def insert_before_group(_conn, _cursor, statement, _parameters, _context, _many):
        nonlocal fired
        if not fired and "GROUP BY shadow_decisions.verdict" in statement:
            fired = True
            add_shadow(path)

    event.listen(engine.sync_engine, "before_cursor_execute", insert_before_group)
    try:
        async with AsyncSession(engine) as session:
            result = await stats.build_stats(session)
            assert fired
            assert result["totals"]["shadow"] == sum(n for _, n in result["verdicts"]) == 1
            # Reporting must not pin a WAL snapshot after its work is finished.
            assert await session.scalar(select(func.count()).select_from(ShadowDecision)) == 2
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", insert_before_group)


async def test_stats_never_flushes_pending_caller_writes(report_engine):
    engine, _path = report_engine
    async with AsyncSession(engine) as session:
        pending = ProcessedEvent(message_id="caller-pending", processed_at=NOW)
        session.add(pending)
        await stats.build_stats(session)
        assert pending in session.new
        async with AsyncSession(engine) as observer:
            assert await observer.get(ProcessedEvent, "caller-pending") is None
        await session.rollback()


async def test_stats_preserves_caller_transaction_rollback(report_engine):
    engine, _path = report_engine
    async with AsyncSession(engine) as session:
        session.add(ProcessedEvent(message_id="caller-write", processed_at=NOW))
        await session.flush()
        await stats.build_stats(session)
        await session.rollback()
    async with AsyncSession(engine) as observer:
        assert await observer.get(ProcessedEvent, "caller-write") is None


async def test_stats_query_failure_releases_read_snapshot(report_engine, monkeypatch):
    engine, path = report_engine
    add_shadow(path, "test-initial")

    async def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic query failure")

    monkeypatch.setattr(stats, "_group_counts", fail)
    async with AsyncSession(engine) as session:
        with pytest.raises(RuntimeError, match="synthetic query failure"):
            await stats.build_stats(session)
        add_shadow(path)
        assert await session.scalar(select(func.count()).select_from(ShadowDecision)) == 2
