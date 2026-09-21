"""A2 contract: a failed derived export cannot undo a committed decision."""

import json
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from app.moderation.image_hash import to_hex
from scripts import image_allowlist_seed as seed
from scripts import image_decision_authority as authority
from scripts.backfill_image_decisions import main as backfill_main

from tests.test_r132_review_image_tool_set_consistency import enabled_hashes
from tests.test_r132_review_image_tool_set_consistency import sandbox as sandbox
from tests.test_r132_review_review_write_contract import apply, make_batch


def test_committed_decision_survives_export_failure(sandbox, tmp_path, monkeypatch):
    db, _, _, _ = sandbox
    batch, _, value = make_batch(tmp_path)

    def fail(*args, **kwargs):
        raise PermissionError("synthetic export unavailable")

    monkeypatch.setattr(authority, "export_snapshot", fail)
    assert apply(batch, db) == 5
    assert enabled_hashes(db) == {to_hex(value)}


H1, H2 = "0000000000000001", "0000000000000002"


def change(db, key=H1, state="allowed", source="review:batch-one"):
    return authority.Decision(key, state, source, "synthetic", "old audit note")


def test_stale_version_rolls_back_history_and_entire_batch(sandbox):
    db, *_ = sandbox
    authority.apply_decisions(db, [change(db)], expected_versions={H1: 0})
    old = authority.versions(db, {H1, H2})
    authority.apply_decisions(db, [change(db, state="rejected")], expected_versions={H1: 1})
    before = authority.read_authority(db)
    with pytest.raises(authority.AuthorityError, match="stale"):
        authority.apply_decisions(db, [change(db), change(db, H2)], expected_versions=old)
    assert authority.read_authority(db) == before
    assert H2 not in before
    journal = json.loads(before[H1]["history_json"])
    assert [e["version"] for e in journal["events"]] == [1, 2]


def test_two_threads_same_expected_version_one_commit(sandbox):
    db, *_ = sandbox
    barrier = threading.Barrier(2)

    def writer(state):
        expected = authority.versions(db, {H1})
        barrier.wait(timeout=10)
        try:
            authority.apply_decisions(db, [change(db, state=state)], expected_versions=expected)
            return "committed"
        except authority.AuthorityError as exc:
            assert "DECISION_CONFLICT" in str(exc)
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(writer, state) for state in ("allowed", "rejected")]
        assert sorted(f.result(timeout=15) for f in futures) == ["committed", "conflict"]
    row = authority.read_authority(db)[H1]
    assert row["decision_version"] == 1
    assert len(json.loads(row["history_json"])["events"]) == 1
    assert authority.export_state(db) == "ok"


def test_crash_after_commit_reexport_does_not_repeat_decision(sandbox):
    db, *_ = sandbox
    code = """
import os,sys
from pathlib import Path
from scripts.image_decision_authority import Decision,commit_decisions
commit_decisions(Path(sys.argv[1]),[Decision('0000000000000001','rejected','review:crash','test')],expected_versions={'0000000000000001':0})
os._exit(19)
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", code, str(db)], capture_output=True, timeout=20
    )
    assert result.returncode == 19, result.stderr
    before = authority.read_authority(db)
    assert before[H1]["decision_state"] == "rejected"
    assert authority.export_state(db) == "missing"
    authority.export_snapshot(db)
    assert authority.export_state(db) == "ok"
    assert authority.read_authority(db) == before


def test_failed_older_export_cannot_undo_newer_decision(sandbox, monkeypatch):
    db, *_ = sandbox
    original = authority.export_snapshot
    first = True

    def interleaved(path):
        nonlocal first
        if first:
            first = False
            authority.apply_decisions(
                db, [change(db, state="rejected", source="review:later")], expected_versions={H1: 1}
            )
            raise PermissionError("old export failed after new commit")
        return original(path)

    monkeypatch.setattr(authority, "export_snapshot", interleaved)
    with pytest.raises(authority.ExportPending):
        authority.apply_decisions(db, [change(db)], expected_versions={H1: 0})
    row = authority.read_authority(db)[H1]
    assert (row["decision_state"], row["decision_version"], row["decision_source"]) == (
        "rejected",
        2,
        "review:later",
    )
    assert authority.export_state(db) == "ok"


@pytest.mark.parametrize("bad", [b"{broken", b"[]"])
def test_corrupt_derived_export_is_preserved_before_rebuild(sandbox, bad):
    db, *_ = sandbox
    path = seed.rejection_snapshot_path(db)
    path.write_bytes(bad)
    authority.apply_decisions(db, [change(db)], expected_versions={H1: 0})
    assert authority.export_state(db) == "ok"
    assert [p.read_bytes() for p in path.parent.glob(path.name + ".invalid-*")] == [bad]


def uninitialize(db):
    with sqlite3.connect(db) as con:
        con.execute("DELETE FROM system_settings WHERE key=?", (authority.MARKER,))


def test_missing_backfill_marker_blocks_empty_database(sandbox):
    db, *_ = sandbox
    uninitialize(db)
    before = db.read_bytes()
    with pytest.raises(authority.AuthorityError, match="NOT_BACKFILLED"):
        authority.read_authority(db)
    assert backfill_main(["--db", str(db)]) == 0
    assert db.read_bytes() == before
    assert backfill_main(["--db", str(db), "--apply"]) == 0
    assert authority.read_authority(db) == {}


def test_backfill_preserves_legacy_fields_and_is_idempotent(sandbox):
    db, *_ = sandbox
    uninitialize(db)
    entry = {
        "state": "rejected",
        "source": "review:legacy",
        "operator": "person",
        "at": "2026-01-01T00:00:00Z",
        "first_rejected": "2025-12-01",
        "history": [{"state": "rejected", "opaque": {"keep": True}}],
        "unknown": [1, "preserve"],
    }
    seed.rejection_snapshot_path(db).write_text(json.dumps({H1: entry}), encoding="utf-8")
    before = db.read_bytes()
    assert authority.backfill(db)["rows"] == 1
    assert db.read_bytes() == before
    authority.backfill(db, apply=True)
    row = authority.read_authority(db)[H1]
    assert (row["enabled"], row["decision_state"], row["decision_version"]) == (0, "rejected", 1)
    assert json.loads(row["history_json"])["legacy_snapshot"] == entry
    after = authority.read_authority(db)
    assert authority.backfill(db, apply=True)["changed"] == 0
    assert authority.read_authority(db) == after
    authority.apply_decisions(db, [change(db)], expected_versions={H1: 1})
    new = authority.read_authority(db)[H1]
    journal = json.loads(new["history_json"])
    assert journal["legacy_snapshot"] == entry
    assert journal["events"][0]["version"] == 2
    exported = json.loads(seed.rejection_snapshot_path(db).read_text())[H1]
    assert exported["unknown"] == entry["unknown"]
    assert exported["first_rejected"] == entry["first_rejected"]
    assert exported["history"][0] == entry["history"][0]


def test_backfill_conflict_does_not_change_enabled_or_any_rows(sandbox):
    db, *_ = sandbox
    uninitialize(db)
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO image_allowlist(phash,enabled,created_at) VALUES(?,1,'2026-01-01')", (H1,)
        )
    seed.rejection_snapshot_path(db).write_text(
        json.dumps({H1: {"state": "rejected"}, H2: {"state": "rejected"}})
    )
    before = db.read_bytes()
    with pytest.raises(authority.AuthorityError, match="BACKFILL_CONFLICT"):
        authority.backfill(db, apply=True)
    assert db.read_bytes() == before


def test_malformed_authority_never_means_no_rejections(sandbox):
    db, *_ = sandbox
    authority.apply_decisions(db, [change(db, state="rejected")], expected_versions={H1: 0})
    with sqlite3.connect(db) as con:
        con.execute("UPDATE image_allowlist SET enabled=1")
    with pytest.raises(authority.AuthorityError, match="INCONSISTENT"):
        seed.load_rejections(db)


def test_export_reads_after_lock_not_before(sandbox, monkeypatch):
    db, *_ = sandbox
    original = authority.read_authority

    def checked(path):
        key = os.path.normcase(str(path.resolve()))
        assert seed._DECISION_LOCK_LOCAL.depths.get(key, 0) >= 1
        return original(path)

    monkeypatch.setattr(authority, "read_authority", checked)
    authority.export_snapshot(db)


def test_old_schema_is_explicit_failure(tmp_path):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE image_allowlist(phash TEXT,enabled INTEGER)")
    with pytest.raises(authority.AuthorityError, match="AUTHORITY_UNAVAILABLE"):
        authority.read_authority(db)


def test_lock_is_per_database_and_age_does_not_steal_live_owner(sandbox, tmp_path):
    db, *_ = sandbox
    other = tmp_path / "other.db"
    with seed.decision_lock(db):
        with seed.decision_lock(other):
            keys = seed._DECISION_LOCK_LOCAL.depths
            assert len(keys) == 2
        lock = Path(str(db.resolve()) + ".review.lock")
        os.utime(lock, (1, 1))
        code = """
import sys
from pathlib import Path
from scripts.image_allowlist_seed import decision_lock
try:
 with decision_lock(Path(sys.argv[1]),timeout=.2,stale=.01):
  raise SystemExit(9)
except RuntimeError:
 raise SystemExit(0)
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", code, str(db)], timeout=10, capture_output=True
        )
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("state", [None, True, 7, "", "unknown"])
def test_invalid_authority_state_fails_closed(sandbox, state):
    db, *_ = sandbox
    authority.apply_decisions(db, [change(db)], expected_versions={H1: 0})
    if state is None:
        with sqlite3.connect(db) as con, pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE image_allowlist SET decision_state=?", (state,))
        return
    with sqlite3.connect(db) as con:
        con.execute("UPDATE image_allowlist SET decision_state=?", (state,))
    with pytest.raises(authority.AuthorityError, match="NOT_BACKFILLED"):
        authority.read_authority(db)


@pytest.mark.parametrize(
    "history",
    [
        "null",
        "[]",
        "{}",
        '{"legacy_snapshot":null,"events":null}',
        '{"legacy_snapshot":null,"events":[7]}',
    ],
)
def test_invalid_authority_history_fails_closed(sandbox, history):
    db, *_ = sandbox
    authority.apply_decisions(db, [change(db)], expected_versions={H1: 0})
    with sqlite3.connect(db) as con:
        con.execute("UPDATE image_allowlist SET history_json=?", (history,))
    with pytest.raises(authority.AuthorityError, match="INCONSISTENT"):
        authority.read_authority(db)


def test_backfill_unknown_marker_and_empty_rejection_conflict(sandbox):
    db, *_ = sandbox
    with sqlite3.connect(db) as con:
        con.execute("UPDATE system_settings SET value='2' WHERE key=?", (authority.MARKER,))
    before = db.read_bytes()
    with pytest.raises(authority.AuthorityError, match="unsupported"):
        authority.backfill(db, apply=True)
    assert db.read_bytes() == before
    uninitialize(db)
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO image_allowlist(phash,enabled,created_at) VALUES(?,1,'2026-01-01')", (H1,)
        )
    seed.rejection_snapshot_path(db).write_text(json.dumps({H1: {}}))
    before = db.read_bytes()
    with pytest.raises(authority.AuthorityError, match="CONFLICT"):
        authority.backfill(db, apply=True)
    assert db.read_bytes() == before


def test_review_captures_version_before_reading_batch(sandbox, tmp_path, monkeypatch):
    from scripts import apply_review_decisions as tool

    db, *_ = sandbox
    batch, _, value = make_batch(tmp_path)
    assert apply(batch, db) == 0
    key = to_hex(value)
    original = tool.load_manifest

    def interleave(path):
        authority.apply_decisions(
            db,
            [authority.Decision(key, "rejected", "review:newer", "test")],
            expected_versions={key: 1},
        )
        return original(path)

    monkeypatch.setattr(tool, "load_manifest", interleave)
    assert apply(batch, db) == 4
    row = authority.read_authority(db)[key]
    assert (row["decision_state"], row["decision_version"]) == ("rejected", 2)


def test_dry_run_checks_authority_and_contradictory_hash(sandbox, tmp_path):
    from scripts import apply_review_decisions as tool

    db, *_ = sandbox
    batch, _, value = make_batch(tmp_path)
    manifest = batch / "IMAGE_REVIEW.md"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(
        text + text.splitlines()[-1].replace("| 01 |", "| 02 |") + "\n", encoding="utf-8"
    )
    (batch / "DECISIONS.json").write_text(
        json.dumps({"decisions": {"01": "放行", "02": "撤回"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert tool.main(["--db", str(db), "--batch", str(batch), "--dry-run"]) == 4
    assert authority.read_authority(db) == {}
    uninitialize(db)
    assert tool.main(["--db", str(db), "--batch", str(batch), "--dry-run"]) == 4


def test_observation_cannot_mix_a_concurrent_commit_with_older_db(sandbox, monkeypatch):
    db, *_ = sandbox
    authority.apply_decisions(db, [change(db)], expected_versions={H1: 0})
    original = authority.read_authority
    entered = threading.Event()
    futures = []

    def writer():
        entered.set()
        authority.apply_decisions(db, [change(db, state="rejected")], expected_versions={H1: 1})

    with ThreadPoolExecutor(max_workers=1) as pool:

        def read(path):
            rows = original(path)
            if threading.current_thread() is threading.main_thread():
                futures.append(pool.submit(writer))
                assert entered.wait(5)
                import time

                time.sleep(0.1)
                assert not futures[0].done()
            return rows

        with monkeypatch.context() as controlled:
            controlled.setattr(authority, "read_authority", read)
            rows, state = authority.observe_authority(db)
        assert (rows[H1]["decision_state"], state) == ("allowed", "ok")
        futures[0].result(timeout=10)
    assert authority.read_authority(db)[H1]["decision_state"] == "rejected"
    assert authority.export_state(db) == "ok"


@pytest.mark.parametrize("kind", ["legacy", "uninitialized", "inconsistent"])
def test_offline_approval_requires_ready_consistent_authority(sandbox, kind):
    db, *_ = sandbox
    authority.apply_decisions(db, [change(db)], expected_versions={H1: 0})
    if kind == "legacy":
        with sqlite3.connect(db) as con:
            con.execute("ALTER TABLE image_allowlist RENAME TO a2_rows")
            con.execute("CREATE TABLE image_allowlist(phash TEXT,enabled INTEGER)")
            con.execute("INSERT INTO image_allowlist VALUES(?,1)", (H1,))
    elif kind == "uninitialized":
        uninitialize(db)
    else:
        with sqlite3.connect(db) as con:
            con.execute("UPDATE image_allowlist SET decision_state='rejected'")
    active, reason = seed.effective_state(db)
    assert active is None and reason != "ok"
    if kind != "legacy":
        with pytest.raises(authority.AuthorityError):
            seed.candidate_rejections(db)


@pytest.mark.parametrize("existing", [False, True])
def test_backfill_unknown_decision_date_remains_null_and_orm_readable(sandbox, existing):
    from datetime import UTC, datetime

    from app.models import ImageAllowlist
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    db, *_ = sandbox
    uninitialize(db)
    if existing:
        with sqlite3.connect(db) as con:
            con.execute(
                "INSERT INTO image_allowlist(phash,enabled,created_at) VALUES(?,0,'2025-01-01')",
                (H1,),
            )
    seed.rejection_snapshot_path(db).write_text(json.dumps({H1: {"state": "rejected"}}))
    started = datetime.now(UTC)
    authority.backfill(db, apply=True)
    engine = create_engine("sqlite:///" + str(db))
    try:
        with Session(engine) as session:
            row = session.scalars(select(ImageAllowlist)).one()
            assert row.decided_at is None
            if existing:
                assert row.created_at.year == 2025
            else:
                assert started <= row.created_at.replace(tzinfo=UTC) <= datetime.now(UTC)
            assert json.loads(row.history_json)["legacy_snapshot"] == {"state": "rejected"}
    finally:
        engine.dispose()


@pytest.mark.parametrize("invalid", ["not-a-date", 7, {"fake": "date"}])
def test_backfill_invalid_date_is_atomic_conflict(sandbox, invalid):
    db, *_ = sandbox
    uninitialize(db)
    seed.rejection_snapshot_path(db).write_text(
        json.dumps({H1: {"state": "rejected", "at": invalid}, H2: {"state": "rejected"}})
    )
    before = db.read_bytes()
    with pytest.raises(authority.AuthorityError, match="BACKFILL_CONFLICT"):
        authority.backfill(db, apply=True)
    assert db.read_bytes() == before
