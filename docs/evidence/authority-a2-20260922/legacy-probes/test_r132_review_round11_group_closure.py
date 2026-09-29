# ruff: noqa: E402, I001, F401, F811, SIM105, S101
# Reviewer round-11 closure pack, promoted from probes/groups/test_group_c01_c02_closure.py.
# Registered adaptation (the only one): the fixed-SHA historical AST audit test
# (	est_existing_boundary_business_test_asts_are_unchanged) is NOT promoted as a
# permanent test -- per the reviewer it audits this SHA only, and it needs
# git show 194eb0b:..., which a depth-1 CI checkout cannot provide.
"""6505a79 closure controls; only synthetic SQLite and UTF-8 snapshots.

This file does not alter the original 12 probes. In particular, the stale
survey flag is supplied through the current real snapshot seam, not the
removed load_survey execution hook.
"""

from __future__ import annotations

import ast
import json
import pathlib
import sqlite3
import subprocess

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.routing import GroupProviderRoute
from app.models import GroupActionOwner, ProviderGroupSettings
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
    monkeypatch.setattr(enable, "ROOT", tmp_path)
    monkeypatch.setattr(enable, "STATS", stats)
    monkeypatch.setenv("ONEBOT_SELF_ID", "10000001")
    monkeypatch.setenv("ONEBOT_ACTION_STAGE", "recall_only")
    yield path, engine, stats, snapshot
    engine.dispose()


def seed(engine, *, settings=True):
    with Session(engine) as session:
        if settings:
            session.add(
                ProviderGroupSettings(
                    provider="onebot", external_group_id="1001", action_enabled=False
                )
            )
        session.add(GroupActionOwner(external_group_id="1001", provider="onebot"))
        session.add(
            GroupProviderRoute(
                message_provider="onebot", external_group_id="1001", action_provider="onebot"
            )
        )
        session.commit()


def run(path):
    return enable.main(["--db", str(path), "--execute"])


def first_row(path, sql):
    with sqlite3.connect(path) as con:
        return con.execute(sql).fetchone()


def test_new_row_lock_holds_from_route_check_through_insert(state, monkeypatch):
    path, engine, _, _ = state
    seed(engine, settings=False)
    original_connect = sqlite3.connect
    observed = {"arrived": 0, "locked": False, "committed": False}

    class BarrierConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            if (
                sql.strip().lower().startswith("insert into provider_group_settings")
                and not observed["arrived"]
            ):
                observed["arrived"] += 1
                observed["transaction"] = self.in_transaction
                external = original_connect(path, timeout=0.02)
                try:
                    external.execute(
                        "DELETE FROM group_provider_routes WHERE message_provider='onebot' AND external_group_id='1001'"
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
    assert run(path) == 0
    assert observed == {"arrived": 1, "locked": True, "committed": False, "transaction": True}
    assert first_row(path, "SELECT action_enabled FROM provider_group_settings") == (1,)
    assert first_row(path, "SELECT COUNT(*) FROM group_provider_routes") == (1,)


def test_route_revoked_before_lock_is_reported_blocked_in_both_audits(state, monkeypatch, capsys):
    path, engine, stats, _ = state
    seed(engine, settings=False)
    original_backup = enable.backup_sqlite

    def revoke_then_backup(url):
        with sqlite3.connect(path) as con:
            con.execute("DELETE FROM group_provider_routes")
        return original_backup(url)

    monkeypatch.setattr(enable, "backup_sqlite", revoke_then_backup)
    assert run(path) == 0
    assert first_row(path, "SELECT COUNT(*) FROM provider_group_settings") == (0,)
    output = capsys.readouterr().out
    assert "BLOCKED_NEW_ROWS" in output and "阻塞 1 个" in output
    for suffix in (".plan.json", ".applied.json"):
        audit = json.loads(next(stats.glob("*" + suffix)).read_text(encoding="utf-8"))
        assert audit["changed"] == []
        assert len(audit["blocked"]) == 1
        assert audit["blocked"][0]["blocked_reason"] == "route_revoked_after_preview"


def test_updated_timestamp_without_version_change_invalidates_old_enable_plan(state, monkeypatch):
    path, engine, _, _ = state
    seed(engine)
    original_backup = enable.backup_sqlite

    def drift_then_backup(url):
        with sqlite3.connect(path) as con:
            con.execute(
                "UPDATE provider_group_settings SET updated_at='2020-01-01 00:00:00.000001'"
            )
        return original_backup(url)

    monkeypatch.setattr(enable, "backup_sqlite", drift_then_backup)
    with pytest.raises(RuntimeError, match="计划已失效"):
        run(path)
    assert first_row(path, "SELECT action_enabled,version FROM provider_group_settings") == (0, 1)


def test_stale_true_flag_reaches_actual_snapshot_parser_and_db_still_controls(state, monkeypatch):
    path, engine, _, snapshot = state
    seed(engine)
    document = json.loads(snapshot.read_text(encoding="utf-8"))
    document["groups"][0]["action_enabled"] = True
    snapshot.write_text(json.dumps(document), encoding="utf-8")
    original_loader = enable.load_snapshot
    observed = []

    def capture(*args, **kwargs):
        meta, rows = original_loader(*args, **kwargs)
        observed.extend(rows)
        return meta, rows

    monkeypatch.setattr(enable, "load_snapshot", capture)
    assert run(path) == 0
    assert len(observed) == 1 and observed[0][3] is True
    assert first_row(path, "SELECT action_enabled,version FROM provider_group_settings") == (1, 2)


def test_injected_display_loader_is_not_called_when_snapshot_missing(state, monkeypatch):
    path, engine, _, snapshot = state
    seed(engine)
    snapshot.unlink()
    calls = []

    def legacy_loader():
        calls.append("unexpected")
        return [("1001", 300, "Synthetic", False)]

    monkeypatch.setattr(enable, "load_survey", legacy_loader)
    assert run(path) == 2
    assert calls == []
    assert first_row(path, "SELECT action_enabled FROM provider_group_settings") == (0,)
