# ruff: noqa: E402, I001, F401, F811, SIM105, S101
# A2 REGISTERED ADAPTATION: original bytes are sealed under docs/evidence/authority-a2-20260922/legacy-probes/.
# Historical nodeids are retained; current contracts and every changed AST node are registered in docs/2026-09-22-authority-a2-adaptations.md.
# Reviewer pack (round-11 review of 6505a79), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
"""Synthetic C04/C05 follow-ups: exact identity ambiguity and accepted time precision.

Only temporary databases/images/reports. Does not read production or real QQ data.
"""

import hashlib
import io
import json
import sqlite3

import pytest
from PIL import Image

from scripts import shadow_report
from tests.test_r132_review_identity_and_window import make_batch, make_db, verifier


def run_identity(verifier, db):
    before = db.read_bytes()
    snap = verifier.rejection_snapshot_path(db)
    snap_before = snap.read_bytes() if snap.exists() else None
    code = verifier.main(["--db", str(db)])
    report = json.loads(next(verifier.OUT_DIR.glob("identity-*.json")).read_text(encoding="utf-8"))
    assert db.read_bytes() == before
    assert (snap.read_bytes() if snap.exists() else None) == snap_before
    return code, report


def write_snapshot(verifier, db, phash, entry):
    from tests.authority_fixtures import identity_snapshot

    identity_snapshot(verifier, db, phash, entry)


def collision_batch(verifier, name, verdicts):
    batch = verifier.REVIEW_ROOT / name
    batch.mkdir()
    lines, hashes, digests = [], [], []
    for number, color in enumerate(((100, 140, 190), (200, 40, 90)), start=1):
        buffer = io.BytesIO()
        Image.new("RGB", (16, 16), color).save(buffer, format="PNG")
        raw = buffer.getvalue()
        phash = verifier.to_hex(verifier.dhash64(raw))
        full = hashlib.sha256(raw).hexdigest()
        name = f"synthetic-{number}.png"
        (batch / name).write_bytes(raw)
        lines.append(f"| {number} | synthetic | `{name}` | test | `{full}` | `{phash}` |\n")
        hashes.append(phash)
        digests.append(full)
    assert hashes[0] == hashes[1] and digests[0] != digests[1]
    (batch / "IMAGE_REVIEW.md").write_text("".join(lines), encoding="utf-8")
    (batch / "DECISIONS.json").write_text(
        json.dumps(
            {
                "batch": batch.name,
                "decisions": {str(i): v for i, v in enumerate(verdicts, start=1)},
            }
        ),
        encoding="utf-8",
    )
    return batch, hashes[0]


@pytest.mark.parametrize("verdicts", [("放行", "撤回"), ("撤回", "放行")])
def test_reapproval_without_exact_identity_rejects_distinct_same_dhash_candidates(
    verifier, tmp_path, verdicts
):
    original, phash = make_batch(verifier)
    current, collision_hash = collision_batch(verifier, "batch-20260102T000000Z", verdicts)
    assert phash == collision_hash
    db = make_db(tmp_path, phash, original)
    write_snapshot(verifier, db, phash, {"state": "approved", "source": f"review:{current.name}"})
    code, report = run_identity(verifier, db)
    assert code != 0, report


@pytest.mark.parametrize("number", ["2", "999"])
def test_same_batch_note_cannot_silently_rebind_to_another_row(verifier, tmp_path, number):
    original, phash = collision_batch(verifier, "batch-20260101T000000Z", ("放行", "撤回"))
    db = make_db(tmp_path, phash, original)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE image_allowlist SET note=?", (f"review:{original.name}:no={number}",))
    code, report = run_identity(verifier, db)
    assert code != 0, report


def test_multiple_source_batches_cannot_take_the_first_match(verifier, tmp_path):
    original, phash = make_batch(verifier)
    approved, _ = make_batch(verifier, "batch-20260102T000000Z")
    rejected, _ = make_batch(verifier, "batch-20260103T000000Z", verdict="撤回")
    db = make_db(tmp_path, phash, original)
    write_snapshot(
        verifier,
        db,
        phash,
        {
            "state": "approved",
            "source": f"review:{approved.name};review:{rejected.name}",
        },
    )
    code, report = run_identity(verifier, db)
    assert code != 0, report


@pytest.mark.parametrize("state", ["pending", None, True, 7, ""])
def test_unknown_snapshot_state_cannot_certify_both_allow_and_reject(verifier, tmp_path, state):
    original, phash = make_batch(verifier)
    rejected, _ = make_batch(verifier, "batch-20260102T000000Z", verdict="撤回")
    db = make_db(tmp_path, phash, original)
    write_snapshot(verifier, db, phash, {"state": state, "source": f"review:{rejected.name}"})
    code, report = run_identity(verifier, db)
    assert code != 0, report


def test_null_snapshot_entry_still_fails_closed_control(verifier, tmp_path):
    original, phash = make_batch(verifier)
    db = make_db(tmp_path, phash, original)
    write_snapshot(verifier, db, phash, None)
    code, report = run_identity(verifier, db)
    assert code != 0 and report["mismatches"], report


def precision_db(tmp_path):
    db = tmp_path / "precision.db"
    with sqlite3.connect(db) as con:
        con.execute(
            "CREATE TABLE shadow_decisions (created_at TEXT, verdict TEXT, external_group_id TEXT, detail_json TEXT, message_id TEXT, kind TEXT)"
        )
        for fraction in ("00.250000", "00.500000", "00.750000", "01.000000", "01.500000"):
            con.execute(
                "INSERT INTO shadow_decisions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"2026-01-02 00:00:{fraction}",
                    "record_only",
                    "synthetic",
                    json.dumps({"image_hash": {"mode": "shadow", "checked": 1, "matched": False}}),
                    fraction,
                    "image",
                ),
            )
    return db


@pytest.mark.parametrize(
    "lo,hi,expected",
    [
        (
            "2026-01-02T00:00:00.500000Z",
            "2026-01-02T00:00:01.500000Z",
            ["00.500000", "00.750000", "01.000000"],
        ),
        (
            "2026-01-02T08:00:00.500000+08:00",
            "2026-01-02T08:00:01.500000+08:00",
            ["00.500000", "00.750000", "01.000000"],
        ),
        ("2026-01-02T00:00:00.250000Z", "2026-01-02T00:00:00.750000Z", ["00.250000", "00.500000"]),
    ],
)
def test_subsecond_bounds_are_exact_or_explicitly_rejected(tmp_path, monkeypatch, lo, hi, expected):
    db = precision_db(tmp_path)
    reports = tmp_path / "reports"
    monkeypatch.setattr(shadow_report, "OUT_DIR", reports)
    try:
        code = shadow_report.main(["--db", str(db), "--since", lo, "--until", hi])
    except (ValueError, SystemExit) as exc:
        assert not isinstance(exc, SystemExit) or exc.code not in (0, None)
        assert not list(reports.glob("shadow-*.md"))
        return
    if code != 0:
        assert not list(reports.glob("shadow-*.md"))
        return
    report = next(reports.glob("shadow-*.md")).read_text(encoding="utf-8")
    actual = [
        line.split(" | ")[0].replace("| 2026-01-02 00:00:", "")
        for line in report.splitlines()
        if line.startswith("| 2026-01-02 ")
    ]
    assert actual == sorted(expected, reverse=True), report
