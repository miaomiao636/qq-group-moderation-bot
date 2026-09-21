"""Long-running data boundaries, using only synthetic files and in-memory SQLite.

Drafted at 4571e0a; not a reviewer-provided probe. The fixture lowers the variable
limit on the actual aiosqlite connection, so 1001 rows exercise a real SQLite
boundary without requiring a large/slow test database.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from app.db import Base
from app.moderation.feedback import FeedbackRecord
from app.notifications.models import NotificationDelivery, NotificationNotice, NotificationState
from app.notifications.service import purge_notifications
from app.reports.cleanup import plan_managed_copies, purge_managed_copies
from app.reports.stats import _agreement
from app.runtime.models import ShadowDecision
from sqlalchemy import event, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

VARIABLE_LIMIT = 1000
BOUNDARY_ROWS = VARIABLE_LIMIT + 1
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=200)
CUTOFF = NOW - timedelta(days=180)


@pytest_asyncio.fixture
async def limited_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    effective_limits = []

    @event.listens_for(engine.sync_engine, "connect")
    def limit_actual_driver_connection(dbapi_connection, _record):
        async def set_and_read(driver):
            # SQLite operations must execute on this aiosqlite worker thread,
            # not a separate sqlite3 connection or the pytest event-loop thread.
            await driver._execute(
                driver._conn.setlimit, sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, VARIABLE_LIMIT
            )
            return await driver._execute(
                driver._conn.getlimit, sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER
            )

        effective_limits.append(dbapi_connection.run_async(set_and_read))

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        assert effective_limits == [VARIABLE_LIMIT]
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
        assert effective_limits and all(limit == VARIABLE_LIMIT for limit in effective_limits)
    finally:
        await engine.dispose()


def _completed_backup(path: Path, *, days_ago: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE synthetic_evidence (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO synthetic_evidence VALUES (1)")
        connection.commit()
    age = time.time() - days_ago * 86400
    os.utime(path, (age, age))
    return path


def _assert_complete_backup(path: Path) -> None:
    assert path.exists(), "cleanup must retain the last completed backup"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert connection.execute("SELECT id FROM synthetic_evidence").fetchall() == [(1,)]


@pytest.mark.parametrize("newer_kind", ["partial", "empty_file", "empty_directory"])
def test_incomplete_backup_cannot_displace_last_completed_backup(tmp_path, newer_kind):
    root = tmp_path / "data"
    completed = _completed_backup(root / "backups" / "moderation-completed.db", days_ago=20)
    newer = (
        root
        / "backups"
        / {
            "partial": "moderation-interrupted.partial",
            "empty_file": "moderation-empty.db",
            "empty_directory": "unfinished-set",
        }[newer_kind]
    )
    if newer_kind == "partial":
        newer.write_bytes(b"synthetic-incomplete-backup")
    elif newer_kind == "empty_file":
        newer.touch()
    else:
        newer.mkdir()

    plan = plan_managed_copies(root=root)
    assert completed not in {Path(item["path"]) for item in plan["files"]}
    purge_managed_copies(root=root)

    _assert_complete_backup(completed)
    assert newer.exists(), "a fresh incomplete artifact is not yet eligible for expiry"


@pytest.mark.parametrize("include_new_partial", [False, True])
def test_completed_directory_backup_set_keeps_its_recovery_files(tmp_path, include_new_partial):
    root = tmp_path / "data"
    older = _completed_backup(root / "backups" / "set_old" / "db.bak", days_ago=40)
    completed = _completed_backup(root / "backups" / "set_new" / "db.bak", days_ago=20)
    companion = completed.parent / "restore-metadata.json"
    companion.write_text('{"synthetic":true}', encoding="utf-8")
    age = time.time() - 20 * 86400
    os.utime(companion, (age, age))
    if include_new_partial:
        (root / "backups" / "moderation-interrupted.partial").write_bytes(b"incomplete")

    purge_managed_copies(root=root)

    _assert_complete_backup(completed)
    assert companion.exists(), "the retained directory backup must remain a complete set"
    assert not older.exists(), "ordinary expiry still removes an older completed backup set"


def test_backup_link_never_displaces_completed_backup_or_deletes_target(tmp_path):
    root = tmp_path / "data"
    completed = _completed_backup(root / "backups" / "moderation-completed.db", days_ago=40)
    external = _completed_backup(tmp_path / "unregistered" / "keep.db", days_ago=1)
    link = root / "backups" / "new-linked-set"
    try:
        link.symlink_to(external.parent, target_is_directory=True)
    except OSError:
        pytest.skip("directory symbolic links are unavailable on this host")

    purge_managed_copies(root=root)

    _assert_complete_backup(completed)
    _assert_complete_backup(external)
    assert link.is_symlink()


async def test_agreement_above_variable_limit_preserves_latest_and_provider_identity(
    limited_session,
):
    session = limited_session
    feedback = [
        {
            "message_id": f"synthetic-{index}",
            "provider": "onebot",
            "group_openid": "group",
            "external_group_id": "group",
            "member_openid": "member",
            "external_user_id": "member",
            "label": "confirmed_violation" if index in (2, 5) else "confirmed_normal",
            "operator": "synthetic-admin",
        }
        for index in range(BOUNDARY_ROWS)
    ]
    shadows = [
        {
            "message_id": row["message_id"],
            "provider": "onebot",
            "group_openid": "group",
            "external_group_id": "group",
            "member_openid": "member",
            "external_user_id": "member",
            "verdict": "violation_high" if index in (2, 5) else "allow",
        }
        for index, row in enumerate(feedback)
    ]
    await session.execute(insert(FeedbackRecord), feedback)
    await session.execute(insert(ShadowDecision), shadows)
    for index, label, provider, group in (
        (0, "unknown_recall", "onebot", "group"),
        (2, "false_positive", "onebot", "group"),
        (3, "confirmed_violation", "onebot", "group"),
        (1, "confirmed_violation", "qq_official", "group"),
        (4, "confirmed_violation", "onebot", "other-group"),
    ):
        session.add(
            FeedbackRecord(
                **{
                    **feedback[index],
                    "label": label,
                    "provider": provider,
                    "group_openid": group,
                    "external_group_id": group,
                }
            )
        )
    await session.commit()

    result = await _agreement(session)

    assert result["total"] == VARIABLE_LIMIT
    assert result["agree"] == VARIABLE_LIMIT - 2
    assert result["true_positive"] == 1
    assert result["false_positive"] == 1
    assert result["false_negative"] == 1
    assert result["true_negative"] == VARIABLE_LIMIT - 3
    assert result["precision"] == 0.5
    assert result["recall"] == 0.5
    assert result["labeled_sample_only"] is True


async def _seed_expired_notices(session: AsyncSession) -> None:
    await session.execute(
        insert(NotificationNotice),
        [
            {
                "id": index + 1,
                "event_key": f"synthetic-expired-{index}",
                "kind": "fault",
                "severity": "ticket",
                "subject": "synthetic",
                "body": "synthetic",
                "created_at": OLD,
                "acknowledged_at": OLD,
            }
            for index in range(BOUNDARY_ROWS)
        ],
    )
    await session.execute(
        insert(NotificationDelivery),
        [
            {
                "notice_id": index + 1,
                "channel": "email",
                "status": "SENT",
                "created_at": OLD,
                "updated_at": OLD,
            }
            for index in range(BOUNDARY_ROWS)
        ],
    )


async def test_notification_cleanup_above_limit_preserves_open_inflight_and_watermarks(
    limited_session,
):
    session = limited_session
    await _seed_expired_notices(session)
    protected = []
    for key, created, acknowledged in (
        ("synthetic-open", OLD, None),
        ("synthetic-inflight", OLD, OLD),
        ("synthetic-recent", NOW, NOW),
    ):
        notice = NotificationNotice(
            event_key=key,
            kind="fault",
            severity="ticket",
            subject="synthetic",
            body="synthetic",
            created_at=created,
            acknowledged_at=acknowledged,
        )
        session.add(notice)
        await session.flush()
        protected.append(notice.id)
        session.add(
            NotificationDelivery(
                notice_id=notice.id,
                channel="email",
                status="SENDING" if key == "synthetic-inflight" else "PENDING",
                created_at=created,
                updated_at=created,
            )
        )
    watermarks = {"collector": '{"case_cursor":10}', "seen_action:50001": "1", "qq_fallback:1": "1"}
    session.add_all([NotificationState(key=key, value=value) for key, value in watermarks.items()])
    await session.commit()

    result = await purge_notifications(session, before=CUTOFF)
    await session.commit()

    assert result == {"notices_deleted": BOUNDARY_ROWS, "deliveries_deleted": BOUNDARY_ROWS}
    assert set(await session.scalars(select(NotificationNotice.id))) == set(protected)
    assert set(await session.scalars(select(NotificationDelivery.notice_id))) == set(protected)
    assert (
        dict((await session.execute(select(NotificationState.key, NotificationState.value))).all())
        == watermarks
    )
    again = await purge_notifications(session, before=CUTOFF)
    await session.commit()
    assert again == {"notices_deleted": 0, "deliveries_deleted": 0}


async def test_notification_cleanup_batches_remain_one_rollbackable_transaction(limited_session):
    session = limited_session
    await _seed_expired_notices(session)
    await session.commit()

    result = await purge_notifications(session, before=CUTOFF)
    assert result == {"notices_deleted": BOUNDARY_ROWS, "deliveries_deleted": BOUNDARY_ROWS}
    await session.rollback()

    assert (
        await session.scalar(select(func.count()).select_from(NotificationNotice)) == BOUNDARY_ROWS
    )
    assert (
        await session.scalar(select(func.count()).select_from(NotificationDelivery))
        == BOUNDARY_ROWS
    )
