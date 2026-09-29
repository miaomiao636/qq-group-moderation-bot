# ruff: noqa: E402, I001, F401, F811, SIM105, S101
# A2 REGISTERED ADAPTATION: original bytes are sealed under docs/evidence/authority-a2-20260922/legacy-probes/.
# Historical nodeids are retained; current contracts and every changed AST node are registered in docs/2026-09-22-authority-a2-adaptations.md.
# Reviewer pack (round-11 review of 6505a79); promoted into the repo suite.
# Registered adaptation (only one, and only the scheduler of the last test):
#   `test_late_json_commit_of_successful_rejection_cannot_be_overwritten` used to
#   release the later review from inside A's compensation snapshot read. Under the
#   cross-process decision lock (reviewer batch-12 option B) that arrival point is
#   UNREACHABLE: the later review is reliably BLOCKED on A's lock instead of
#   committing DB and only writing JSON. The test now asserts blocked-then-sequential
#   completion; its FINAL-state assertions (JSON=rejected, DB row not enabled) are
#   unchanged. See docs/2026-09-20-r132-round14-review-submission.md.
# Everything else in this file is verbatim.
"""6505a79: operation ownership across commit, JSON persistence, and compensation.

Generated images and temporary SQLite only. Fault hooks use real application and
database operations; no production data or business result is mocked.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, get_ident

import pytest
from PIL import Image

from app.moderation.image_hash import dhash64_file, to_hex
from scripts import image_decision_authority as authority
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
    if fail_second:

        def fail(path):
            rows = authority.read_authority(db)
            assert set(rows) == {first, second}
            assert all(r["decision_version"] == 1 for r in rows.values())
            raise PermissionError("synthetic whole-export failure after atomic batch")

        with monkeypatch.context() as controlled:
            controlled.setattr(authority, "export_snapshot", fail)
            assert apply(batch, db) == 5
        output = capsys.readouterr().out
        assert "DECISIONS_COMMITTED_EXPORT_PENDING" in output and "IMPORT_OK" not in output
        assert "COMPENSATED" not in output
        assert enabled_hashes(db) == {first, second}
        authority.export_snapshot(db)
        assert authority.export_state(db) == "ok"
    else:
        assert apply(batch, db) == 0
        assert enabled_hashes(db) == {first, second}


def test_after_snapshot_cannot_adopt_an_external_row_as_its_own(sandbox, tmp_path, monkeypatch):
    db, _, _, _ = sandbox
    batch, _, value = make_batch(tmp_path)

    def independent_replacement(path):
        with sqlite3.connect(db) as con:
            con.execute("DELETE FROM image_allowlist WHERE phash=?", (to_hex(value),))
            con.execute(
                "INSERT INTO image_allowlist(id,phash,note,source,enabled,created_at,created_by) VALUES(9001,?,'independent-row','outside',1,'2026-09-20','outside')",
                (to_hex(value),),
            )
        raise PermissionError("synthetic export failure after external replacement")

    monkeypatch.setattr(authority, "export_snapshot", independent_replacement)
    assert apply(batch, db) == 5
    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT id,enabled,note FROM image_allowlist WHERE phash=?", (to_hex(value),)
        ).fetchone() == (9001, 1, "independent-row")


def test_late_json_commit_of_successful_rejection_cannot_be_overwritten(
    sandbox, tmp_path, monkeypatch
):
    db, _, _, _ = sandbox
    initial, _, value = make_batch(tmp_path, name="batch-initial")
    earlier, _, _ = make_batch(tmp_path, name="batch-earlier", verdict="撤回")
    later, _, _ = make_batch(tmp_path, name="batch-later", verdict="撤回")
    assert apply(initial, db) == 0
    original = authority.export_snapshot
    started = Event()
    futures = []

    def run_later():
        started.set()
        return apply(later, db)

    with ThreadPoolExecutor(max_workers=1) as pool:

        def export(path):
            if (
                authority.read_authority(db)[to_hex(value)]["decision_source"]
                == "review:batch-earlier"
            ):
                futures.append(pool.submit(run_later))
                assert started.wait(10)
                time.sleep(0.3)
                assert not futures[0].done(), (
                    "later writer waits while the existing full decision lock is held"
                )
                raise PermissionError("synthetic earlier export failure")
            return original(path)

        monkeypatch.setattr(authority, "export_snapshot", export)
        assert apply(earlier, db) == 5
        assert futures[0].result(timeout=30) == 0
    raw = json.loads(seed_tool.rejection_snapshot_path(db).read_text(encoding="utf-8"))
    assert raw[to_hex(value)]["state"] == "rejected"
    assert to_hex(value) not in enabled_hashes(db)
    assert authority.read_authority(db)[to_hex(value)]["decision_version"] == 3
