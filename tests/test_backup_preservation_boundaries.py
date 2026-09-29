"""Only registered database artifacts can satisfy the final backup floor."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path

import pytest
from app.reports.backup import backup_sqlite
from app.reports.cleanup import plan_managed_copies, purge_managed_copies


def completed(path, days):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE synthetic(id INTEGER)")
    age = time.time() - days * 86400
    os.utime(path, (age, age))
    return path


def aged_text(path, days):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("synthetic-companion", encoding="utf-8")
    age = time.time() - days * 86400
    os.utime(path, (age, age))
    return path


@pytest.mark.parametrize("name", ["manifest.json", "log-only/backup.log", "unregistered.copy"])
def test_non_database_file_cannot_displace_only_backup(tmp_path, name):
    root = tmp_path / "data"
    database = completed(root / "backups/moderation-only.db", 40)
    unrelated = aged_text(root / "backups" / name, 20)
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in [database, unrelated]}
    plan = plan_managed_copies(root=root)
    assert database not in {Path(item["path"]) for item in plan["files"]}
    purge_managed_copies(root=root)
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before} == before


def test_backup_companion_timestamp_cannot_displace_newer_database(tmp_path):
    root = tmp_path / "data"
    old = completed(root / "backups/older/db.bak", 40)
    old_note = aged_text(old.parent / "restore-metadata.json", 1)
    new = completed(root / "backups/newer/db.bak", 20)
    new_note = aged_text(new.parent / "restore-metadata.json", 20)
    plan = {Path(item["path"]) for item in plan_managed_copies(root=root)["files"]}
    assert old in plan and new not in plan
    purge_managed_copies(root=root)
    assert new.exists() and new_note.exists() and old_note.exists()
    assert not old.exists()


def test_backup_guard_does_not_change_other_registered_directory_expiry(tmp_path):
    root = tmp_path / "data"
    expired = aged_text(root / "_dbg_tmp/ordinary.log", 5)
    unknown = aged_text(root / "unknown/retain.log", 50)
    purge_managed_copies(root=root)
    assert not expired.exists() and unknown.exists()


def test_backup_refuses_directory_link_before_creating_any_copy(tmp_path):
    source = completed(tmp_path / "source/synthetic.db", 0)
    external = tmp_path / "unregistered"
    external.mkdir()
    link = source.parent / "backups"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("directory links unavailable")
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(external)], capture_output=True
        )
        if result.returncode:
            pytest.skip("junction creation unavailable")
    with pytest.raises(ValueError):
        backup_sqlite(f"sqlite+aiosqlite:///{source}")
    assert list(external.iterdir()) == []
    assert source.exists()


def test_known_old_partial_still_expires_without_displacing_database(tmp_path):
    root = tmp_path / "data"
    database = completed(root / "backups/moderation-only.db", 40)
    partial = aged_text(root / "backups/moderation-interrupted.partial", 20)
    purge_managed_copies(root=root)
    assert database.exists() and not partial.exists()
