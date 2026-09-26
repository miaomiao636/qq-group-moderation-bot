"""Bounded, read-only summaries of this tool's own saved task directories."""

from __future__ import annotations

import heapq
import os
import re
import sqlite3
import stat
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

from app.space_inspector.contracts import InspectionError, numeric_id
from app.space_inspector.store import (
    APPLICATION_ID,
    MAX_GROUPS,
    MAX_MEMBERSHIPS,
    MAX_UNIQUE_MEMBERS,
    MAX_VISITS,
    SCHEMA_VERSION,
)

_TASK_NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")
_MAX_DIRECTORY_ENTRIES = 20_000
_MAX_DATABASE_BYTES = 2 * 1024**3
_QUERY_SECONDS = 0.25
_ENUMERATION_SECONDS = 5.0
_COLUMNS = {
    "meta": (("key", "TEXT"), ("value", "TEXT")),
    "groups": (
        ("group_id", "TEXT"),
        ("name", "TEXT"),
        ("declared_count", "INTEGER"),
        ("snapshot_count", "INTEGER"),
        ("saved_at", "TEXT"),
    ),
    "membership": (("group_id", "TEXT"), ("qq", "TEXT")),
    "visits": (
        ("id", "INTEGER"),
        ("qq", "TEXT"),
        ("status", "TEXT"),
        ("reason", "TEXT"),
        ("checked_at", "TEXT"),
        ("evidence_json", "TEXT"),
    ),
    "observations": (
        ("qq", "TEXT"),
        ("visit_id", "INTEGER"),
        ("status", "TEXT"),
        ("reason", "TEXT"),
        ("checked_at", "TEXT"),
        ("evidence_json", "TEXT"),
    ),
}


def _ordinary(path: Path, *, directory: bool) -> bool:
    try:
        value = path.lstat()
    except OSError:
        return False
    if (
        stat.S_ISLNK(value.st_mode)
        or getattr(value, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        return False
    return stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)


def _created_at(value: object) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError
    return value


def _read_task(folder: Path) -> dict[str, object] | None:
    database = folder / "task.sqlite3"
    connection: sqlite3.Connection | None = None
    try:
        if not _ordinary(folder, directory=True) or not _ordinary(database, directory=False):
            return None
        if database.stat().st_size > _MAX_DATABASE_BYTES:
            return None
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = database.with_name(database.name + suffix)
            if (sidecar.exists() or sidecar.is_symlink()) and (
                not _ordinary(sidecar, directory=False)
                or sidecar.stat().st_size > _MAX_DATABASE_BYTES
            ):
                return None
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.05)
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 8192)
        connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 8192)
        # Filesystem checks and opening SQLite may be slow on Windows. Start the
        # SQL budget at the first query; keep its duration and interrupt guard.
        deadline = time.monotonic() + _QUERY_SECONDS
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        if (
            connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
            or connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
        ):
            return None
        schema = connection.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 101"
        ).fetchall()
        if (
            len(schema) > 100
            or any(row[0] not in {"table", "index"} for row in schema)
            or {row[1] for row in schema if row[0] == "table"} != set(_COLUMNS)
            or any(row[0] == "table" and "VIRTUAL" in str(row[2]).upper().split() for row in schema)
        ):
            return None
        for table, expected in _COLUMNS.items():
            columns = tuple(
                (row[1], row[2].upper())
                for row in connection.execute(f"PRAGMA table_info({table})")
            )
            if columns != expected:
                return None
        metadata_rows = connection.execute("SELECT key,value FROM meta LIMIT 5").fetchall()
        metadata = dict(metadata_rows)
        if (
            len(metadata_rows) != 4
            or set(metadata) != {"created_at", "source_self_id", "viewer_qq", "prepared"}
            or metadata["prepared"] not in {"0", "1"}
        ):
            return None
        created_at = _created_at(metadata["created_at"])
        for key in ("source_self_id", "viewer_qq"):
            if metadata[key]:
                numeric_id(metadata[key])
        for table, maximum in (
            ("groups", MAX_GROUPS),
            ("membership", MAX_MEMBERSHIPS),
            ("observations", MAX_UNIQUE_MEMBERS),
            ("visits", MAX_VISITS),
        ):
            count = connection.execute(
                f"SELECT COUNT(*) FROM (SELECT 1 FROM {table} LIMIT ?)", (maximum + 1,)
            ).fetchone()[0]
            if count > maximum:
                return None
        groups = connection.execute(
            "SELECT group_id,name,declared_count,snapshot_count FROM groups ORDER BY group_id LIMIT ?",
            (MAX_GROUPS + 1,),
        ).fetchall()
        names: list[str] = []
        for group_id, name, declared, snapshot in groups:
            numeric_id(group_id)
            if (
                not isinstance(name, str)
                or not 1 <= len(name) <= 500
                or any(unicodedata.category(char) in {"Cc", "Cs"} for char in name)
            ):
                return None
            if (
                type(declared) is not int
                or type(snapshot) is not int
                or not 0 <= declared <= 20000
                or not 0 <= snapshot <= 20000
            ):
                return None
            names.append(name)
        if connection.execute(
            "SELECT 1 FROM observations WHERE status NOT IN ('RESTRICTION_OBSERVED','UNCONFIRMED','BLOCKED') OR status IS NULL LIMIT 1"
        ).fetchone():
            return None
        total, checked, restricted = connection.execute(
            """
SELECT COUNT(*), COALESCE(SUM(o.status IN ('RESTRICTION_OBSERVED','UNCONFIRMED')),0),
       COALESCE(SUM(o.status='RESTRICTION_OBSERVED'),0)
FROM (SELECT DISTINCT qq FROM membership LIMIT ?) m LEFT JOIN observations o ON o.qq=m.qq
""",
            (MAX_UNIQUE_MEMBERS + 1,),
        ).fetchone()
        if (
            not 0 <= restricted <= checked <= total <= MAX_UNIQUE_MEMBERS
            or time.monotonic() >= deadline
        ):
            return None
        display = "、".join(name[:60] for name in names[:3])
        if len(names) > 3:
            display += f" 等 {len(names)} 个群"
        return {
            "folder": folder,
            "created_at": created_at,
            "group_names": display or "尚未保存群快照",
            "checked": checked,
            "total": total,
            "restricted": restricted,
        }
    except (OSError, ValueError, TypeError, sqlite3.Error, InspectionError):
        return None
    finally:
        if connection is not None:
            connection.close()


def list_tasks(root: Path) -> list[dict[str, object]]:
    """Read at most the latest fifty canonical task names; never recurse or use Store."""
    root = Path(os.path.abspath(root))
    if not root.exists():
        return []
    if not _ordinary(root, directory=True):
        raise InspectionError("历史任务目录不可用，请检查本机任务目录。")
    deadline = time.monotonic() + _ENUMERATION_SECONDS
    candidates: list[str] = []
    try:
        with os.scandir(root) as entries:
            for number, entry in enumerate(entries, 1):
                if number > _MAX_DIRECTORY_ENTRIES or time.monotonic() >= deadline:
                    raise InspectionError("历史任务目录过大或读取超时，请稍后重试。")
                if not _TASK_NAME.fullmatch(entry.name):
                    continue
                try:
                    datetime.strptime(entry.name[:16], "%Y%m%dT%H%M%SZ")
                except ValueError:
                    continue
                if _ordinary(root / entry.name, directory=True):
                    candidates.append(entry.name)
        tasks = []
        for name in heapq.nlargest(50, candidates):
            task = _read_task(root / name)
            if task is not None:
                tasks.append(task)
        return tasks
    except OSError:
        raise InspectionError("无法读取历史任务，请稍后重试。") from None
