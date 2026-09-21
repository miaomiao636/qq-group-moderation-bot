"""Real temporary SQLite tasks; no browser, QQ connection, or production state."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.space_inspector import history
from app.space_inspector.contracts import (
    RESTRICTED,
    UNCONFIRMED,
    Group,
    InspectionError,
    Observation,
)
from app.space_inspector.history import list_tasks
from app.space_inspector.store import Store


def make_task(root: Path, name: str = "20260921T120000Z-0123abcd") -> Store:
    store = Store(root / name, create=True)
    store.bind_source("12345678")
    store.bind_viewer("23456789")
    for group, label in (("34567890", "合成甲群"), ("45678901", "合成乙群")):
        store.add_snapshot(Group(group, label, 3), ["56789012", "67890123", "78901234"])
    store.seal_snapshots()
    with store.db:
        store.db.execute("UPDATE meta SET value='2026-09-21T12:00:00+00:00' WHERE key='created_at'")
    for qq, status in (("56789012", RESTRICTED), ("67890123", UNCONFIRMED)):
        restricted = status == RESTRICTED
        store.save(
            Observation(
                qq,
                status,
                "synthetic_notice",
                "2026-09-21T12:00:00+00:00",
                {
                    "page_url": "https://user.qzone.qq.com/" + qq,
                    "viewer_qq": "23456789",
                    "notice": "您访问的空间存在违规信息,已被多名用户举报,暂时无法查看！"
                    if restricted
                    else "",
                    "notice_source": "qzone_top_level_error" if restricted else "qzone_profile",
                    "ready_state": "complete",
                    "panel_count": 1 if restricted else 0,
                    "report_icon_count": 1 if restricted else 0,
                },
            )
        )
    return store


def fingerprint(root: Path) -> dict[str, str]:
    # SQLite may create its normal WAL/shared-memory sidecars during a readonly open.
    # Business records and all other files must remain unchanged.
    return {
        str(file.relative_to(root)): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in root.rglob("*")
        if file.is_file() and not file.name.endswith(("-shm", "-wal"))
    }


def test_closed_task_history_counts_unique_members_without_modifying_files(tmp_path: Path) -> None:
    store = make_task(tmp_path)
    folder = store.folder
    store.close()
    before = fingerprint(tmp_path)
    rows = list_tasks(tmp_path)
    assert rows == [
        {
            "folder": folder,
            "created_at": "2026-09-21T12:00:00+00:00",
            "group_names": "合成甲群、合成乙群",
            "checked": 2,
            "total": 3,
            "restricted": 1,
        }
    ]
    assert fingerprint(tmp_path) == before


def test_history_reads_uncheckpointed_wal_and_leaves_current_store_open(tmp_path: Path) -> None:
    store = make_task(tmp_path)
    try:
        expected = store.summary()
        wal = store.folder / "task.sqlite3-wal"
        before_wal = wal.read_bytes()
        rows = list_tasks(tmp_path)
        assert rows[0]["checked"] == expected["checked"]
        assert rows[0]["restricted"] == expected["restricted"]
        assert rows[0]["total"] == expected["total"]
        assert store.summary() == expected
        assert wal.read_bytes() == before_wal
    finally:
        store.close()


def test_history_uses_readonly_sqlite_uri_and_never_constructs_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_task(tmp_path)
    store.close()
    real_connect = sqlite3.connect
    opened: list[str] = []
    sql: list[str] = []

    def connect(database: str, **kwargs: Any) -> sqlite3.Connection:
        assert kwargs["uri"] is True
        assert "mode=ro" in database
        opened.append(database)
        connection = real_connect(database, **kwargs)
        connection.set_trace_callback(sql.append)
        return connection

    def no_store(*args: object, **kwargs: object) -> None:
        pytest.fail("history must not open Store")

    monkeypatch.setattr(history.sqlite3, "connect", connect)
    monkeypatch.setattr(Store, "__init__", no_store)
    assert len(list_tasks(tmp_path)) == 1
    assert len(opened) == 1
    assert not any(
        statement.upper().startswith(
            ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "BEGIN IMMEDIATE")
        )
        for statement in sql
    )


@pytest.mark.parametrize(
    "tamper",
    [
        "PRAGMA application_id=0",
        "PRAGMA user_version=9",
        "CREATE VIEW unrelated AS SELECT 'not a task'",
        "CREATE TRIGGER bad AFTER INSERT ON groups BEGIN SELECT 1; END",
        "ALTER TABLE groups ADD COLUMN surprise TEXT",
        "UPDATE meta SET value='not-a-date' WHERE key='created_at'",
    ],
)
def test_foreign_or_invalid_schema_is_skipped_without_changes(tmp_path: Path, tamper: str) -> None:
    store = make_task(tmp_path)
    database = store.folder / "task.sqlite3"
    store.close()
    connection = sqlite3.connect(database)
    connection.execute(tamper)
    connection.commit()
    connection.close()
    before = fingerprint(tmp_path)
    assert list_tasks(tmp_path) == []
    assert fingerprint(tmp_path) == before


def test_recent_fifty_use_canonical_directory_names_and_ignore_unrelated_files(
    tmp_path: Path,
) -> None:
    store = make_task(tmp_path, "20260921T120000Z-00000000")
    store.close()
    data = (store.folder / "task.sqlite3").read_bytes()
    for index in range(1, 53):
        folder = tmp_path / f"20260921T12{index:02d}00Z-00000000"
        folder.mkdir()
        (folder / "task.sqlite3").write_bytes(data)
    (tmp_path / "unrelated").mkdir()
    (tmp_path / "99999999T999999Z-00000000").mkdir()
    rows = list_tasks(tmp_path)
    assert len(rows) == 50
    assert rows[0]["folder"] == tmp_path / "20260921T125200Z-00000000"
    assert rows[-1]["folder"] == tmp_path / "20260921T120300Z-00000000"


def test_symlink_task_directory_is_not_followed(tmp_path: Path) -> None:
    external = tmp_path / "outside"
    external.mkdir()
    store = make_task(external)
    store.close()
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    try:
        os.symlink(store.folder, tasks / store.folder.name, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink unavailable on this platform: {type(exc).__name__}")
    before = fingerprint(external)
    assert list_tasks(tasks) == []
    assert fingerprint(external) == before


def test_database_size_and_membership_bounds_are_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_task(tmp_path)
    store.close()
    with monkeypatch.context() as scoped:
        scoped.setattr(history, "_MAX_DATABASE_BYTES", 1)
        assert list_tasks(tmp_path) == []
    with monkeypatch.context() as scoped:
        scoped.setattr(history, "MAX_MEMBERSHIPS", 5)
        assert list_tasks(tmp_path) == []


def test_missing_root_is_empty_and_invalid_folder_is_ignored(tmp_path: Path) -> None:
    assert list_tasks(tmp_path / "missing") == []
    (tmp_path / "20260921T120000Z-0123abcd").mkdir()
    assert list_tasks(tmp_path) == []


def test_reparse_point_directory_is_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_task(tmp_path)
    folder = store.folder
    store.close()
    original_lstat = Path.lstat

    def lstat(path: Path):
        value = original_lstat(path)
        if path == folder:
            return SimpleNamespace(
                st_mode=value.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT
            )
        return value

    monkeypatch.setattr(Path, "lstat", lstat)
    assert list_tasks(tmp_path) == []


def test_directory_enumeration_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    monkeypatch.setattr(history, "_MAX_DIRECTORY_ENTRIES", 1)
    with pytest.raises(InspectionError, match="过大"):
        list_tasks(tmp_path)


def test_actual_sqlite_query_is_interrupted_when_budget_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_task(tmp_path)
    with store.db:
        store.db.executemany(
            "INSERT INTO membership VALUES ('34567890',?)",
            [(str(10000000 + index),) for index in range(5000)],
        )
        store.db.execute("UPDATE groups SET snapshot_count=5003 WHERE group_id='34567890'")
    folder = store.folder
    store.close()
    real_connect = sqlite3.connect
    progress_calls: list[int] = []

    class TrackedConnection(sqlite3.Connection):
        def set_progress_handler(self, handler, instructions):
            def observe():
                result = handler()
                progress_calls.append(result)
                return result

            super().set_progress_handler(observe, instructions)

    def connect(database: str, **kwargs: Any) -> sqlite3.Connection:
        return real_connect(database, factory=TrackedConnection, **kwargs)

    monkeypatch.setattr(history.sqlite3, "connect", connect)
    monkeypatch.setattr(history, "_QUERY_SECONDS", 0)
    assert history._read_task(folder) is None
    assert progress_calls and progress_calls[-1] == 1
