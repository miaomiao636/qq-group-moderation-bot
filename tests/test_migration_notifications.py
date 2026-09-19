"""Notification migration is additive and leaves moderation data untouched."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from app.config import PROJECT_ROOT


def test_notification_upgrade_constraints_and_downgrade(tmp_path: Path) -> None:
    database = tmp_path / "notifications-migration.db"
    environment = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{database}"}

    def migrate(direction: str, target: str) -> None:
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(PROJECT_ROOT / "alembic.ini"),
                direction,
                target,
            ],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            # 2026-09-19 CI（windows-latest）曾因冷启动让 `alembic upgrade` 卡到 30s 超时而失败，
            # 同 SHA 重跑即三 job 全绿——属环境抖动而非迁移缺陷。这里给足余量，**只放宽超时，
            # 不改任何断言**，避免把 runner 抖动误判成产品问题。
            timeout=180,
        )
        assert process.returncode == 0, process.stderr

    migrate("upgrade", "c2e4f6a8b010")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO system_settings(key,value,updated_at) VALUES('sentinel','unchanged',CURRENT_TIMESTAMP)"
        )
    migrate("upgrade", "head")
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"notification_notices", "notification_deliveries", "notification_state"} <= tables
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO notification_notices(event_key,kind,severity,subject,body,created_at) VALUES('test:1','runtime_offline','page','固定标题','固定摘要',CURRENT_TIMESTAMP)"
        )
        connection.execute(
            "INSERT INTO notification_deliveries(notice_id,channel,created_at,updated_at) VALUES(1,'smtp',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO notification_deliveries(notice_id,channel,created_at,updated_at) VALUES(1,'smtp',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO notification_deliveries(notice_id,channel,created_at,updated_at) VALUES(999,'smtp',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
        assert connection.execute(
            "SELECT value FROM system_settings WHERE key='sentinel'"
        ).fetchone() == ("unchanged",)
    migrate("downgrade", "c2e4f6a8b010")
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert (
            not {"notification_notices", "notification_deliveries", "notification_state"} & tables
        )
        assert connection.execute(
            "SELECT value FROM system_settings WHERE key='sentinel'"
        ).fetchone() == ("unchanged",)
    migrate("upgrade", "head")
