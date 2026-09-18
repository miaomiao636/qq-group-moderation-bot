# ruff: noqa: E402, I001, F401
# Reviewer probe pack (PR #45 / r132), promoted verbatim into the repo test suite.
# Isolation asserts intentionally run BEFORE application imports (E402 is by design).
# This header changes no assertion and no logic.

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_url = os.environ.get("DATABASE_URL", "")
assert _url.startswith("sqlite+aiosqlite:///")
_db = Path(_url.split(":///", 1)[1]).resolve()
assert _db.parent.name.startswith("qqbot-test-")
assert _db.is_relative_to(Path(tempfile.gettempdir()).resolve())
assert os.environ.get("APP_ENV") == "test"
assert os.environ.get("ACTION_MODE") == "SHADOW"
for _key in (
    "AI_ENABLED",
    "ONEBOT_ACTIONS_ENABLED",
    "NOTIFICATIONS_ENABLED",
    "NOTIFICATION_QQ_ENABLED",
    "NOTIFICATION_EMAIL_ENABLED",
    "NOTIFICATION_HEARTBEAT_ENABLED",
):
    assert os.environ.get(_key) == "false"

import asyncio
import json
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.db import SessionLocal
from app.models import AllowlistMember, AdminAudit
from app.moderation.allowlist import add_member, load_allowlist_members
from app.web import auth
from tests.test_r115_admin_migration import migrate


async def _clear_members():
    async with SessionLocal() as session:
        await session.execute(delete(AllowlistMember))
        await session.commit()


@pytest.fixture()
def logged_in():
    from app.main import app

    asyncio.run(_clear_members())
    client = TestClient(app, raise_server_exceptions=False)
    assert (
        client.post(
            "/admin/login",
            data={"username": "admin", "password": "test-admin-pass"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    csrf = auth.csrf_token(client.cookies[auth.SESSION_COOKIE])
    try:
        yield client, csrf
    finally:
        auth.logout(client.cookies[auth.SESSION_COOKIE])
        client.close()
        asyncio.run(_clear_members())


async def _enabled(user_id):
    async with SessionLocal() as session:
        row = await session.scalar(
            select(AllowlistMember).where(
                AllowlistMember.provider == "onebot", AllowlistMember.external_user_id == user_id
            )
        )
        return None if row is None else bool(row.enabled)


def test_import_requires_a_server_bound_preview(logged_in):
    client, csrf = logged_in
    reply = client.post(
        "/admin/allowlist/members/import",
        data={"csrf": csrf, "confirmed": "1", "ack": "1", "text": "910000001\n"},
        follow_redirects=False,
    )
    actual = asyncio.run(_enabled("910000001"))
    assert actual is None, ("unpreviewed import wrote a fully trusted member", reply.status_code)


def test_stale_preview_must_not_reenable_a_revoked_member(logged_in):
    client, csrf = logged_in
    for member in ("910000011", "910000012"):
        assert (
            client.post(
                "/admin/allowlist/members/add",
                data={"csrf": csrf, "member_id": member},
                follow_redirects=False,
            ).status_code
            == 303
        )
    text = "910000011\n910000012\n"
    preview = client.post(
        "/admin/allowlist/members/import", data={"csrf": csrf, "text": text}, follow_redirects=False
    )
    assert preview.status_code == 200
    assert "重新启用 0" in preview.text

    async def row_id():
        async with SessionLocal() as session:
            return await session.scalar(
                select(AllowlistMember.id).where(AllowlistMember.external_user_id == "910000012")
            )

    target = asyncio.run(row_id())
    assert (
        client.post(
            f"/admin/allowlist/members/{target}/toggle",
            data={"csrf": csrf, "target_enabled": "false"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert asyncio.run(_enabled("910000012")) is False
    # User submits the old preview, which proposed no reactivation. It must be
    # invalidated or re-previewed, never silently recomputed into new authority.
    confirmed = client.post(
        "/admin/allowlist/members/import",
        data={"csrf": csrf, "text": text, "confirmed": "1"},
        follow_redirects=False,
    )
    assert asyncio.run(_enabled("910000012")) is False, (
        "revoked member silently regained all-category allowance",
        confirmed.status_code,
    )


async def test_concurrent_add_keeps_one_identity_and_one_creation_audit():
    provider = "p" + uuid.uuid4().hex[:10]
    user = "910000021"

    async def worker():
        async with SessionLocal() as session:
            row, created = await add_member(session, user, operator="synthetic", provider=provider)
            return row.id, created

    results = await asyncio.gather(*(worker() for _ in range(4)))
    assert len({row_id for row_id, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    async with SessionLocal() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(AdminAudit)
            .where(
                AdminAudit.action == "allowlist_member_add",
                AdminAudit.target_id == f"{provider}:{user}",
            )
        )
        assert count == 1


async def test_member_loader_fails_closed(monkeypatch):
    import app.moderation.allowlist as allowlist

    class FailingReader:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            raise RuntimeError("synthetic read failure")

        async def __aexit__(self, *args):
            return None

    async with SessionLocal() as session:
        monkeypatch.setattr(allowlist, "AsyncSession", FailingReader)
        assert await load_allowlist_members(session) == frozenset()


def test_member_migration_downgrade_preserves_existing_tables(tmp_path):
    database = tmp_path / "member-migration.db"
    migrate(database, "b8d4f2a05e31")
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO system_settings (key,value,updated_at) VALUES ('r132-probe','keep','2026-09-18 00:00:00')"
        )
    migrate(database, "c9a1f4d27e30")
    migrate(database, None, operation="check")
    with sqlite3.connect(database) as db:
        ddl = db.execute("SELECT sql FROM sqlite_master WHERE name='allowlist_members'").fetchone()[
            0
        ]
        assert "AUTOINCREMENT" in ddl
        assert "UNIQUE (provider, external_user_id)" in ddl
        db.execute(
            "INSERT INTO allowlist_members (provider,external_user_id,enabled,created_at,updated_at) VALUES ('onebot','910000099',1,'2026-09-18','2026-09-18')"
        )
    migrate(database, "b8d4f2a05e31", operation="downgrade")
    with sqlite3.connect(database) as db:
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='allowlist_members'").fetchone()
            is None
        )
        assert db.execute(
            "SELECT value FROM system_settings WHERE key='r132-probe'"
        ).fetchone() == ("keep",)
    migrate(database, "c9a1f4d27e30")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM allowlist_members").fetchone() == (0,)
