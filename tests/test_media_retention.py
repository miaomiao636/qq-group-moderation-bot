"""Retention failures must remain visible; all files and databases are synthetic."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from app.adapters.qq_official.media import purge_media
from app.db import Base
from app.models import SystemSetting
from app.reports import cleanup, maintenance
from app.runtime import pipeline
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


def _expired_file(directory: Path, name: str) -> Path:
    target = directory / name
    target.write_bytes(b"synthetic-media")
    expired_at = time.time() - 20 * 86400
    os.utime(target, (expired_at, expired_at))
    return target


def test_media_cleanup_deletes_only_expired_complete_files(tmp_path: Path) -> None:
    expired = _expired_file(tmp_path, "expired.jpg")
    incomplete = _expired_file(tmp_path, "incomplete.part")
    recent = tmp_path / "recent.jpg"
    recent.write_bytes(b"synthetic-new-media")
    nested = tmp_path / "nested"
    nested.mkdir()
    nested_file = _expired_file(nested, "not-in-this-cleanup-scope.jpg")

    assert purge_media(tmp_path, retention_days=15) == 1

    assert not expired.exists()
    assert incomplete.exists() and recent.exists() and nested_file.exists()


def test_locked_media_delete_is_a_visible_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expired = _expired_file(tmp_path, "locked.jpg")
    original_unlink = Path.unlink

    def locked_unlink(target: Path, *args: object, **kwargs: object) -> None:
        if target == expired:
            raise PermissionError("synthetic-private-path-must-not-be-logged")
        original_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked_unlink)

    with pytest.raises(PermissionError):
        purge_media(tmp_path, retention_days=15)

    assert expired.exists()


def test_concurrent_media_removal_is_not_a_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expired = _expired_file(tmp_path, "already-removed.jpg")
    original_unlink = Path.unlink

    def concurrent_unlink(target: Path, *args: object, **kwargs: object) -> None:
        original_unlink(target, *args, **kwargs)
        if target == expired:
            raise FileNotFoundError("another-cleaner-already-removed-this-file")

    monkeypatch.setattr(Path, "unlink", concurrent_unlink)

    assert purge_media(tmp_path, retention_days=15) == 0
    assert not expired.exists()


def test_media_cleanup_failure_does_not_claim_filesystem_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    removable = _expired_file(tmp_path, "first.jpg")
    locked = _expired_file(tmp_path, "second.jpg")
    original_iterdir = Path.iterdir
    original_unlink = Path.unlink

    def ordered_files(directory: Path):
        return iter([removable, locked]) if directory == tmp_path else original_iterdir(directory)

    def locked_unlink(target: Path, *args: object, **kwargs: object) -> None:
        if target == locked:
            raise PermissionError("synthetic-locked-media")
        original_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "iterdir", ordered_files)
    monkeypatch.setattr(Path, "unlink", locked_unlink)

    with pytest.raises(PermissionError):
        purge_media(tmp_path, retention_days=15)

    assert not removable.exists()
    assert locked.exists()


async def test_real_media_failure_marks_scheduled_cleanup_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expired = _expired_file(tmp_path, "locked.jpg")
    original_unlink = Path.unlink

    def locked_unlink(target: Path, *args: object, **kwargs: object) -> None:
        if target == expired:
            raise PermissionError("synthetic-private-path-must-not-be-logged")
        original_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked_unlink)
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    settings = cleanup.get_settings().model_copy(
        update={"raw_retention_days": 15, "decision_retention_days": 15}
    )
    monkeypatch.setattr(cleanup, "get_settings", lambda: settings)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            session.add(SystemSetting(key="auto_cleanup_enabled", value="1"))
            await session.commit()

            result = await maintenance.run_cleanup(session)

            assert result == {"status": "failed", "counts": {}, "error": "cleanup_failed"}
            saved_status = await session.get(SystemSetting, "last_cleanup_status")
            saved_error = await session.get(SystemSetting, "last_cleanup_error")
            assert saved_status is not None and saved_status.value == "failed"
            assert saved_error is not None and saved_error.value == "cleanup_failed"
            assert expired.exists()
            assert "synthetic-private-path" not in json.dumps(result)
    finally:
        await engine.dispose()


def test_cleanup_failure_propagates_nonzero_cli_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        maintenance,
        "_run_cleanup",
        AsyncMock(return_value={"status": "failed", "counts": {}, "error": "cleanup_failed"}),
    )

    assert maintenance.main(["cleanup"]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "failed"
    assert output["error"] == "cleanup_failed"
