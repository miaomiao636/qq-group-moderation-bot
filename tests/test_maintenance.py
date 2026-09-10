"""Scheduled cleanup is an explicit, default-off, observable CLI operation."""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from app.db import Base
from app.models import ProcessedEvent, SystemSetting
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


@pytest.fixture
async def maintenance_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
    finally:
        await engine.dispose()


@pytest.mark.parametrize("setting", [None, "0", "unexpected"])
async def test_disabled_cleanup_preserves_expired_data(
    maintenance_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, setting: str | None
) -> None:
    maintenance = importlib.import_module("app.reports.maintenance")
    session = maintenance_session
    session.add(
        ProcessedEvent(
            message_id="expired-test",
            processed_at=datetime.now(UTC) - timedelta(days=200),
        )
    )
    if setting is not None:
        session.add(SystemSetting(key="auto_cleanup_enabled", value=setting))
    await session.commit()
    purge = AsyncMock(side_effect=AssertionError("disabled cleanup must never purge"))
    monkeypatch.setattr("app.reports.cleanup.purge_expired", purge)

    result = await maintenance.run_cleanup(session)

    assert result["status"] == "skipped"
    assert result["counts"] == {}
    assert await session.get(ProcessedEvent, "expired-test") is not None
    assert (await session.get(SystemSetting, "last_cleanup_status")).value == "skipped"
    assert (await session.get(SystemSetting, "last_cleanup_result")).value == "{}"
    assert datetime.fromisoformat((await session.get(SystemSetting, "last_cleanup_at")).value)
    purge.assert_not_awaited()


async def test_enabled_cleanup_records_only_counts(
    maintenance_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    maintenance = importlib.import_module("app.reports.maintenance")
    session = maintenance_session
    session.add(SystemSetting(key="auto_cleanup_enabled", value="1"))
    await session.commit()
    counts = {"processed_events_deleted": 2, "inbox_payloads_purged": 3}
    purge = AsyncMock(return_value=counts)
    monkeypatch.setattr("app.reports.cleanup.purge_expired", purge)

    result = await maintenance.run_cleanup(session)

    assert result["status"] == "succeeded"
    assert result["counts"] == counts
    assert json.loads((await session.get(SystemSetting, "last_cleanup_result")).value) == counts
    assert (await session.get(SystemSetting, "last_cleanup_error")).value == ""
    purge.assert_awaited_once_with(session)


async def test_failed_cleanup_records_safe_failure_and_rolls_back(
    maintenance_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    maintenance = importlib.import_module("app.reports.maintenance")
    session = maintenance_session
    session.add(SystemSetting(key="auto_cleanup_enabled", value="1"))
    await session.commit()

    async def fail(dirty_session: AsyncSession) -> dict[str, int]:
        dirty_session.add(SystemSetting(key="partial_uncommitted", value="uncommitted"))
        await dirty_session.flush()
        raise RuntimeError("private-message-content must never appear in logs")

    monkeypatch.setattr("app.reports.cleanup.purge_expired", fail)

    result = await maintenance.run_cleanup(session)

    assert result["status"] == "failed"
    assert result["error"] == "cleanup_failed"
    assert await session.get(SystemSetting, "partial_uncommitted") is None
    assert (await session.get(SystemSetting, "last_cleanup_status")).value == "failed"
    metadata = dict((await session.execute(select(SystemSetting.key, SystemSetting.value))).all())
    assert "private-message-content" not in json.dumps(metadata)
    assert metadata["last_cleanup_result"] == "{}"
    assert "private-message-content" not in json.dumps(result)


@pytest.mark.parametrize("status,exit_code", [("skipped", 0), ("succeeded", 0), ("failed", 1)])
def test_cli_exit_status_and_structured_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    exit_code: int,
) -> None:
    maintenance = importlib.import_module("app.reports.maintenance")
    monkeypatch.setattr(
        maintenance,
        "_run_cleanup",
        AsyncMock(return_value={"status": status, "counts": {}, "error": ""}),
    )
    assert maintenance.main(["cleanup"]) == exit_code
    output = json.loads(capsys.readouterr().out)
    assert output["event"] == "maintenance_cleanup"
    assert output["entry_point"] == "cli"
    assert output["run_id"]
    assert output["status"] == status


def test_cli_startup_failure_never_prints_raw_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    maintenance = importlib.import_module("app.reports.maintenance")
    monkeypatch.setattr(
        maintenance, "_run_cleanup", AsyncMock(side_effect=RuntimeError("private-config-value"))
    )
    assert maintenance.main(["cleanup"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"] == "startup_or_metadata_failed"
    assert "private-config-value" not in captured.out + captured.err


def test_actual_cli_uses_temporary_database_and_propagates_failure(tmp_path: Path) -> None:
    """Real subprocess boundary; forced failure occurs before any media cleanup."""
    project = Path(__file__).resolve().parents[1]
    database = tmp_path / "maintenance.db"
    environment = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{database.as_posix()}",
        "AI_ENABLED": "false",
        "ACTION_MODE": "SHADOW",
        "ONEBOT_ACTIONS_ENABLED": "false",
    }
    migrated = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert migrated.returncode == 0, migrated.stderr
    command = [sys.executable, "-m", "app.reports.maintenance", "cleanup"]
    disabled = subprocess.run(
        command, cwd=project, env=environment, capture_output=True, text=True, timeout=30
    )
    assert disabled.returncode == 0
    assert json.loads(disabled.stdout)["status"] == "skipped"

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM system_settings WHERE key='last_cleanup_status'"
        ).fetchone() == ("skipped",)
        connection.execute(
            "INSERT INTO system_settings(key,value,updated_at) VALUES(?,?,?)",
            ("auto_cleanup_enabled", "1", datetime.now(UTC).isoformat()),
        )
        # Destruction is limited to this test-created empty database table.
        connection.execute("DROP TABLE action_logs")
    failed = subprocess.run(
        command, cwd=project, env=environment, capture_output=True, text=True, timeout=30
    )
    assert failed.returncode == 1
    assert json.loads(failed.stdout)["error"] == "cleanup_failed"
    assert "no such table" not in failed.stdout + failed.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM system_settings WHERE key='last_cleanup_status'"
        ).fetchone() == ("failed",)
