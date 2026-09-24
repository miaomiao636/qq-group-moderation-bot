# ruff: noqa: E402, I001, F401, F811
# RECALL-CONFIRM REGISTERED ADAPTATION: only the expected Alembic head literal changes.
# Original: dd9454f; AST/metadata-only proof: docs/2026-09-24-recall-confirmation.md.
# A2 REGISTERED ADAPTATION: original bytes are sealed under docs/evidence/authority-a2-20260922/legacy-probes/.
# Historical nodeids are retained; current contracts and every changed AST node are registered in docs/2026-09-22-authority-a2-adaptations.md.
# Reviewer round-6 probe pack (85b0c0b), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
"""Observed startup boundary, not permission to migrate a production database."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from tests.test_r132_member_import_review import migrate
from app.config import PROJECT_ROOT


def child(database, *args):
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{database}",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "IMAGE_HASH_MODE": "off",
    }
    return subprocess.run(
        [sys.executable, *args],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
    )


def test_new_code_old_schema_blocks_startup_and_daily_cleanup_even_when_hash_off(tmp_path):
    database = tmp_path / "synthetic-old.db"
    migrate(database, "c9a1f4d27e30")
    check = child(
        database,
        "-c",
        "import asyncio; from app.db import check_db_migrated; asyncio.run(check_db_migrated())",
    )
    assert check.returncode != 0
    assert "c9a1f4d27e30" in check.stderr and "f3c8a9d12064" in check.stderr
    cleanup = child(database, "-m", "app.reports.maintenance", "cleanup")
    assert cleanup.returncode == 1, cleanup.stdout + cleanup.stderr
    assert json.loads(cleanup.stdout)["error"] == "startup_or_metadata_failed"
    # Positive control on this synthetic DB only: applying head unblocks startup.
    migrate(database, "head")
    check_after = child(
        database,
        "-c",
        "import asyncio; from app.db import check_db_migrated; asyncio.run(check_db_migrated())",
    )
    assert check_after.returncode == 0, check_after.stdout + check_after.stderr
    cleanup_after = child(database, "-m", "app.reports.maintenance", "cleanup")
    assert cleanup_after.returncode == 0
    assert json.loads(cleanup_after.stdout)["status"] == "skipped"
