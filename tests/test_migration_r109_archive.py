"""R-109：案件归档迁移只增可见性元数据，业务证据升级/回退保持。"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from app.config import PROJECT_ROOT


def test_archive_migration_preserves_case_evidence_and_has_no_schema_drift(
    tmp_path: Path,
) -> None:
    database = tmp_path / "archive-migration.db"
    environment = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{database}"}

    def alembic(*arguments: str) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(PROJECT_ROOT / "alembic.ini"),
                *arguments,
            ],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr + result.stdout

    alembic("upgrade", "736da9b1cedb")
    expected = (1, "synthetic-migration", "CLOSED", "[7]", '{"history":"synthetic"}')
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO cases "
            "(case_no, group_openid, member_openid, status, violation_ids_json, "
            "audit_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                expected[1],
                "synthetic-g",
                "synthetic-u",
                expected[2],
                expected[3],
                expected[4],
                "2026-09-01 00:00:00",
            ),
        )

    alembic("upgrade", "head")
    select_case = "SELECT id, case_no, status, violation_ids_json, audit_json FROM cases"
    with sqlite3.connect(database) as connection:
        assert connection.execute(select_case).fetchone() == expected
        assert connection.execute("SELECT archived, archived_at FROM cases").fetchone() == (
            0,
            None,
        )
        connection.execute(
            "UPDATE cases SET archived=1, archived_at=? WHERE id=1",
            ("2026-09-14 00:00:00",),
        )

    alembic("downgrade", "736da9b1cedb")
    with sqlite3.connect(database) as connection:
        fields = {row[1] for row in connection.execute("PRAGMA table_info(cases)")}
        assert not {"archived", "archived_at"} & fields
        assert connection.execute(select_case).fetchone() == expected

    alembic("upgrade", "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute(select_case).fetchone() == expected
        # 归档标记在 downgrade 被删除，再升级不是从备份恢复原可见性。
        assert connection.execute("SELECT archived, archived_at FROM cases").fetchone() == (
            0,
            None,
        )
    alembic("check")
