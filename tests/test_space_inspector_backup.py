"""Offline inspector backup tests; no browser, NapCat or real AppData access."""

import hashlib
import json
import sqlite3

import pytest
from app.phone_inspector.locks import FileLock
from app.phone_inspector.pages import InspectionError as LockError
from app.space_inspector.store import Store

from tests.test_space_inspector_store import MEMBER, OTHER, make_store, observation

TASK = "20260926T010203Z-1234abcd"


def sample(tmp_path):
    root = tmp_path / "inspector"
    folder = root / "tasks" / TASK
    store = make_store(folder)
    store.save(observation())
    exports = tmp_path / "exports"
    exports.mkdir()
    store.export(exports)
    store.close()
    (root / "browser").mkdir()
    (root / "browser/private-cookie").write_text("PRIVATE_SESSION_SENTINEL")
    (root / "export-settings.json").write_text(json.dumps({"export_root": str(exports)}))
    return root, exports


def test_offline_backup_restores_resume_and_exports_without_credentials(tmp_path):
    from app.space_inspector.backup import create_backup, restore_backup

    root, exports = sample(tmp_path)
    backup = tmp_path / "backup"
    result = create_backup(root, backup, [exports], reserve=0)
    assert result["task_count"] == 1
    target = tmp_path / "restored"
    restore_backup(backup, target, reserve=0)
    resumed = Store(target / "inspector/tasks" / TASK)
    try:
        assert resumed.pending() == [OTHER]
        assert resumed.summary()["restricted"] == 1
        assert resumed.db.execute("select qq from visits").fetchone()[0] == MEMBER
    finally:
        resumed.close()
    assert list((target / "exports").rglob("restricted.csv"))
    assert not (target / "inspector/export-settings.json").exists()
    assert (target / "settings/export-settings.json").exists()
    assert all(
        b"PRIVATE_SESSION_SENTINEL" not in p.read_bytes() for p in backup.rglob("*") if p.is_file()
    )


@pytest.mark.parametrize("lock", ["desktop.lock", f"tasks/{TASK}/task.lock"])
def test_active_inspector_cannot_be_backed_up(tmp_path, lock):
    from app.space_inspector.backup import create_backup

    root, exports = sample(tmp_path)
    with FileLock(root / lock), pytest.raises(LockError):
        create_backup(root, tmp_path / "backup", [exports], reserve=0)
    assert not (tmp_path / "backup/COMPLETE.json").exists()


def test_all_tasks_including_older_than_ui_limit_are_backed_up(tmp_path):
    from app.space_inspector.backup import create_backup

    root, exports = sample(tmp_path)
    for i in range(55):
        with sqlite3.connect(root / "tasks" / TASK / "task.sqlite3") as db:
            folder = root / "tasks" / f"20260926T010203Z-{i:08x}"
            folder.mkdir()
            with sqlite3.connect(folder / "task.sqlite3") as copied:
                db.backup(copied)
    receipt = create_backup(root, tmp_path / "backup", [exports], reserve=0)
    assert receipt["task_count"] == 56


def test_tamper_and_existing_restore_target_never_verify(tmp_path):
    from app.space_inspector.backup import create_backup, restore_backup

    root, exports = sample(tmp_path)
    backup = tmp_path / "backup"
    create_backup(root, backup, [exports], reserve=0)
    with pytest.raises(ValueError):
        restore_backup(backup, root, reserve=0)
    file = next(backup.rglob("restricted.csv"))
    file.write_bytes(b"changed")
    with pytest.raises(ValueError):
        restore_backup(backup, tmp_path / "restored", reserve=0)
    assert not (tmp_path / "restored/RESTORE_VERIFIED.json").exists()


def test_export_root_does_not_collect_unrelated_desktop_files(tmp_path):
    from app.space_inspector.backup import create_backup

    root, exports = sample(tmp_path)
    (exports / "unrelated.txt").write_bytes(b"UNRELATED_SENTINEL")
    backup = tmp_path / "backup"
    result = create_backup(root, backup, [exports], reserve=0)
    assert result["excluded_export_entries"] == 1
    assert all(
        b"UNRELATED_SENTINEL" not in p.read_bytes() for p in backup.rglob("*") if p.is_file()
    )


@pytest.mark.parametrize("damage", ["identity", "count", "history"])
def test_unloadable_task_never_gets_successful_backup(tmp_path, damage):
    from app.space_inspector.backup import create_backup
    from app.space_inspector.contracts import InspectionError

    root, exports = sample(tmp_path)
    with sqlite3.connect(root / "tasks" / TASK / "task.sqlite3") as db:
        if damage == "identity":
            db.execute("UPDATE meta SET value='bad' WHERE key='source_self_id'")
        elif damage == "count":
            db.execute("UPDATE groups SET snapshot_count=100")
        else:
            db.execute("UPDATE observations SET reason='different'")
    with pytest.raises((ValueError, InspectionError)):
        create_backup(root, tmp_path / "backup", [exports], reserve=0)
    assert not (tmp_path / "backup/COMPLETE.json").exists()


def test_database_hash_does_not_count_size_twice(tmp_path):
    from app.space_inspector.backup import Budget, _copy

    path = tmp_path / "snapshot"
    path.write_bytes(b"a" * 4096)
    budget = Budget(tmp_path, 4096, 0, 10)
    budget.used = 4096
    assert _copy(path, None, budget)[1] == 4096


@pytest.mark.parametrize("damage", ["count", "missing_export", "outside", "case_duplicate"])
def test_restore_rejects_inconsistent_or_unsafe_manifest(tmp_path, damage):
    from app.space_inspector.backup import create_backup, restore_backup

    root, exports = sample(tmp_path)
    backup = tmp_path / "backup"
    create_backup(root, backup, [exports], reserve=0)
    manifest = json.loads((backup / "manifest.json").read_bytes())
    if damage == "count":
        manifest["task_count"] += 1
    elif damage == "missing_export":
        manifest["files"] = [e for e in manifest["files"] if not e["path"].endswith("/COMPLETE")]
    elif damage == "outside":
        manifest["files"][0]["path"] = "../outside"
    else:
        manifest["files"].append(
            {**manifest["files"][0], "path": manifest["files"][0]["path"].upper()}
        )
    raw = json.dumps(manifest).encode()
    (backup / "manifest.json").write_bytes(raw)
    (backup / "COMPLETE.json").write_text(
        json.dumps({"manifest_sha256": hashlib.sha256(raw).hexdigest()})
    )
    with pytest.raises((ValueError, OSError)):
        restore_backup(backup, tmp_path / "restored", reserve=0)
    assert not (tmp_path / "restored/RESTORE_VERIFIED.json").exists()


def test_wal_and_cache_preserve_identity_age_and_multiple_export_locations(tmp_path):
    from datetime import UTC, datetime, timedelta

    from app.space_inspector.backup import create_backup, restore_backup
    from app.space_inspector.cache import ObservationCache

    from tests.test_space_inspector_store import VIEWER

    root, exports = sample(tmp_path)
    task = Store(root / "tasks" / TASK)
    cache = ObservationCache(root / "observations.sqlite3")
    old = tmp_path / "older-exports"
    old.mkdir()
    stamp = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    try:
        task.db.execute("PRAGMA wal_autocheckpoint=0")
        task.save(observation(qq=OTHER, checked_at=stamp))
        cache.remember(task, OTHER)
        task.export(old)
        task.export(task.folder / "exports")
        assert (task.folder / "task.sqlite3-wal").stat().st_size > 0
        backup = tmp_path / "backup"
        create_backup(root, backup, [exports, old], reserve=0)
    finally:
        task.close()
        cache.close()
    target = tmp_path / "restored"
    restore_backup(backup, target, reserve=0)
    restored = Store(target / "inspector/tasks" / TASK)
    try:
        assert restored.pending() == []
        assert (
            restored.db.execute(
                "SELECT checked_at FROM observations WHERE qq=?", (OTHER,)
            ).fetchone()[0]
            == stamp
        )
    finally:
        restored.close()
    with sqlite3.connect(target / "inspector/observations.sqlite3") as db:
        assert db.execute("SELECT viewer,checked_at,source_task FROM cached").fetchall() == [
            (VIEWER, stamp, TASK)
        ]
    assert len(list((target / "exports").rglob("report.json"))) == 3


def test_capacity_failure_never_writes_complete_marker(tmp_path):
    from app.space_inspector.backup import create_backup

    root, exports = sample(tmp_path)
    with pytest.raises(ValueError):
        create_backup(root, tmp_path / "backup", [exports], reserve=0, maximum=1)
    assert not (tmp_path / "backup/COMPLETE.json").exists()


def test_cache_with_unsupported_schema_cannot_be_verified(tmp_path):
    from app.space_inspector.backup import create_backup
    from app.space_inspector.cache import ObservationCache

    root, exports = sample(tmp_path)
    cache = ObservationCache(root / "observations.sqlite3")
    cache.db.execute("CREATE INDEX extra ON cached(qq)")
    cache.db.commit()
    cache.close()
    with pytest.raises(ValueError, match="SCHEMA"):
        create_backup(root, tmp_path / "backup", [exports], reserve=0)
    assert not (tmp_path / "backup/COMPLETE.json").exists()


def test_cache_size_limit_matches_runtime_loader(tmp_path, monkeypatch):
    from app.space_inspector import backup
    from app.space_inspector.cache import ObservationCache

    root, exports = sample(tmp_path)
    cache = ObservationCache(root / "observations.sqlite3")
    cache.close()
    monkeypatch.setattr(backup, "MAX_CACHE", 1)
    with pytest.raises(ValueError, match="TOO_LARGE"):
        backup.create_backup(root, tmp_path / "backup", [exports], reserve=0)


def test_legacy_summary_only_export_is_preserved(tmp_path):
    from app.space_inspector.backup import create_backup, restore_backup

    root, exports = sample(tmp_path)
    folder = next(exports.iterdir())
    for file in folder.iterdir():
        if file.name not in {"report.csv", "restricted.csv", "report.json", "COMPLETE"}:
            file.unlink()
    backup = tmp_path / "backup"
    create_backup(root, backup, [exports], reserve=0)
    restore_backup(backup, tmp_path / "restored", reserve=0)
    assert len(list((tmp_path / "restored/exports").rglob("report.csv"))) == 1
