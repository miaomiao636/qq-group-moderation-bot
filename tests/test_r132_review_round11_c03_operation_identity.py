# ruff: noqa: E402, I001, F401, F811, SIM105, S101
# Reviewer pack (round-11 review of 6505a79), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
"""6505a79: operation ownership across commit, JSON persistence, and compensation.

Generated images and temporary SQLite only. Fault hooks use real application and
database operations; no production data or business result is mocked.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, get_ident

import pytest
from PIL import Image

from app.moderation.image_hash import dhash64_file, to_hex
from scripts import apply_review_decisions as apply_tool
from scripts import image_allowlist_seed as seed_tool
from tests.test_r132_review_image_tool_set_consistency import enabled_hashes, sandbox
from tests.test_r132_review_review_write_contract import apply, make_batch


def two_picture_batch(tmp_path):
    batch, _, first = make_batch(tmp_path)
    picture = batch / "second.png"
    Image.new("L", (64, 64), 0).save(picture)
    second = dhash64_file(picture)
    assert second is not None and second != first
    digest = hashlib.sha256(picture.read_bytes()).hexdigest()
    manifest = batch / "IMAGE_REVIEW.md"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + f"| 02 | 候选 | `second.png` | candidate | {digest} | {to_hex(second)} | "
        "- | - | 1 | **0** | record_only:1 | ad:1 | synthetic | |\n",
        encoding="utf-8",
    )
    (batch / "DECISIONS.json").write_text(
        json.dumps({"decisions": {"01": "放行", "02": "放行"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return batch, to_hex(first), to_hex(second)


@pytest.mark.parametrize("fail_second", [False, True])
def test_partial_snapshot_reports_accepted_manual_recovery_state(
    sandbox, tmp_path, monkeypatch, capsys, fail_second
):
    db, _, _, _ = sandbox
    batch, first, second = two_picture_batch(tmp_path)
    original = apply_tool.record_approval
    written = []

    def write(db_arg, value, *, source, operator):
        if fail_second and value == second:
            assert written == [first]
            raise PermissionError("synthetic second snapshot write denied")
        result = original(db_arg, value, source=source, operator=operator)
        written.append(value)
        return result

    monkeypatch.setattr(apply_tool, "record_approval", write)
    if fail_second:
        with pytest.raises(PermissionError):
            apply(batch, db)
        # The owner explicitly accepts incomplete restoration when reported as
        # COMPENSATION_CONFLICT/manual recovery. Preserve this as a positive control,
        # not an unrequested demand for an all-or-nothing architecture.
        output = capsys.readouterr().out
        assert "COMPENSATION_CONFLICT" in output and "conflicts=1" in output
        assert "IMPORT_OK" not in output
        assert enabled_hashes(db) == {first}
    else:
        assert apply(batch, db) == 0
        assert enabled_hashes(db) == {first, second}


def test_after_snapshot_cannot_adopt_an_external_row_as_its_own(sandbox, tmp_path, monkeypatch):
    db, _, _, _ = sandbox
    batch, _, value = make_batch(tmp_path)
    original_import = apply_tool.import_seeds

    def interleave_after_commit(**kwargs):
        result = original_import(**kwargs)
        # Real independently committed replacement after import commit but before
        # the caller's separate `after = _states()` query.
        with sqlite3.connect(db) as con:
            con.execute("DELETE FROM image_allowlist WHERE phash=?", (to_hex(value),))
            con.execute(
                "INSERT INTO image_allowlist(id,phash,note,source,enabled,created_at,created_by) "
                "VALUES(9001,?,'independent-row','outside',1,'2026-09-20','outside')",
                (to_hex(value),),
            )
        return result

    def fail_snapshot(*_args, **_kwargs):
        raise PermissionError("synthetic snapshot persistence failure")

    monkeypatch.setattr(apply_tool, "import_seeds", interleave_after_commit)
    monkeypatch.setattr(apply_tool, "record_approval", fail_snapshot)
    with pytest.raises(PermissionError):
        apply(batch, db)
    with sqlite3.connect(db) as con:
        row = con.execute(
            "SELECT id,enabled,note FROM image_allowlist WHERE phash=?", (to_hex(value),)
        ).fetchone()
    assert row == (9001, 1, "independent-row"), (
        "Post-commit `after` captured a foreign row; compensation deleted that row."
    )


def test_late_json_commit_of_successful_rejection_cannot_be_overwritten(
    sandbox, tmp_path, monkeypatch
):
    db, _, _, _ = sandbox
    initial, _, value = make_batch(tmp_path, name="batch-initial")
    earlier, _, _ = make_batch(tmp_path, name="batch-earlier", verdict="撤回")
    later, _, _ = make_batch(tmp_path, name="batch-later", verdict="撤回")
    assert apply(initial, db) == 0
    snapshot = seed_tool.rejection_snapshot_path(db)
    later_at_json = Event()
    allow_later_json = Event()
    compensation_phase = Event()
    main_thread = get_ident()
    original_record = apply_tool.record_rejection
    original_read = Path.read_bytes
    later_future = []
    released = False

    with ThreadPoolExecutor(max_workers=1) as pool:

        def controlled_record(db_arg, hash_arg, *, source, operator):
            if source == "review:batch-earlier":
                later_future.append(pool.submit(apply, later, db))
                assert later_at_json.wait(5)
                compensation_phase.set()
                raise PermissionError("synthetic earlier snapshot failure")
            if source == "review:batch-later":
                later_at_json.set()  # Later DB commit already completed.
                assert allow_later_json.wait(5)
            return original_record(db_arg, hash_arg, source=source, operator=operator)

        def controlled_read(path):
            nonlocal released
            content = original_read(path)
            if (
                path == snapshot
                and get_ident() == main_thread
                and compensation_phase.is_set()
                and not released
            ):
                released = True
                # Compensation has read the old JSON, but BEGIN IMMEDIATE does not
                # block a writer that has already committed DB and only writes JSON.
                allow_later_json.set()
                assert later_future[0].result(timeout=5) == 0
            return content

        monkeypatch.setattr(apply_tool, "record_rejection", controlled_record)
        monkeypatch.setattr(Path, "read_bytes", controlled_read)
        try:
            with pytest.raises(PermissionError):
                apply(earlier, db)
        finally:
            allow_later_json.set()
    assert released
    raw = json.loads(original_read(snapshot).decode("utf-8"))
    assert raw[to_hex(value)]["state"] == "rejected"
    assert to_hex(value) not in enabled_hashes(db), (
        "Later full apply succeeded after JSON read; older compensation restored enabled=1."
    )
