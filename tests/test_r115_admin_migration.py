# ruff: noqa: E402  (R-115 入库探针：保留原文件 import 顺序)
"""Independent review probe. Use README commands and the repository test bootstrap."""

import os
import sys

# 入库说明（R-115 正式回归，来源：主审交付包）：原探针要求在仓库外运行并由外部
# conftest 引导；迁入 tests/ 后由 tests/conftest.py 自动提供隔离环境（临时迁移库 /
# SHADOW / 关闭真实模型与通知），因此不再做外部引导检查。


"""Extra R-115 W02-W04 verification; run with repo conftest, synthetic temp DBs only."""

import sqlite3
import subprocess

import pytest
from app.config import PROJECT_ROOT
from app.db import get_head_revision
from app.moderation.allowlist import load_allowlist_terms
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

OLD = "a7c3e91f0b24"
# 不在探针里硬编码 head：R-115 之后的每个迁移都会改变 head，硬编码会让
# `alembic check`（要求"数据库 == 代码 head"）随新迁移必然失败。这里改为
# 从迁移脚本目录读取当前 head，保持探针意图（升级到最新并校验一致性）不变。
NEW = get_head_revision()
COLUMNS = "id,term,normalized,enabled,created_by,created_at,updated_at"


def migrate(database, revision, *, operation="upgrade", expect_success=True):
    environment = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{database}"}
    command = [
        sys.executable,
        "-m",
        "alembic",
        "-c",
        str(PROJECT_ROOT / "alembic.ini"),
        operation,
    ]
    if revision is not None:
        command.append(revision)
    result = subprocess.run(
        command,
        env=environment,
        cwd=database.parent,
        text=True,
        capture_output=True,
        timeout=30,
    )
    if expect_success:
        assert result.returncode == 0, result.stderr + result.stdout
    else:
        assert result.returncode != 0
    return result


def old_row(number, term, normalized, enabled):
    return (
        number,
        term,
        normalized,
        enabled,
        "synthetic-migration-admin",
        "2026-09-01 01:02:03",
        "2026-09-15 04:05:06",
    )


def read_rows(database):
    with sqlite3.connect(database) as db:
        return db.execute(f"SELECT {COLUMNS} FROM allowlist_terms ORDER BY id").fetchall()


def seed_legacy(database, rows):
    migrate(database, OLD)
    with sqlite3.connect(database) as db:
        db.executemany(f"INSERT INTO allowlist_terms ({COLUMNS}) VALUES (?,?,?,?,?,?,?)", rows)
        db.execute(
            "INSERT INTO system_settings (key,value,updated_at) VALUES ('synthetic-old-settings','keep','2026-09-16 00:00:00')"
        )


def test_new_migration_preserves_real_old_rows_and_monotonic_identity(tmp_path):
    database = tmp_path / "legacy-words.db"
    rows = [
        old_row(1, "Synthetic Alpha", "synthetic alpha", 1),
        old_row(27, "Synthetic Beta", "synthetic beta", 0),
    ]
    seed_legacy(database, rows)
    migrate(database, NEW)
    assert read_rows(database) == rows
    with sqlite3.connect(database) as db:
        assert (
            "AUTOINCREMENT"
            in db.execute("SELECT sql FROM sqlite_master WHERE name='allowlist_terms'").fetchone()[
                0
            ]
        )
        assert (
            db.execute("SELECT seq FROM sqlite_sequence WHERE name='allowlist_terms'").fetchone()[0]
            >= 27
        )
        db.execute(
            f"INSERT INTO allowlist_terms ({COLUMNS[3:]}) VALUES (?,?,?,?,?,?)",
            old_row(0, "Synthetic New", "synthetic new", 1)[1:],
        )
        first_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        assert first_id > 27
        db.execute("DELETE FROM allowlist_terms WHERE id=?", (first_id,))
        db.execute(
            f"INSERT INTO allowlist_terms ({COLUMNS[3:]}) VALUES (?,?,?,?,?,?)",
            old_row(0, "Synthetic Next", "synthetic next", 1)[1:],
        )
        second_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        assert second_id > first_id
        assert db.execute(
            "SELECT value FROM system_settings WHERE key='synthetic-old-settings'"
        ).fetchone() == ("keep",)
    migrate(database, None, operation="check")
    expected_after_insert = read_rows(database)
    migrate(database, OLD, operation="downgrade")
    assert read_rows(database) == expected_after_insert
    with sqlite3.connect(database) as db:
        assert (
            "AUTOINCREMENT"
            not in db.execute(
                "SELECT sql FROM sqlite_master WHERE name='allowlist_terms'"
            ).fetchone()[0]
        )
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='ix_allowlist_terms_normalized'"
        ).fetchone()
    migrate(database, NEW)
    assert read_rows(database) == expected_after_insert
    migrate(database, None, operation="check")


@pytest.mark.parametrize("states", [(1, 1), (1, 0), (0, 1), (0, 0)])
def test_duplicate_legacy_words_abort_migration_without_erasing_or_merging(tmp_path, states):
    database = tmp_path / "duplicates.db"
    rows = [
        old_row(3, "Synthetic Duplicate", "synthetic duplicate", states[0]),
        old_row(9, "SYNTHETIC DUPLICATE", "synthetic duplicate", states[1]),
    ]
    seed_legacy(database, rows)
    result = migrate(database, NEW, expect_success=False)
    assert "需人工合并后再迁移" in result.stderr
    assert read_rows(database) == rows
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (OLD,)
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '_allowlist_terms_%'"
            ).fetchall()
            == []
        )
        assert db.execute(
            "SELECT value FROM system_settings WHERE key='synthetic-old-settings'"
        ).fetchone() == ("keep",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "states,expected",
    [((1, 1), True), ((1, 0), False), ((0, 1), False), ((0, 0), False)],
)
async def test_legacy_duplicate_reader_requires_every_equivalent_row_enabled(
    tmp_path, states, expected
):
    database = tmp_path / "reader-legacy.db"
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE allowlist_terms (normalized TEXT NOT NULL, enabled BOOLEAN NOT NULL)"
        )
        db.executemany(
            "INSERT INTO allowlist_terms (normalized, enabled) VALUES (?,?)",
            [
                ("synthetic same", states[0]),
                ("synthetic same", states[1]),
                ("synthetic control", 1),
            ],
        )
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    try:
        async with AsyncSession(engine) as session:
            result = await load_allowlist_terms(session)
            assert ("synthetic same" in result) is expected
            assert "synthetic control" in result
    finally:
        await engine.dispose()
