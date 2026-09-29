# ruff: noqa: E402, I001, F401, F811, SIM105, S101
# A2 REGISTERED ADAPTATION: original bytes are sealed under docs/evidence/authority-a2-20260922/legacy-probes/.
# Historical nodeids are retained; current contracts and every changed AST node are registered in docs/2026-09-22-authority-a2-adaptations.md.
# Reviewer pack (round-11 review of 6505a79), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
"""194eb0b: compensation must not undo a later independently committed decision.

All operations use temporary synthetic SQLite and generated images. Hooks interleave
real application calls after the first DB commit, immediately before its failing
snapshot write; SQL/results are not mocked.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.moderation.image_hash import to_hex
from scripts import image_decision_authority as authority
from scripts import image_allowlist_seed as seed_tool
from tests.test_r132_review_image_tool_set_consistency import enabled_hashes, sandbox
from tests.test_r132_review_review_write_contract import apply, make_batch


def test_failed_old_rejection_cannot_reenable_later_successful_rejection(
    sandbox, tmp_path, monkeypatch
):
    db, _, _, _ = sandbox
    initial, _, value = make_batch(tmp_path, name="batch-initial")
    earlier, _, _ = make_batch(tmp_path, name="batch-earlier", verdict="撤回")
    later, _, _ = make_batch(tmp_path, name="batch-later", verdict="撤回")
    assert apply(initial, db) == 0
    original = authority.export_snapshot

    def interleave(path):
        if authority.read_authority(db)[to_hex(value)]["decision_source"] == "review:batch-earlier":
            assert to_hex(value) not in enabled_hashes(db)
            assert apply(later, db) == 0
            raise PermissionError("synthetic older export failure")
        return original(path)

    monkeypatch.setattr(authority, "export_snapshot", interleave)
    assert apply(earlier, db) == 5
    raw = json.loads(seed_tool.rejection_snapshot_path(db).read_text(encoding="utf-8"))
    assert raw[to_hex(value)]["state"] == "rejected"
    assert to_hex(value) not in enabled_hashes(db)
    assert authority.read_authority(db)[to_hex(value)]["decision_version"] == 3
    assert to_hex(value) in seed_tool.load_rejections(db)


def test_failed_new_row_approval_cannot_delete_later_successful_approval(
    sandbox, tmp_path, monkeypatch
):
    db, _, _, _ = sandbox
    earlier, _, value = make_batch(tmp_path, name="batch-earlier")
    later, _, _ = make_batch(tmp_path, name="batch-later")
    original = authority.export_snapshot

    def interleave(path):
        if authority.read_authority(db)[to_hex(value)]["decision_source"] == "review:batch-earlier":
            assert apply(later, db) == 0
            raise PermissionError("synthetic older export failure")
        return original(path)

    monkeypatch.setattr(authority, "export_snapshot", interleave)
    assert apply(earlier, db) == 5
    raw = json.loads(seed_tool.rejection_snapshot_path(db).read_text(encoding="utf-8"))
    assert raw[to_hex(value)]["source"] == "review:batch-later"
    assert to_hex(value) in enabled_hashes(db)
    assert authority.read_authority(db)[to_hex(value)]["decision_version"] == 2


@pytest.mark.parametrize("replace_deleted_row", [False, True])
def test_compensation_does_not_modify_recreated_external_row(
    sandbox, tmp_path, monkeypatch, replace_deleted_row
):
    db, _, _, _ = sandbox
    initial, _, value = make_batch(tmp_path, name="batch-initial")
    disabled, _, _ = make_batch(tmp_path, name="batch-disabled", verdict="撤回")
    earlier, _, _ = make_batch(tmp_path, name="batch-earlier")
    assert apply(initial, db) == 0
    assert apply(disabled, db) == 0

    def interleave(path):
        with sqlite3.connect(db) as con:
            con.execute("DELETE FROM image_allowlist WHERE phash=?", (to_hex(value),))
            if replace_deleted_row:
                con.execute(
                    "INSERT INTO image_allowlist(id,phash,note,source,enabled,created_at,created_by) VALUES(7001,?,'outside-row','outside',1,'2026-09-20','outside')",
                    (to_hex(value),),
                )
        raise PermissionError("synthetic export failure after independent mutation")

    monkeypatch.setattr(authority, "export_snapshot", interleave)
    assert apply(earlier, db) == 5
    with sqlite3.connect(db) as con:
        row = con.execute(
            "SELECT id,enabled,note FROM image_allowlist WHERE phash=?", (to_hex(value),)
        ).fetchone()
    assert row == (7001, 1, "outside-row") if replace_deleted_row else row is None
    if replace_deleted_row:
        with pytest.raises(authority.AuthorityError, match="NOT_BACKFILLED"):
            authority.read_authority(db)


def test_failed_reapproval_restores_prior_note_without_erasing_old_history(
    sandbox, tmp_path, monkeypatch
):
    db, _, _, _ = sandbox
    initial, _, value = make_batch(tmp_path, name="batch-initial")
    disabled, _, _ = make_batch(tmp_path, name="batch-disabled", verdict="撤回")
    earlier, _, _ = make_batch(tmp_path, name="batch-earlier")
    assert apply(initial, db) == 0
    assert apply(disabled, db) == 0
    old_note = "prior-audit;reapproved:2026-09-19;excluded:2026-09-19"
    with sqlite3.connect(db) as con:
        con.execute("UPDATE image_allowlist SET note=? WHERE phash=?", (old_note, to_hex(value)))
    old_events = json.loads(authority.read_authority(db)[to_hex(value)]["history_json"])["events"]

    def fail(path):
        raise PermissionError("synthetic export failure")

    monkeypatch.setattr(authority, "export_snapshot", fail)
    assert apply(earlier, db) == 5
    row = authority.read_authority(db)[to_hex(value)]
    assert (row["enabled"], row["decision_state"], row["decision_version"]) == (1, "allowed", 3)
    events = json.loads(row["history_json"])["events"]
    assert events[:-1] == old_events
    assert events[-1]["previous_note"] == old_note
