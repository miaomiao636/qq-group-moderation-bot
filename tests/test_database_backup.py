"""Backups must include committed WAL rows from the configured database."""

import sqlite3
from contextlib import closing

import pytest
from app.reports.backup import backup_sqlite


def test_online_backup_uses_configured_database_and_includes_wal(tmp_path) -> None:
    source = tmp_path / "custom store.db"
    with closing(sqlite3.connect(source)) as live:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("CREATE TABLE action_audit (id INTEGER PRIMARY KEY, state TEXT)")
        live.commit()
        live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        live.execute("INSERT INTO action_audit VALUES (1, 'SUCCEEDED')")
        live.commit()
        backup = backup_sqlite(f"sqlite+aiosqlite:///{source}")
        assert backup.parent == tmp_path / "backups"
        assert backup != source
        with closing(sqlite3.connect(backup)) as restored:
            assert restored.execute("SELECT state FROM action_audit").fetchall() == [("SUCCEEDED",)]
            assert restored.execute("PRAGMA quick_check").fetchone() == ("ok",)
        another = backup_sqlite(f"sqlite+aiosqlite:///{source}")
        assert another != backup and backup.exists()
    assert not list((tmp_path / "backups").glob("*.partial"))


@pytest.mark.parametrize("url", ["sqlite+aiosqlite:///:memory:", "postgresql://localhost/test"])
def test_unsupported_backup_never_claims_success(url) -> None:
    with pytest.raises(ValueError):
        backup_sqlite(url)


def test_missing_source_is_not_silently_created(tmp_path) -> None:
    source = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        backup_sqlite(f"sqlite+aiosqlite:///{source}")
    assert not source.exists()


def test_backup_timeout_removes_partial_file(tmp_path) -> None:
    source = tmp_path / "source.db"
    with closing(sqlite3.connect(source)) as db:
        db.execute("CREATE TABLE data (id INTEGER)")
    with pytest.raises(TimeoutError):
        backup_sqlite(f"sqlite+aiosqlite:///{source}", timeout_seconds=0)
    assert not list((tmp_path / "backups").iterdir())
