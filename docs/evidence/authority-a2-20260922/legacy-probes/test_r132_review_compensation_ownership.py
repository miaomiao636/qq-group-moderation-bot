# ruff: noqa: E402, I001, F401, F811, SIM105, S101
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
from scripts import apply_review_decisions as apply_tool
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
    original = apply_tool.record_rejection
    later_success = []

    def interleave(db_arg, hash_arg, *, source, operator):
        if source == "review:batch-earlier":
            assert to_hex(value) not in enabled_hashes(db)
            later_success.append(apply(later, db))
            assert later_success == [0]
            assert to_hex(value) in seed_tool.load_rejections(db)
            raise PermissionError("synthetic earlier snapshot write failure")
        return original(db_arg, hash_arg, source=source, operator=operator)

    monkeypatch.setattr(apply_tool, "record_rejection", interleave)
    with pytest.raises(PermissionError):
        apply(earlier, db)
    raw = json.loads(seed_tool.rejection_snapshot_path(db).read_text(encoding="utf-8"))
    assert raw[to_hex(value)]["state"] == "rejected"
    assert to_hex(value) not in enabled_hashes(db), (
        "Older failed operation restored enabled=1 over the later successful rejection."
    )
    assert to_hex(value) in seed_tool.load_rejections(db)


def test_failed_new_row_approval_cannot_delete_later_successful_approval(
    sandbox, tmp_path, monkeypatch
):
    db, _, _, _ = sandbox
    earlier, _, value = make_batch(tmp_path, name="batch-earlier")
    later, _, _ = make_batch(tmp_path, name="batch-later")
    original = apply_tool.record_approval
    later_success = []

    def interleave(db_arg, hash_arg, *, source, operator):
        if source == "review:batch-earlier":
            later_success.append(apply(later, db))
            assert later_success == [0]
            assert to_hex(value) in enabled_hashes(db)
            raise PermissionError("synthetic earlier snapshot write failure")
        return original(db_arg, hash_arg, source=source, operator=operator)

    monkeypatch.setattr(apply_tool, "record_approval", interleave)
    with pytest.raises(PermissionError):
        apply(earlier, db)
    raw = json.loads(seed_tool.rejection_snapshot_path(db).read_text(encoding="utf-8"))
    assert raw[to_hex(value)]["source"] == "review:batch-later"
    assert to_hex(value) in enabled_hashes(db), "Compensation deleted another successful approval."


@pytest.mark.parametrize("replace_deleted_row", [False, True])
def test_compensation_does_not_modify_recreated_external_row(
    sandbox, tmp_path, monkeypatch, replace_deleted_row
):
    db, _, _, _ = sandbox
    initial, _, value = make_batch(tmp_path, name="batch-initial")
    disabled, _, _ = make_batch(tmp_path, name="batch-disable", verdict="撤回")
    earlier, _, _ = make_batch(tmp_path, name="batch-earlier")
    assert apply(initial, db) == 0
    assert apply(disabled, db) == 0
    original = apply_tool.record_approval

    def interleave(db_arg, hash_arg, *, source, operator):
        assert source == "review:batch-earlier"
        with sqlite3.connect(db) as con:
            con.execute("DELETE FROM image_allowlist WHERE phash=?", (to_hex(value),))
            if replace_deleted_row:
                con.execute(
                    "INSERT INTO image_allowlist(id,phash,note,source,enabled,created_at,created_by) "
                    "VALUES(7001,?,'outside-row','outside',1,'2026-09-20','outside')",
                    (to_hex(value),),
                )
        if replace_deleted_row:
            original(db_arg, hash_arg, source="outside-new-row", operator="synthetic-outside")
        raise PermissionError("synthetic failed write after external replacement")

    monkeypatch.setattr(apply_tool, "record_approval", interleave)
    with pytest.raises(PermissionError):
        apply(earlier, db)
    with sqlite3.connect(db) as con:
        row = con.execute(
            "SELECT id,enabled,note FROM image_allowlist WHERE phash=?", (to_hex(value),)
        ).fetchone()
    if replace_deleted_row:
        assert row == (7001, 1, "outside-row"), (
            "Compensation modified a replacement row it did not own."
        )
    else:
        assert row is None, "Compensation must not resurrect a concurrently deleted row."


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

    def fail(*_args, **_kwargs):
        raise PermissionError("synthetic current snapshot failure")

    monkeypatch.setattr(apply_tool, "record_approval", fail)
    with pytest.raises(PermissionError):
        apply(earlier, db)
    with sqlite3.connect(db) as con:
        row = con.execute(
            "SELECT enabled,note FROM image_allowlist WHERE phash=?", (to_hex(value),)
        ).fetchone()
    assert row == (0, old_note), (
        "Compensation stripped historical markers not created by this call."
    )
