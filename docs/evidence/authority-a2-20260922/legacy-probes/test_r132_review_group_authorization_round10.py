# ruff: noqa: E402, I001, F401, F811, SIM105, S101
# Reviewer round-9 probe pack (dbd80a5), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
"""New independent group authorization probes; synthetic state only.

All DBs, snapshots, artifacts and backups are pytest temporary files. Survey
network seams are replaced; no real QQ/NapCat or production settings are used.
"""

from __future__ import annotations

import json
import asyncio
import pathlib
import sqlite3
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models import ProviderGroupSettings, GroupActionOwner
from app.core.routing import GroupProviderRoute, resolve_action_provider
from scripts import enable_group_actions as enable
from scripts import authorize_group_routes as authorize
from scripts import group_size_survey as survey


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.db"
    engine = create_engine(f"sqlite:///{path}")
    for model in (ProviderGroupSettings, GroupActionOwner, GroupProviderRoute):
        model.__table__.create(engine)
    stats = tmp_path / "stats"
    stats.mkdir()
    for module in (enable, authorize, survey):
        monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(enable, "STATS", stats)
    monkeypatch.setattr(authorize, "STATS", stats)
    monkeypatch.setattr(survey, "OUT_DIR", stats)
    monkeypatch.setattr(authorize, "load_survey", enable.load_survey)
    monkeypatch.setattr(authorize, "load_survey_meta", enable.load_survey_meta)
    monkeypatch.setattr(authorize, "configured_self_id", enable.configured_self_id)
    monkeypatch.setattr(authorize, "declared_stage", enable.declared_stage)
    monkeypatch.setenv("ONEBOT_SELF_ID", "10000001")
    monkeypatch.setenv("ONEBOT_ACTION_STAGE", "recall_only")
    snapshot = stats / "groups-synthetic.json"
    write_snapshot(snapshot)
    yield path, engine, stats, snapshot
    engine.dispose()


def write_snapshot(path, *, gid="1001", self_id="10000001", provider="onebot"):
    path.write_text(
        json.dumps(
            {
                "self_id": self_id,
                "provider": provider,
                "collected_at": datetime.now(UTC).isoformat(),
                "groups": [
                    {
                        "external_group_id": gid,
                        "member_count": 300,
                        "name": "Synthetic",
                        "action_enabled": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def add_group(engine, *, gid="1001", enabled=False, provider="onebot", routed=False):
    with Session(engine) as session:
        session.add(
            ProviderGroupSettings(provider=provider, external_group_id=gid, action_enabled=enabled)
        )
        if routed:
            session.add(GroupActionOwner(external_group_id=gid, provider=provider))
            session.add(
                GroupProviderRoute(
                    message_provider=provider, external_group_id=gid, action_provider=provider
                )
            )
        session.commit()


def read_one(path, sql, args=()):
    with sqlite3.connect(path) as con:
        return con.execute(sql, args).fetchone()


def enable_run(path, *args):
    return enable.main(["--db", str(path), "--execute", *args])


def authorize_run(path, *args):
    return authorize.main(
        [
            "--db",
            str(path),
            "--execute",
            "--authorized-by",
            "synthetic owner B authorization",
            *args,
        ]
    )


def runtime_route(path, provider):
    async def check():
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        try:
            async with AsyncSession(engine) as session:
                return await resolve_action_provider(session, provider, "1001")
        finally:
            await engine.dispose()

    return asyncio.run(check())


def test_enable_rejects_aba_revocation_after_preview(fixture, monkeypatch):
    path, engine, _, _ = fixture
    add_group(engine, routed=True)
    original = enable.backup_sqlite

    def revoke_then_backup(url):
        with sqlite3.connect(path) as con:
            con.execute(
                "UPDATE provider_group_settings SET action_enabled=1, version=version+1 WHERE provider='onebot' AND external_group_id='1001'"
            )
            con.execute(
                "UPDATE provider_group_settings SET action_enabled=0, version=version+1 WHERE provider='onebot' AND external_group_id='1001'"
            )
        return original(url)

    monkeypatch.setattr(enable, "backup_sqlite", revoke_then_backup)
    try:
        enable_run(path)
    except RuntimeError:
        pass
    assert read_one(
        path,
        "SELECT action_enabled FROM provider_group_settings WHERE provider='onebot' AND external_group_id='1001'",
    ) == (0,), (
        "A rowcount check alone reopens a row whose owner revoked authorization after preview"
    )


@pytest.mark.parametrize("self_id,provider", [("", "onebot"), ("10000001", "qq_official")])
def test_enable_requires_complete_matching_snapshot_identity(fixture, self_id, provider):
    path, engine, _, snapshot = fixture
    add_group(engine, routed=True)
    write_snapshot(snapshot, self_id=self_id, provider=provider)
    enable_run(path)
    assert read_one(path, "SELECT action_enabled FROM provider_group_settings") == (0,), (
        "Missing account identity or another provider's snapshot must not arm the target OneBot row"
    )


def test_snapshot_metadata_and_rows_are_one_pinned_read(fixture, monkeypatch):
    path, engine, _, snapshot = fixture
    add_group(engine, gid="1001", routed=True)
    add_group(engine, gid="9009", routed=True)
    original = enable.load_survey_meta

    def change_after_metadata():
        old_meta = original()
        write_snapshot(snapshot, gid="9009", self_id="90000009")
        return old_meta

    monkeypatch.setattr(enable, "load_survey_meta", change_after_metadata)
    enable_run(path, "--survey", str(snapshot))
    assert read_one(
        path, "SELECT action_enabled FROM provider_group_settings WHERE external_group_id='9009'"
    ) == (0,), (
        "The audit binds old self_id/hash while a second read applies a changed snapshot's target"
    )


def test_top_level_survey_cannot_fallback_after_account_binding_refusal(fixture, monkeypatch):
    def refused():
        raise SystemExit("configured account has no matching OneBot config")

    monkeypatch.setattr(survey, "_groups_from_onebot_http", refused)
    monkeypatch.setattr(survey, "_webui", lambda: ("synthetic-token", "127.0.0.1", 1))
    monkeypatch.setattr(survey, "_login_token", lambda: "synthetic-jwt")
    monkeypatch.setattr(
        survey, "_call", lambda *args: {"data": [{"group_id": "9009", "member_count": 300}]}
    )
    with pytest.raises(SystemExit):
        survey._groups()


def test_authorize_rejects_wrong_account_snapshot(fixture):
    path, engine, _, snapshot = fixture
    add_group(engine, enabled=True)
    write_snapshot(snapshot, self_id="90000009")
    authorize_run(path)
    assert read_one(path, "SELECT COUNT(*) FROM group_provider_routes") == (0,), (
        "Route activation omits even the account-binding comparison used by enable_group_actions"
    )


def test_route_authorization_rechecks_disabled_group_before_commit(fixture, monkeypatch):
    path, engine, _, _ = fixture
    add_group(engine, enabled=True)
    original = authorize.backup_sqlite

    def disable_then_backup(url):
        with sqlite3.connect(path) as con:
            con.execute("UPDATE provider_group_settings SET action_enabled=0, version=version+1")
        return original(url)

    monkeypatch.setattr(authorize, "backup_sqlite", disable_then_backup)
    try:
        authorize_run(path)
    except RuntimeError:
        pass
    assert read_one(path, "SELECT action_enabled FROM provider_group_settings") == (0,)
    assert read_one(path, "SELECT COUNT(*) FROM group_provider_routes") == (0,), (
        "A group withdrawn from the enabled batch after preview must not acquire a new executable route"
    )


def test_route_authorization_rechecks_concurrent_other_provider_route(fixture, monkeypatch):
    path, engine, _, _ = fixture
    add_group(engine, enabled=True)
    original = authorize.backup_sqlite

    def foreign_route_then_backup(url):
        with sqlite3.connect(path) as con:
            con.execute(
                "INSERT INTO group_provider_routes(message_provider,external_group_id,action_provider,updated_at) VALUES ('qq_official','1001','qq_official','2026-09-19 00:00:00')"
            )
        return original(url)

    monkeypatch.setattr(authorize, "backup_sqlite", foreign_route_then_backup)
    try:
        authorize_run(path)
    except RuntimeError:
        pass
    assert read_one(
        path, "SELECT COUNT(*) FROM group_provider_routes WHERE message_provider='qq_official'"
    ) == (1,)
    assert read_one(path, "SELECT COUNT(*) FROM group_action_owners WHERE provider='onebot'") == (
        0,
    ), (
        "Inserting OneBot owner before conflict recheck masks the concurrent official route in route_ready"
    )


@pytest.mark.parametrize("tool", ["enable", "authorize"])
def test_plan_write_failure_rolls_back_changes(fixture, monkeypatch, tool):
    path, engine, stats, _ = fixture
    add_group(engine, enabled=(tool == "authorize"), routed=(tool == "enable"))
    original = pathlib.Path.write_text

    def fail_plan(self, *args, **kwargs):
        if self.parent == stats and self.name.endswith(".plan.json"):
            raise PermissionError("synthetic plan export failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", fail_plan)
    with pytest.raises(PermissionError):
        (enable_run if tool == "enable" else authorize_run)(path)
    if tool == "enable":
        assert read_one(path, "SELECT action_enabled FROM provider_group_settings") == (0,)
    else:
        assert read_one(path, "SELECT COUNT(*) FROM group_provider_routes") == (0,)
        assert read_one(path, "SELECT COUNT(*) FROM group_action_owners") == (0,)


@pytest.mark.parametrize("tool", ["enable", "authorize"])
def test_applied_export_failure_reports_committed_and_retains_plan(
    fixture, monkeypatch, capsys, tool
):
    path, engine, stats, _ = fixture
    add_group(engine, enabled=(tool == "authorize"), routed=(tool == "enable"))
    original = pathlib.Path.write_text

    def fail_applied(self, *args, **kwargs):
        if self.parent == stats and self.name.endswith(".applied.json"):
            raise PermissionError("synthetic outcome export failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", fail_applied)
    result = (enable_run if tool == "enable" else authorize_run)(path)
    assert result == 3
    assert "COMMITTED_BUT_AUDIT_EXPORT_FAILED" in capsys.readouterr().out
    plans = list(stats.glob("*.plan.json"))
    assert len(plans) == 1 and json.loads(plans[0].read_text())["op_id"]
    if tool == "enable":
        assert read_one(path, "SELECT action_enabled FROM provider_group_settings") == (1,)
    else:
        assert read_one(path, "SELECT COUNT(*) FROM group_provider_routes") == (1,)


def test_authorize_preserves_closed_rows_and_existing_provider_conflicts(fixture):
    path, engine, _, _ = fixture
    add_group(engine, gid="1001", enabled=False)
    add_group(engine, gid="2002", enabled=True)
    with Session(engine) as session:
        session.add(GroupActionOwner(external_group_id="2002", provider="qq_official"))
        session.add(
            GroupProviderRoute(
                message_provider="qq_official",
                external_group_id="2002",
                action_provider="qq_official",
            )
        )
        session.commit()
    assert authorize_run(path) == 0
    assert read_one(
        path, "SELECT COUNT(*) FROM group_provider_routes WHERE message_provider='onebot'"
    ) == (0,)
    assert read_one(
        path, "SELECT provider FROM group_action_owners WHERE external_group_id='2002'"
    ) == ("qq_official",)
    assert read_one(
        path, "SELECT action_enabled FROM provider_group_settings WHERE external_group_id='1001'"
    ) == (0,)


def test_authorize_does_not_take_over_existing_implicit_official_route(fixture):
    path, engine, _, _ = fixture
    add_group(engine, enabled=True)
    add_group(engine, provider="qq_official", enabled=True)
    # This is a supported runtime state, not corrupt data: official channels
    # have an implicit default route when no owner/route was persisted.
    assert runtime_route(path, "qq_official") == "qq_official"
    authorize_run(path)
    assert runtime_route(path, "qq_official") == "qq_official", (
        "Adding the shared OneBot owner silently revokes the active official provider's implicit route"
    )
    assert read_one(path, "SELECT COUNT(*) FROM group_action_owners WHERE provider='onebot'") == (
        0,
    )
