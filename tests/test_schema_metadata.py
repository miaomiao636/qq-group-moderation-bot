"""Future migrations must not mistake retained tables/columns for deletions."""

from __future__ import annotations

import os
import subprocess
import sys

from app.config import PROJECT_ROOT


def test_migrated_schema_matches_registered_metadata(tmp_path):
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'schema.db'}",
        "APP_ENV": "test",
        "ACTION_MODE": "SHADOW",
        "ONEBOT_WS_ENABLED": "false",
        "AI_ENABLED": "false",
    }
    for arguments in (("upgrade", "head"), ("check",)):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", str(PROJECT_ROOT / "alembic.ini"), *arguments],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
