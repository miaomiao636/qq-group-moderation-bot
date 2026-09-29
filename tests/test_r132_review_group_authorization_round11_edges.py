# ruff: noqa: E402, I001, F401, F811, SIM105, S101
# Reviewer pack (round-11 review of 6505a79), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
"""Independent synthetic-only follow-up probes for 194eb0b.

No sockets, production DBs, or real configuration are used. The old 14 probes
remain unchanged; these fixtures use real structured snapshots. The concurrency
seam uses two actual SQLite connections, not a fabricated rowcount or result.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import GroupActionOwner, ProviderGroupSettings
from app.core.routing import GroupProviderRoute
from scripts import authorize_group_routes as authorize
from scripts import enable_group_actions as enable


@pytest.fixture
def state(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.db"
    engine = create_engine(f"sqlite:///{path}")
    for model in (ProviderGroupSettings, GroupActionOwner, GroupProviderRoute):
        model.__table__.create(engine)
    stats = tmp_path / "stats"
    stats.mkdir()
    snapshot = stats / "groups-synthetic.json"
    snapshot.write_text(
        json.dumps(
            {
                "self_id": "10000001",
                "provider": "onebot",
                "collected_at": "2026-09-20T00:00:00Z",
                "groups": [
                    {
                        "external_group_id": "1001",
                        "member_count": 300,
                        "name": "Synthetic",
                        "action_enabled": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEBOT_SELF_ID", "10000001")
    monkeypatch.setenv("ONEBOT_ACTION_STAGE", "recall_only")
    for module in (enable, authorize):
        monkeypatch.setattr(module, "ROOT", tmp_path)
        monkeypatch.setattr(module, "STATS", stats)
    monkeypatch.setattr(authorize, "configured_self_id", enable.configured_self_id)
    monkeypatch.setattr(authorize, "declared_stage", enable.declared_stage)
    yield path, engine, stats, snapshot
    engine.dispose()


def add(engine, gid="1001", *, enabled=False, settings=True, routed=False):
    with Session(engine) as session:
        if settings:
            session.add(
                ProviderGroupSettings(
                    provider="onebot", external_group_id=gid, action_enabled=enabled
                )
            )
        if routed:
            session.add(GroupActionOwner(external_group_id=gid, provider="onebot"))
            session.add(
                GroupProviderRoute(
                    message_provider="onebot", external_group_id=gid, action_provider="onebot"
                )
            )
        session.commit()


def run(path, *, tool=authorize, extra=()):
    argv = ["--db", str(path), "--execute", *extra]
    if tool is authorize:
        argv += ["--authorized-by", "synthetic B authorization"]
    return tool.main(argv)


def row(path, sql):
    with sqlite3.connect(path) as con:
        return con.execute(sql).fetchone()


@pytest.mark.parametrize("drift", ["revoke", "foreign_route", "official_implicit"])
def test_authorize_final_recheck_is_serialized_with_first_write(state, monkeypatch, drift):
    path, engine, _, _ = state
    add(engine, enabled=True)
    original_connect = sqlite3.connect
    observed = {"arrived": 0, "committed": False, "locked": False}

    class BarrierConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            if (
                sql.strip().lower().startswith("insert into group_action_owners")
                and not observed["arrived"]
            ):
                observed["arrived"] += 1
                observed["transaction_before_first_write"] = self.in_transaction
                external = original_connect(path, timeout=0.02)
                try:
                    if drift == "revoke":
                        external.execute(
                            "UPDATE provider_group_settings SET action_enabled=0, version=version+1 WHERE provider='onebot' AND external_group_id='1001'"
                        )
                    elif drift == "foreign_route":
                        external.execute(
                            "INSERT INTO group_provider_routes(message_provider,external_group_id,action_provider,updated_at) VALUES ('qq_official','1001','qq_official','2026-09-20 00:00:00')"
                        )
                    else:
                        external.execute(
                            "INSERT INTO provider_group_settings(provider,external_group_id,name,action_enabled,updated_at) VALUES ('qq_official','1001','Synthetic official',1,'2026-09-20 00:00:00')"
                        )
                    external.commit()
                    observed["committed"] = True
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower():
                        raise
                    observed["locked"] = True
                finally:
                    external.close()
            return super().execute(sql, parameters)

    def connect(database, *args, **kwargs):
        if str(database) == str(path):
            kwargs["factory"] = BarrierConnection
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        run(path)
    except (RuntimeError, sqlite3.IntegrityError):
        pass
    assert observed["arrived"] == 1, observed
    assert observed["committed"] or observed["locked"], observed
    if observed["committed"]:
        assert row(path, "SELECT COUNT(*) FROM group_action_owners WHERE provider='onebot'") == (
            0,
        ), observed
        assert row(
            path, "SELECT COUNT(*) FROM group_provider_routes WHERE message_provider='onebot'"
        ) == (0,), observed
    else:
        # Correct write-lock serialization is also acceptable: do not insist a
        # competing writer can commit while an immediate transaction holds it.
        assert observed["locked"] and observed["transaction_before_first_write"], observed


def test_enable_delete_recreate_is_not_same_original_plan(state, monkeypatch):
    path, engine, _, _ = state
    add(engine, routed=True)
    original_backup = enable.backup_sqlite

    def replace_then_backup(url):
        with sqlite3.connect(path) as con:
            con.execute(
                "DELETE FROM provider_group_settings WHERE provider='onebot' AND external_group_id='1001'"
            )
            con.execute(
                "INSERT INTO provider_group_settings(provider,external_group_id,name,action_enabled,updated_at) VALUES ('onebot','1001','New disabled identity',0,'2026-09-20 00:00:01')"
            )
        return original_backup(url)

    monkeypatch.setattr(enable, "backup_sqlite", replace_then_backup)
    try:
        run(path, tool=enable)
    except RuntimeError:
        pass
    assert row(path, "SELECT action_enabled FROM provider_group_settings") == (0,), (
        "Deleted/recreated row resets version to 1 and old plan opens it"
    )


def test_enable_deleted_target_refuses_without_recreating(state, monkeypatch):
    path, engine, _, _ = state
    add(engine, routed=True)
    original_backup = enable.backup_sqlite

    def delete_then_backup(url):
        with sqlite3.connect(path) as con:
            con.execute("DELETE FROM provider_group_settings")
        return original_backup(url)

    monkeypatch.setattr(enable, "backup_sqlite", delete_then_backup)
    with pytest.raises(RuntimeError, match="计划已失效"):
        run(path, tool=enable)
    assert row(path, "SELECT COUNT(*) FROM provider_group_settings") == (0,)


def test_enable_concurrent_new_disabled_row_is_preserved(state, monkeypatch):
    path, engine, _, _ = state
    add(engine, settings=False, routed=True)
    original_backup = enable.backup_sqlite

    def insert_then_backup(url):
        add(engine, enabled=False)
        return original_backup(url)

    monkeypatch.setattr(enable, "backup_sqlite", insert_then_backup)
    with pytest.raises(sqlite3.IntegrityError):
        run(path, tool=enable)
    assert row(path, "SELECT action_enabled FROM provider_group_settings") == (0,)


def test_enable_new_row_rechecks_route_revoked_during_backup(state, monkeypatch):
    path, engine, _, _ = state
    add(engine, settings=False, routed=True)
    original_backup = enable.backup_sqlite

    def remove_route_then_backup(url):
        with sqlite3.connect(path) as con:
            con.execute(
                "DELETE FROM group_provider_routes WHERE message_provider='onebot' AND external_group_id='1001'"
            )
            con.execute(
                "DELETE FROM group_action_owners WHERE provider='onebot' AND external_group_id='1001'"
            )
        return original_backup(url)

    monkeypatch.setattr(enable, "backup_sqlite", remove_route_then_backup)
    try:
        run(path, tool=enable)
    except RuntimeError:
        pass
    assert row(path, "SELECT COUNT(*) FROM group_provider_routes") == (0,)
    assert row(path, "SELECT COUNT(*) FROM provider_group_settings WHERE action_enabled=1") == (
        0,
    ), (
        "New settings became enabled after their required route was withdrawn; runtime remains fail-closed, but admission contract and success report are false"
    )


def test_real_snapshot_is_read_once_and_audit_matches_original_bytes(state, monkeypatch):
    path, engine, stats, snapshot = state
    add(engine, routed=True)
    add(engine, gid="9009", routed=True)
    original_read = pathlib.Path.read_bytes
    raw = original_read(snapshot)
    replacement = json.loads(raw)
    replacement["self_id"] = "90000009"
    replacement["groups"][0]["external_group_id"] = "9009"
    calls = []

    def replace_after_read(target):
        result = original_read(target)
        if target == snapshot:
            calls.append(target)
            snapshot.write_text(json.dumps(replacement), encoding="utf-8")
        return result

    monkeypatch.setattr(pathlib.Path, "read_bytes", replace_after_read)
    assert run(path, tool=enable, extra=("--survey", str(snapshot))) == 0
    assert len(calls) == 1
    assert row(
        path, "SELECT action_enabled FROM provider_group_settings WHERE external_group_id='1001'"
    ) == (1,)
    assert row(
        path, "SELECT action_enabled FROM provider_group_settings WHERE external_group_id='9009'"
    ) == (0,)
    audit = json.loads(next(stats.glob("*.plan.json")).read_text(encoding="utf-8"))
    assert audit["survey"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert audit["survey"]["self_id"] == "10000001"


@pytest.mark.parametrize("input_kind", ["missing_snapshot", "forged_source_field"])
def test_normal_enable_entry_does_not_use_injected_loader_bypass(state, input_kind):
    path, engine, _, snapshot = state
    add(engine, routed=True)
    if input_kind == "missing_snapshot":
        snapshot.unlink()
    else:
        document = json.loads(snapshot.read_text(encoding="utf-8"))
        document.update(self_id="", source="injected_loader")
        snapshot.write_text(json.dumps(document), encoding="utf-8")
    assert run(path, tool=enable) == 2
    assert row(path, "SELECT action_enabled FROM provider_group_settings") == (0,)


def test_trusted_inprocess_injected_loader_exception_is_explicit_diagnostic(state, monkeypatch):
    """Document exception honestly; this requires replacing trusted Python code."""
    path, engine, stats, snapshot = state
    add(engine, routed=True)
    snapshot.unlink()
    monkeypatch.setattr(enable, "configured_self_id", lambda: "")
    monkeypatch.setattr(enable, "load_survey", lambda: [("1001", 300, "Synthetic", False)])
    result = run(path, tool=enable)
    if result == 0:
        assert row(path, "SELECT action_enabled FROM provider_group_settings") == (1,)
        audit = json.loads(next(stats.glob("*.plan.json")).read_text(encoding="utf-8"))
        assert audit["survey"]["source"] == "injected_loader"
        assert not audit["survey"]["sha256"] and not audit["survey"]["self_id"]
    else:
        # Preferred cleanup may delete this trusted-code-only exception. This
        # diagnostic must not force production to retain a permissive branch.
        assert row(path, "SELECT action_enabled FROM provider_group_settings") == (0,)
        assert not list(stats.glob("*.applied.json"))


def test_authorize_threshold_and_snapshot_really_limit_targets(state):
    path, engine, _, snapshot = state
    for gid in ("1001", "1002", "1003", "1004"):
        add(engine, gid=gid, enabled=True)
    document = json.loads(snapshot.read_text(encoding="utf-8"))
    document["groups"] = [
        {
            "external_group_id": gid,
            "member_count": count,
            "name": "Synthetic",
            "action_enabled": True,
        }
        for gid, count in (("1001", 199), ("1002", 200), ("1003", 201))
    ]
    snapshot.write_text(json.dumps(document), encoding="utf-8")
    assert run(path) == 0
    assert row(path, "SELECT GROUP_CONCAT(external_group_id) FROM group_provider_routes") == (
        "1003",
    )
