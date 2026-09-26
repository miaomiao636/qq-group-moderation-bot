"""Explicit new-group authorization must be atomic, scoped and auditable."""

import json
import sqlite3

import pytest
from scripts.initialize_group_actions import initialize, select_targets, verify_committed


@pytest.fixture
def con():
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE provider_group_settings(provider TEXT, external_group_id TEXT, name TEXT,
            moderation_enabled INTEGER, action_enabled INTEGER, version INTEGER, updated_at TEXT,
            PRIMARY KEY(provider, external_group_id));
        CREATE TABLE group_action_owners(external_group_id TEXT PRIMARY KEY, provider TEXT);
        CREATE TABLE group_provider_routes(external_group_id TEXT, message_provider TEXT,
            action_provider TEXT, updated_at TEXT, PRIMARY KEY(external_group_id,message_provider));
        CREATE TABLE admin_audits(id INTEGER PRIMARY KEY, operator TEXT, action TEXT,
            target_type TEXT, target_id TEXT, detail_json TEXT, created_at TEXT);
    """)
    yield db
    db.close()


def group(gid="900000001", count=200):
    return {"group_id": gid, "name": "synthetic", "member_count": count}


def identity():
    return "10000001", "recall_only"


def test_inclusive_threshold_and_explicit_scope():
    assert select_targets([group(), group("900000002", 999)], ["900000001"]) == [group()]
    with pytest.raises(ValueError):
        select_targets([group(count=199)], ["900000001"])
    with pytest.raises(ValueError):
        select_targets([group()], ["900000002"])


def test_all_new_rows_and_audit_are_atomic(con, tmp_path):
    path = tmp_path / "audit.json"
    initialize(
        con,
        [group()],
        self_id="10000001",
        authorization="synthetic owner approval",
        audit_path=path,
        verify_identity=identity,
    )
    assert con.execute(
        "select moderation_enabled,action_enabled,version from provider_group_settings"
    ).fetchone() == (1, 1, 1)
    assert con.execute("select provider from group_action_owners").fetchall() == [("onebot",)]
    assert con.execute(
        "select message_provider,action_provider from group_provider_routes"
    ).fetchall() == [("onebot", "onebot")]
    audit = json.loads(path.read_text(encoding="utf-8"))
    assert audit["stage_runtime_proven"] is False
    assert con.execute("select count(*) from admin_audits").fetchone()[0] == 1
    assert audit["before"] == "all target settings/owners/routes absent"


@pytest.mark.parametrize(
    "table,sql",
    [
        (
            "settings",
            "insert into provider_group_settings(provider,external_group_id,action_enabled) values('qq_official','900000002',0)",
        ),
        ("owner", "insert into group_action_owners values('900000002','onebot')"),
        ("route", "insert into group_provider_routes values('900000002','onebot','onebot','old')"),
    ],
)
def test_any_existing_row_blocks_whole_plan(con, tmp_path, table, sql):
    con.execute(sql)
    con.commit()
    with pytest.raises(ValueError):
        initialize(
            con,
            [group(), group("900000002")],
            self_id="10000001",
            authorization="approved",
            audit_path=tmp_path / "audit.json",
            verify_identity=identity,
        )
    assert (
        con.execute(
            "select count(*) from provider_group_settings where provider='onebot'"
        ).fetchone()[0]
        == 0
    )
    assert con.execute("select count(*) from admin_audits").fetchone()[0] == 0


@pytest.mark.parametrize("observed", [("99999999", "recall_only"), ("10000001", "full")])
def test_identity_or_stage_drift_blocks_before_writes(con, tmp_path, observed):
    with pytest.raises(ValueError):
        initialize(
            con,
            [group()],
            self_id="10000001",
            authorization="approved",
            audit_path=tmp_path / "audit.json",
            verify_identity=lambda: observed,
        )
    assert con.execute("select count(*) from provider_group_settings").fetchone()[0] == 0


def test_audit_failure_rolls_back_and_preserves_existing_file(con, tmp_path):
    path = tmp_path / "audit.json"
    path.write_text("existing evidence", encoding="utf-8")
    with pytest.raises(FileExistsError):
        initialize(
            con,
            [group()],
            self_id="10000001",
            authorization="approved",
            audit_path=path,
            verify_identity=identity,
        )
    assert con.execute("select count(*) from provider_group_settings").fetchone()[0] == 0
    assert path.read_text(encoding="utf-8") == "existing evidence"


@pytest.mark.parametrize(
    "sql",
    [
        "update provider_group_settings set action_enabled=0",
        "update provider_group_settings set moderation_enabled=0",
        "update provider_group_settings set version=2",
        "delete from group_action_owners",
        "delete from group_provider_routes",
    ],
)
def test_commit_verification_detects_subsequent_revocation(con, tmp_path, sql):
    receipt = initialize(
        con,
        [group()],
        self_id="10000001",
        authorization="approved",
        audit_path=tmp_path / "audit.json",
        verify_identity=identity,
    )
    verify_committed(con, receipt)
    con.execute(sql)
    con.commit()
    with pytest.raises(RuntimeError):
        verify_committed(con, receipt)
