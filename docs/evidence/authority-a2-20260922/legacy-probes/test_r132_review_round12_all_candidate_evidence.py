# ruff: noqa: E402, I001, F401, F811, SIM105, S101
# Reviewer round-12 pack (166a1ab), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
"""C04-R: each claimed candidate must have a complete byte/decision chain.

No production data. Reuses the now-committed synthetic fixtures only.
"""

import json

import pytest

from tests.test_r132_review_identity_and_window import make_batch, make_db, verifier
from tests.test_r132_review_round11_identity_collisions import (
    collision_batch,
    run_identity,
    write_snapshot,
)


def context(verifier, tmp_path, state):
    expected = "放行" if state == "approved" else "撤回"
    original, phash = make_batch(verifier)
    current, collision_hash = collision_batch(
        verifier, "batch-20260102T000000Z", (expected, expected)
    )
    assert phash == collision_hash
    db = make_db(tmp_path, phash, original) if state == "approved" else make_db(tmp_path)
    write_snapshot(verifier, db, phash, {"state": state, "source": f"review:{current.name}"})
    return db, current


@pytest.mark.parametrize("state", ["approved", "rejected"])
@pytest.mark.parametrize(
    "fault", ["missing_file", "wrong_sha", "unreadable_image", "missing_decision"]
)
def test_nonselected_candidate_must_not_be_falsely_certified(verifier, tmp_path, state, fault):
    db, current = context(verifier, tmp_path, state)
    target = current / "synthetic-2.png"
    if fault == "missing_file":
        target.unlink()  # Only the generated second synthetic candidate.
    elif fault == "unreadable_image":
        target.write_bytes(b"synthetic-not-a-picture")
    elif fault == "wrong_sha":
        manifest = current / "IMAGE_REVIEW.md"
        lines = manifest.read_text(encoding="utf-8").splitlines()
        cells = lines[1].split("|")
        cells[5] = f" `{'f' * 64}` "
        lines[1] = "|".join(cells)
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        path = current / "DECISIONS.json"
        decisions = json.loads(path.read_text(encoding="utf-8"))
        del decisions["decisions"]["2"]
        path.write_text(json.dumps(decisions), encoding="utf-8")
    code, report = run_identity(verifier, db)
    assert code != 0 and report["mismatches"], report


@pytest.mark.parametrize("state", ["approved", "rejected"])
def test_all_candidates_with_valid_bytes_and_same_decision_may_pass(verifier, tmp_path, state):
    db, _current = context(verifier, tmp_path, state)
    code, report = run_identity(verifier, db)
    assert code == 0 and not report["mismatches"], report


def test_legal_reapproval_renumbering_still_passes(verifier, tmp_path):
    original, phash = make_batch(verifier)
    current, _ = make_batch(verifier, "batch-20260102T000000Z")
    path = current / "IMAGE_REVIEW.md"
    path.write_text(path.read_text(encoding="utf-8").replace("| 1 |", "| 7 |", 1), encoding="utf-8")
    (current / "DECISIONS.json").write_text(
        json.dumps(
            {
                "batch": current.name,
                "decisions": {"7": "放行"},
            }
        ),
        encoding="utf-8",
    )
    db = make_db(tmp_path, phash, original)
    write_snapshot(verifier, db, phash, {"state": "approved", "source": f"review:{current.name}"})
    code, report = run_identity(verifier, db)
    assert code == 0 and not report["mismatches"], report
