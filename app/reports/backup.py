"""Consistent, bounded SQLite online backups of the configured database.

Uses Connection.backup (Python 3.12 standard library), not a copy of the main
file: committed data may still live in WAL. Backups are private local data.
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from app.core.fs_guard import is_link_like


def backup_sqlite(
    database_url: str, *, timeout_seconds: float = 30, destination_dir: Path | None = None
) -> Path:
    url = make_url(database_url)
    if (
        url.get_backend_name() != "sqlite"
        or not url.database
        or url.database == ":memory:"
        or url.query
    ):
        raise ValueError("online backup requires the configured file-backed SQLite database")
    source_path = Path(url.database).resolve(strict=True)
    if not source_path.is_file():
        raise ValueError("database source is not a file")
    backup_dir = destination_dir if destination_dir is not None else source_path.parent / "backups"
    if backup_dir.exists() and is_link_like(backup_dir):
        raise ValueError("backup directory cannot be a link or reparse point")
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    if is_link_like(backup_dir):
        raise ValueError("backup directory cannot be a link or reparse point")
    prefix = f"moderation-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-"
    with tempfile.NamedTemporaryFile(
        prefix=prefix, suffix=".partial", dir=backup_dir, delete=False
    ) as reserved:
        partial = Path(reserved.name)
    deadline = time.monotonic() + timeout_seconds

    def check_deadline(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("database backup exceeded deadline")

    try:
        with (
            closing(
                sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True, timeout=0.5)
            ) as source,
            closing(sqlite3.connect(partial, timeout=0.5)) as target,
        ):
            source.backup(target, pages=128, progress=check_deadline, sleep=0.05)
            target.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            if target.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("backup integrity check failed")
        destination = partial.with_suffix(".db")
        if destination.exists():
            raise FileExistsError("backup destination already exists")
        partial.rename(destination)
        return destination
    except BaseException:
        # Only the uniquely reserved artifact from this call is removed.
        partial.unlink(missing_ok=True)
        raise
