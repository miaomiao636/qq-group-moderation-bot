# ruff: noqa: E402, I001, F401, F811
# Reviewer round-6 probe pack (85b0c0b), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
"""Independent synthetic controls for 1e70724; no live DB, media, or network."""

from __future__ import annotations

import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from PIL import Image
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import ImageAllowlist
from app.moderation import image_hash


ROOT = Path.cwd()


def _picture(size=(80, 80)) -> bytes:
    image = Image.new("L", size)
    for y in range(size[1]):
        for x in range(size[0]):
            image.putpixel((x, y), (x * 7 + y * 3) % 256)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _alembic(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(
        os.environ,
        DATABASE_URL=f"sqlite+aiosqlite:///{path}",
        APP_ENV="test",
        AI_ENABLED="false",
        ACTION_MODE="SHADOW",
        ONEBOT_ACTIONS_ENABLED="false",
        NOTIFICATIONS_ENABLED="false",
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def test_real_migration_constraints_check_and_roundtrip(tmp_path):
    path = tmp_path / "完整迁移 control.sqlite"
    _alembic(path, "upgrade", "c9a1f4d27e30")
    with sqlite3.connect(path) as db:
        baseline = dict(
            db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' AND name!='alembic_version'"
            )
        )
        db.execute(
            "INSERT INTO system_meta(key,value,created_at) VALUES('review-control','preserve-me','2026-09-19 00:00:00')"
        )
        db.commit()
    _alembic(path, "upgrade", "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("d4b7c1e9a502",)
        columns = {row[1]: row for row in db.execute("PRAGMA table_info(image_allowlist)")}
        assert set(columns) == {
            "id",
            "phash",
            "note",
            "source",
            "hit_count",
            "enabled",
            "created_at",
            "created_by",
        }
        assert all(row[3] == 1 for row in columns.values())
        db.execute("INSERT INTO image_allowlist(phash) VALUES(?)", ("0123456789abcdef",))
        assert db.execute(
            "SELECT note,source,hit_count,enabled,created_by FROM image_allowlist"
        ).fetchone() == ("", "", 0, 1, "")
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            db.execute("INSERT INTO image_allowlist(phash) VALUES(?)", ("0123456789abcdef",))
        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
            db.execute("INSERT INTO image_allowlist(phash) VALUES(NULL)")
        db.commit()
    _alembic(path, "check")
    _alembic(path, "downgrade", "c9a1f4d27e30")
    with sqlite3.connect(path) as db:
        after = dict(
            db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' AND name!='alembic_version'"
            )
        )
        assert after == baseline
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("c9a1f4d27e30",)
        assert db.execute(
            "SELECT value FROM system_meta WHERE key='review-control'"
        ).fetchone() == ("preserve-me",)
    _alembic(path, "upgrade", "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM image_allowlist").fetchone() == (0,)


@pytest.mark.parametrize(
    "distance,limit,matched",
    [(2, 2, True), (3, 2, False), (8, 8, True), (9, 8, False), (0, 0, True), (1, 0, False)],
)
def test_inclusive_threshold_controls(distance, limit, matched):
    value = (1 << distance) - 1
    assert (image_hash.best_match(0, [(7, value)], max_distance=limit) is not None) is matched
    assert image_hash.DEFAULT_MAX_DISTANCE == 8


@pytest.mark.parametrize("value", ["", "invalid", "-1", "10000000000000000", None])
def test_bad_hashes_do_not_parse(value):
    assert image_hash.from_hex(value) is None


async def test_missing_table_is_fail_closed(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'missing.sqlite'}")
    try:
        async with async_sessionmaker(engine)() as session:
            assert await image_hash.load_enabled(session) == []
            assert await image_hash.match_bytes(session, _picture()) is None
    finally:
        await engine.dispose()


async def test_malformed_rows_disabled_and_fresh_read(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'isolated.sqlite'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(ImageAllowlist.__table__.create)
        sessionmaker = async_sessionmaker(engine)
        data = _picture()
        phash = image_hash.dhash64(data)
        assert phash is not None
        async with sessionmaker() as session:
            session.add(ImageAllowlist(phash=image_hash.to_hex(phash)))
            session.add(ImageAllowlist(phash="not-a-hash"))
            session.add(ImageAllowlist(phash="10000000000000000"))
            await session.commit()
            assert await image_hash.load_enabled(session) == [(1, phash)]
            assert await image_hash.match_bytes(session, data, max_distance=2) == (1, 0)
            assert (
                await session.execute(text("SELECT hit_count FROM image_allowlist WHERE id=1"))
            ).scalar_one() == 0
            await session.execute(text("UPDATE image_allowlist SET enabled=0 WHERE id=1"))
            await session.commit()
            assert await image_hash.match_bytes(session, data, max_distance=2) is None
            await session.execute(text("UPDATE image_allowlist SET enabled=1 WHERE id=1"))
            await session.commit()
            assert await image_hash.match_bytes(session, data, max_distance=2) == (1, 0)
            for _ in range(5):
                await image_hash.record_hit(session, 1)
            await image_hash.record_hit(session, 999)
            await session.commit()
            assert (
                await session.execute(text("SELECT hit_count FROM image_allowlist WHERE id=1"))
            ).scalar_one() == 5
    finally:
        await engine.dispose()


def test_too_small_contract(tmp_path):
    """Declared 'too small => None' guard currently runs after resize, so is ineffective."""
    output = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(output, format="PNG")
    assert image_hash.dhash64(output.getvalue()) is None


def test_animation_scope_is_first_frame_only():
    """Observed limitation, not a passing assertion of full-animation equivalence."""
    with Image.open(io.BytesIO(_picture())) as opened:
        first = opened.convert("RGB")
    variants = []
    for color in ("white", "black"):
        output = io.BytesIO()
        first.save(
            output,
            format="GIF",
            save_all=True,
            append_images=[Image.new("RGB", first.size, color)],
            duration=[100, 900],
            loop=0,
        )
        data = output.getvalue()
        with Image.open(io.BytesIO(data)) as image:
            assert image.n_frames == 2
            image.seek(1)
            assert image.convert("L").getpixel((0, 0)) == (255 if color == "white" else 0)
        variants.append(data)
    assert variants[0] != variants[1]
    left, right = (image_hash.dhash64(item) for item in variants)
    assert left is not None and right is not None
    assert left == right
    assert image_hash.best_match(right, [(1, left)], max_distance=0) == (1, 0)


async def test_hit_count_failure_is_swallowed_for_sqlite_read_only(tmp_path):
    path = tmp_path / "readonly.sqlite"
    _alembic(path, "upgrade", "head")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO image_allowlist(phash) VALUES('0123456789abcdef')")
        db.commit()
    engine = create_async_engine(f"sqlite+aiosqlite:///file:{path}?mode=ro&uri=true")
    try:
        async with async_sessionmaker(engine)() as session:
            await image_hash.record_hit(session, 1)
            assert (
                await session.execute(text("SELECT hit_count FROM image_allowlist"))
            ).scalar_one() == 0
    finally:
        await engine.dispose()
