"""R-115 independent admin acceptance probes (synthetic data only).

Copy this file outside the repository, then run FROM THE REPOSITORY ROOT using
the repository's tests/conftest.py and pyproject.toml. Do not run as a script or
omit the conftest plugin: it sets safe switches, creates a temporary migrated
SQLite database, and removes only its own test directory after the run.

POSIX:
    PYTHONPATH="tests:." python -m pytest -c pyproject.toml -p conftest \
        <path-to-this-file>/qqbot-r115-admin-probe.py -q -o addopts=""

PowerShell:
    $env:PYTHONPATH = "tests;."
    python -m pytest -c pyproject.toml -p conftest <path-to-this-file>/qqbot-r115-admin-probe.py -q -o addopts=""

Run in a fresh shell, or restore its previous PYTHONPATH afterward.

Original baseline at 58344a6: 3 failed / 8 passed. The three assertions
describe desired fixed behavior, not expected outcomes to weaken or invert.
This reacceptance copy only adapts the explicit target_enabled form field and
the reader failure hook from scalars to execute, keeping all core assertions.
They cover explicit disable retry, stale deleted-row identity, and concurrent
normalized deduplication. All external effects remain disabled by conftest.
Migration tests use the checked-out application's PROJECT_ROOT/alembic.ini and
their own pytest tmp_path database; no deployment database or media is accessed.
"""

# 入库说明（R-115 正式回归，来源：主审交付包）：原探针要求在仓库外运行并由外部
# conftest 引导；迁入 tests/ 后由 tests/conftest.py 自动提供隔离环境（临时迁移库 /
# SHADOW / 关闭真实模型与通知），因此不再做外部引导检查。
import asyncio
import os
import sys
import uuid

import pytest
from app.db import SessionLocal
from app.models import AdminAudit, AllowlistTerm
from app.moderation.allowlist import (
    add_term,
    delete_term,
    load_allowlist_terms,
    set_term_enabled,
)
from app.web import auth
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


@pytest.fixture
def client():
    from app.main import app

    client = TestClient(app)
    assert (
        client.post(
            "/admin/login",
            data={"username": "admin", "password": "test-admin-pass"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    yield client
    auth.logout(client.cookies[auth.SESSION_COOKIE])
    client.close()


def csrf(client):
    return auth.csrf_token(client.cookies[auth.SESSION_COOKIE])


async def create(term):
    async with SessionLocal() as session:
        row, _ = await add_term(session, term, operator="synthetic-probe")
        return row.id


async def enabled(term_id):
    async with SessionLocal() as session:
        row = await session.get(AllowlistTerm, term_id)
        return row.enabled if row else None


def test_same_disable_post_is_idempotent(client):
    term_id = asyncio.run(create("SyntheticRepeat" + uuid.uuid4().hex))
    url = f"/admin/allowlist/{term_id}/toggle"
    response = client.post(
        url,
        data={"csrf": csrf(client), "target_enabled": "false"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert asyncio.run(enabled(term_id)) is False
    # An HTTP retry or a second submit of the same old "disable" form must not enable.
    response = client.post(
        url,
        data={"csrf": csrf(client), "target_enabled": "false"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert asyncio.run(enabled(term_id)) is False


@pytest.mark.parametrize("target", [None, "", "toggle", "0", "null"])
def test_missing_or_invalid_explicit_target_does_not_change_state_or_audit(client, target):
    term = "SyntheticInvalidTarget" + uuid.uuid4().hex
    term_id = asyncio.run(create(term))
    data = {"csrf": csrf(client)}
    if target is not None:
        data["target_enabled"] = target
    response = client.post(f"/admin/allowlist/{term_id}/toggle", data=data, follow_redirects=False)
    assert response.status_code == 303
    assert asyncio.run(enabled(term_id)) is True

    async def read_audit():
        async with SessionLocal() as session:
            row = await session.get(AllowlistTerm, term_id)
            return list(
                await session.scalars(
                    select(AdminAudit.action).where(AdminAudit.target_id == row.normalized)
                )
            )

    assert asyncio.run(read_audit()) == ["allowlist_add"]


def test_form_carries_explicit_target_and_repeat_audit_distinguishes_change(client):
    import json
    import re

    term = "SyntheticFormTarget" + uuid.uuid4().hex
    term_id = asyncio.run(create(term))
    url = f"/admin/allowlist/{term_id}/toggle"
    page = client.get("/admin/allowlist")
    form = re.search(rf'<form[^>]*action="{url}"[^>]*>(.*?)</form>', page.text, re.S)
    assert form and 'name=target_enabled value="false"' in form.group(1)
    for _ in range(2):
        assert (
            client.post(
                url,
                data={"csrf": csrf(client), "target_enabled": "false"},
                follow_redirects=False,
            ).status_code
            == 303
        )
    assert asyncio.run(enabled(term_id)) is False
    page = client.get("/admin/allowlist")
    form = re.search(rf'<form[^>]*action="{url}"[^>]*>(.*?)</form>', page.text, re.S)
    assert form and 'name=target_enabled value="true"' in form.group(1)

    async def read_audit():
        async with SessionLocal() as session:
            row = await session.get(AllowlistTerm, term_id)
            return [
                json.loads(raw)
                for raw in await session.scalars(
                    select(AdminAudit.detail_json)
                    .where(
                        AdminAudit.target_id == row.normalized,
                        AdminAudit.action == "allowlist_disable",
                    )
                    .order_by(AdminAudit.id)
                )
            ]

    audits = asyncio.run(read_audit())
    assert [(a["before"], a["changed"], a["enabled"]) for a in audits] == [
        (True, True, False),
        (False, False, False),
    ]


def test_stale_deleted_term_form_cannot_remove_replacement(client):
    old_id = asyncio.run(create("SyntheticOld" + uuid.uuid4().hex))
    data = {"csrf": csrf(client)}
    assert (
        client.post(
            f"/admin/allowlist/{old_id}/delete", data=data, follow_redirects=False
        ).status_code
        == 303
    )
    new_id = asyncio.run(create("SyntheticNew" + uuid.uuid4().hex))
    assert (
        client.post(
            f"/admin/allowlist/{old_id}/delete", data=data, follow_redirects=False
        ).status_code
        == 303
    )
    assert asyncio.run(enabled(new_id)) is True, (old_id, new_id)


@pytest.mark.asyncio
async def test_equivalent_additions_are_atomic(monkeypatch):
    term = "SyntheticRace" + uuid.uuid4().hex
    original_scalar = AsyncSession.scalar
    arrived = 0
    both = asyncio.Event()

    async def pause_after_lookup(self, statement, *args, **kwargs):
        nonlocal arrived
        result = await original_scalar(self, statement, *args, **kwargs)
        if str(statement).startswith("SELECT allowlist_terms.") and result is None:
            arrived += 1
            if arrived == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 5)
        return result

    monkeypatch.setattr(AsyncSession, "scalar", pause_after_lookup)

    async def add(raw):
        async with SessionLocal() as session:
            return await add_term(session, raw, operator="synthetic-probe")

    one, two = await asyncio.gather(add(term.lower()), add(term.upper()))
    async with SessionLocal() as session:
        await set_term_enabled(session, one[0].id, False, operator="synthetic-probe")
        terms = await load_allowlist_terms(session)
        assert one[0].normalized not in terms, (
            "equivalent_ids",
            one[0].id,
            two[0].id,
            "disabled_first_but_still_matches",
        )


@pytest.mark.parametrize("operation", ["add", "toggle", "delete"])
def test_auth_csrf_guard_controls(client, operation):
    term_id = asyncio.run(create("SyntheticAuth" + uuid.uuid4().hex))
    url = (
        "/admin/allowlist/add" if operation == "add" else f"/admin/allowlist/{term_id}/{operation}"
    )
    assert (
        client.post(url, data={"term": "SyntheticShouldNotAdd"}, follow_redirects=False).status_code
        == 403
    )
    auth.logout(client.cookies[auth.SESSION_COOKIE])
    assert (
        client.post(url, data={"term": "SyntheticShouldNotAdd"}, follow_redirects=False).status_code
        == 401
    )
    assert asyncio.run(enabled(term_id)) is True


@pytest.mark.asyncio
async def test_fresh_reader_sees_writes_not_callers_wal_snapshot():
    term = "SyntheticFresh" + uuid.uuid4().hex
    async with SessionLocal() as stale:
        await stale.execute(text("BEGIN"))
        await stale.execute(select(AllowlistTerm))
        async with SessionLocal() as writer:
            row, _ = await add_term(writer, term, operator="synthetic-probe")
            normalized = row.normalized
            assert normalized in await load_allowlist_terms(stale)
            await set_term_enabled(writer, row.id, False, operator="synthetic-probe")
            assert normalized not in await load_allowlist_terms(stale)
            await set_term_enabled(writer, row.id, True, operator="synthetic-probe")
            assert normalized in await load_allowlist_terms(stale)
            await delete_term(writer, row.id, operator="synthetic-probe")
            assert normalized not in await load_allowlist_terms(stale)


@pytest.mark.asyncio
async def test_missing_table_fail_closed(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
    try:
        async with AsyncSession(engine) as session:
            assert await load_allowlist_terms(session) == frozenset()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_read_error_fail_closed(monkeypatch):
    async def error(*args, **kwargs):
        raise RuntimeError("synthetic-read-failure")

    monkeypatch.setattr(AsyncSession, "execute", error)
    async with SessionLocal() as session:
        assert await load_allowlist_terms(session) == frozenset()


@pytest.mark.asyncio
async def test_audit_records_all_writes():
    term = "SyntheticAudit" + uuid.uuid4().hex
    async with SessionLocal() as session:
        row, _ = await add_term(session, term, operator="synthetic-probe")
        normalized = row.normalized
        await set_term_enabled(session, row.id, False, operator="synthetic-probe")
        await set_term_enabled(session, row.id, True, operator="synthetic-probe")
        await delete_term(session, row.id, operator="synthetic-probe")
        actions = list(
            await session.scalars(
                select(AdminAudit.action)
                .where(
                    AdminAudit.target_type == "allowlist_term",
                    AdminAudit.target_id == normalized,
                )
                .order_by(AdminAudit.id)
            )
        )
        assert actions == [
            "allowlist_add",
            "allowlist_disable",
            "allowlist_enable",
            "allowlist_delete",
        ]


def test_allowlist_migration_round_trip_preserves_existing_business_rows(tmp_path):
    import sqlite3
    import subprocess

    from app.config import PROJECT_ROOT

    database = tmp_path / "migration.db"
    environment = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{database}"}

    def migrate(*args):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(PROJECT_ROOT / "alembic.ini"),
                *args,
            ],
            env=environment,
            cwd=tmp_path,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr + result.stdout

    migrate("upgrade", "af66bc20f1ed")
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO system_settings (key,value,updated_at) VALUES ('synthetic-review-sentinel','retained','2026-09-16 00:00:00')"
        )
    migrate("upgrade", "head")
    migrate("check")
    with sqlite3.connect(database) as db:
        assert db.execute(
            "SELECT value FROM system_settings WHERE key='synthetic-review-sentinel'"
        ).fetchone() == ("retained",)
        assert db.execute("SELECT COUNT(*) FROM allowlist_terms").fetchone() == (0,)
    migrate("downgrade", "af66bc20f1ed")
    with sqlite3.connect(database) as db:
        assert db.execute(
            "SELECT value FROM system_settings WHERE key='synthetic-review-sentinel'"
        ).fetchone() == ("retained",)
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='allowlist_terms'"
            ).fetchone()
            is None
        )
    migrate("upgrade", "head")
    with sqlite3.connect(database) as db:
        assert db.execute(
            "SELECT value FROM system_settings WHERE key='synthetic-review-sentinel'"
        ).fetchone() == ("retained",)
