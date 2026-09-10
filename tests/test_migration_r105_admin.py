"""The additive control migration preserves history and never re-arms old actions."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys

from app.config import PROJECT_ROOT


def test_provider_settings_upgrade_and_safe_downgrade(tmp_path):
    database = tmp_path / "migration.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{database}"}

    def migrate(command, target):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(PROJECT_ROOT / "alembic.ini"),
                command,
                target,
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr

    migrate("upgrade", "e1a4b8c2d3f5")
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO group_settings (group_openid,name,moderation_enabled,action_enabled,updated_at) VALUES (?, ?, 0, 1, CURRENT_TIMESTAMP)",
            [
                ("unique", "unique-name"),
                ("ambiguous", "ambiguous-name"),
                ("unknown", "unknown-name"),
            ],
        )
        connection.executemany(
            "INSERT INTO group_provider_routes (message_provider,external_group_id,action_provider,updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            [
                ("onebot", "unique", "onebot"),
                ("onebot", "ambiguous", "onebot"),
                ("qq_official", "ambiguous", "qq_official"),
            ],
        )
    migrate("upgrade", "b1d3f5a7c909")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM group_settings").fetchone()[0] == 3
        assert (
            connection.execute(
                "SELECT count(*) FROM group_settings WHERE action_enabled=1"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM provider_group_settings WHERE action_enabled=1"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute(
            "SELECT provider, moderation_enabled FROM provider_group_settings WHERE external_group_id='unique'"
        ).fetchall() == [("onebot", 0)]
        assert (
            connection.execute(
                "SELECT count(*) FROM provider_group_settings WHERE external_group_id='ambiguous' AND moderation_enabled=1"
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                "SELECT provider FROM group_action_owners WHERE external_group_id='ambiguous'"
            ).fetchone()[0]
            == ""
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM provider_group_settings WHERE external_group_id='unknown'"
            ).fetchone()[0]
            == 0
        )
    migrate("downgrade", "e1a4b8c2d3f5")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM group_settings").fetchone()[0] == 3
        assert (
            connection.execute(
                "SELECT count(*) FROM group_settings WHERE action_enabled=1"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT name FROM group_settings WHERE group_openid='unknown'"
            ).fetchone()[0]
            == "unknown-name"
        )
    migrate("upgrade", "head")
