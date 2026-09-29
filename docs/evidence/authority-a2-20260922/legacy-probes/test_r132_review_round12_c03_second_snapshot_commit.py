# ruff: noqa: E402, I001, F401, F811, SIM105, S101
# Reviewer round-12 pack (166a1ab), from probes/images/test_c03_second_snapshot_commit.py.
#
# REGISTERED ADAPTATION (the only one; scheduler only):
#   The reviewer's file released the later review from inside A's N-th compensation
#   snapshot read (params 1 and 2). Under reviewer batch-12 **option B** -- one
#   cross-process decision lock covering pre-state read -> DB change -> snapshot
#   publish -> compensation -- that arrival point is UNREACHABLE: the later review is
#   reliably BLOCKED on A's lock, so it cannot commit DB and then be left with only
#   JSON work. The 2 params therefore collapse into one scenario that asserts:
#     * real threads/DB/snapshot writes (unchanged),
#     * the later review is genuinely blocked while A holds the lock,
#     * both reviews then complete sequentially and the FINAL state is consistent
#       (snapshot=rejected, DB row not enabled, IMPORT_OK count 2) -- the reviewer's
#       own accepted outcome mode 1 ("competitor reliably blocked, then sequential and
#       finally correct").
#   The reviewer's "COMPENSATION_CONFLICT must be printed" expectation belongs to
#   outcome mode 2 and does not apply once A's window cannot be crossed; A's
#   compensation report line is still asserted. No lock was weakened to run this.
"""166a1ab: later review completion vs the decision-lock boundary (synthetic only)."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from app.moderation.image_hash import to_hex
from scripts import apply_review_decisions as apply_tool
from scripts import image_allowlist_seed as seed_tool
from tests.test_r132_review_image_tool_set_consistency import enabled_hashes, sandbox
from tests.test_r132_review_review_write_contract import apply, make_batch


def test_blocked_later_rejection_serializes_and_final_state_is_consistent(
    sandbox, tmp_path, monkeypatch, capsys
):
    db, _, _, _ = sandbox
    initial, _, value = make_batch(tmp_path, name="batch-initial")
    earlier, _, _ = make_batch(tmp_path, name="batch-earlier", verdict="撤回")
    later, _, _ = make_batch(tmp_path, name="batch-later", verdict="撤回")
    assert apply(initial, db) == 0
    snapshot = seed_tool.rejection_snapshot_path(db)
    later_future = []
    later_started = Event()
    original_record = apply_tool.record_rejection
    original_read = Path.read_bytes

    def run_later():
        later_started.set()
        return apply(later, db)

    with ThreadPoolExecutor(max_workers=1) as pool:

        def controlled_record(db_arg, hash_arg, *, source, operator):
            if source == "review:batch-earlier":
                later_future.append(pool.submit(run_later))
                assert later_started.wait(10)
                # The later review must be waiting on the decision lock here: A holds
                # it across pre-state read -> DB change -> snapshot publish -> compensation.
                time.sleep(0.3)
                raise PermissionError("synthetic earlier snapshot persistence failure")
            return original_record(db_arg, hash_arg, source=source, operator=operator)

        monkeypatch.setattr(apply_tool, "record_rejection", controlled_record)
        with pytest.raises(PermissionError):
            apply(earlier, db)
        assert not later_future[0].done(), "later review must be blocked on the decision lock"
        assert later_future[0].result(timeout=30) == 0

    raw = json.loads(original_read(snapshot).decode("utf-8"))
    assert raw[to_hex(value)]["state"] == "rejected"
    output = capsys.readouterr().out
    assert "COMPENSATED_APPROVAL_ROLLBACK" in output
    assert output.count("IMPORT_OK") == 2  # Initial + later B; earlier A failed.
    assert to_hex(value) not in enabled_hashes(db), (
        f"Later review completed, but the older failed operation restored enabled=1. Output: {output}"
    )
