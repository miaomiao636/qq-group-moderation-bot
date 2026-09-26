# ruff: noqa: E402, I001, F401, F811, SIM105, S101
# Reviewer pack (round-11 review of 6505a79), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
"""Independent synthetic identity-source and UTC-window follow-ups for 194eb0b."""

import json
import sqlite3

import pytest

from scripts import shadow_report
from tests.test_r132_review_identity_and_window import (
    make_batch,
    make_db,
    run_identity,
    verifier,
)


def snapshot(verifier, db, phash, value):
    verifier.rejection_snapshot_path(db).write_text(json.dumps({phash: value}), encoding="utf-8")


@pytest.mark.parametrize("kind", ["nonobject", "approved-source-missing"])
def test_incomplete_snapshot_cannot_certify_current_identity(verifier, tmp_path, kind):
    batch, phash = make_batch(verifier)
    db = make_db(tmp_path, phash, batch)
    if kind == "nonobject":
        snapshot(verifier, db, phash, ["synthetic-invalid-entry"])
    elif kind == "approved-source-missing":
        snapshot(verifier, db, phash, {"state": "approved", "source": "", "history": []})
    code, report = run_identity(verifier, db)
    assert code != 0, report


def test_missing_snapshot_is_explicit_not_silently_certified_present(verifier, tmp_path):
    batch, phash = make_batch(verifier)
    db = make_db(tmp_path, phash, batch)
    _code, report = run_identity(verifier, db)
    # A never-created optional snapshot may be legitimate; absence is not corruption.
    assert report["rejection_state"] == "missing", report


def test_enabled_db_does_not_hide_rejected_snapshot_control(verifier, tmp_path):
    batch, phash = make_batch(verifier)
    db = make_db(tmp_path, phash, batch)
    snapshot(verifier, db, phash, {"state": "rejected", "source": f"review:{batch.name}"})
    code, report = run_identity(verifier, db)
    assert code != 0 and report["mismatches"], report


@pytest.mark.parametrize("fault", ["missing-picture", "wrong-declared-batch"])
def test_latest_approved_source_requires_full_identity_chain(verifier, tmp_path, fault):
    original, phash = make_batch(verifier)
    current, _ = make_batch(verifier, "batch-20260102T000000Z")
    if fault == "missing-picture":
        (current / "synthetic.png").unlink()  # Only this test's generated synthetic fixture.
    else:
        (current / "DECISIONS.json").write_text(
            json.dumps(
                {
                    "batch": "batch-19990101T000000Z",
                    "decisions": {"1": "放行"},
                }
            ),
            encoding="utf-8",
        )
    db = make_db(tmp_path, phash, original)
    snapshot(verifier, db, phash, {"state": "approved", "source": f"review:{current.name}"})
    code, report = run_identity(verifier, db)
    assert code != 0, report


def test_valid_reapproval_can_move_to_different_manifest_number(verifier, tmp_path):
    original, phash = make_batch(verifier)
    current, _ = make_batch(verifier, "batch-20260102T000000Z")
    manifest = current / "IMAGE_REVIEW.md"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("| 1 |", "| 7 |", 1), encoding="utf-8"
    )
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
    snapshot(verifier, db, phash, {"state": "approved", "source": f"review:{current.name}"})
    code, report = run_identity(verifier, db)
    assert code == 0 and not report["mismatches"], report


def test_conflicting_normalized_decision_numbers_are_rejected(verifier, tmp_path):
    batch, phash = make_batch(verifier)
    (batch / "DECISIONS.json").write_text(
        json.dumps(
            {
                "batch": batch.name,
                "decisions": {"01": "撤回", "1": "放行"},
            }
        ),
        encoding="utf-8",
    )
    db = make_db(tmp_path, phash, batch)
    verifier.rejection_snapshot_path(db).write_text("{}", encoding="utf-8")
    code, report = run_identity(verifier, db)
    assert code != 0, report


def test_conflicting_normalized_manifest_numbers_are_rejected(verifier, tmp_path):
    batch, phash = make_batch(verifier)
    manifest = batch / "IMAGE_REVIEW.md"
    valid = manifest.read_text(encoding="utf-8")
    invalid = valid.replace("| 1 |", "| 01 |", 1).replace("synthetic.png", "missing.png")
    manifest.write_text(invalid + valid, encoding="utf-8")
    db = make_db(tmp_path, phash, batch)
    verifier.rejection_snapshot_path(db).write_text("{}", encoding="utf-8")
    code, report = run_identity(verifier, db)
    assert code != 0, report


def make_shadow_db(tmp_path):
    db = tmp_path / "utc.db"
    with sqlite3.connect(db) as con:
        con.execute(
            "CREATE TABLE shadow_decisions (created_at TEXT, verdict TEXT, external_group_id TEXT, detail_json TEXT, message_id TEXT, kind TEXT)"
        )
        for day in (1, 2, 3):
            con.execute(
                "INSERT INTO shadow_decisions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"2026-01-0{day} 00:00:00",
                    "record_only",
                    "synthetic",
                    json.dumps({"image_hash": {"mode": "shadow", "checked": 1, "matched": False}}),
                    f"synthetic-{day}",
                    "image",
                ),
            )
    return db


@pytest.mark.parametrize(
    "lo,hi",
    [
        ("2026-01-02 00:00:00", "2026-01-03 00:00:00"),
        ("2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"),
        ("2026-01-02T08:00:00+08:00", "2026-01-03T08:00:00+08:00"),
    ],
)
def test_equivalent_utc_windows_select_same_half_open_rows(tmp_path, monkeypatch, lo, hi):
    db = make_shadow_db(tmp_path)
    monkeypatch.setattr(shadow_report, "OUT_DIR", tmp_path / "reports")
    canonical = "T" not in lo
    try:
        code = shadow_report.main(["--db", str(db), "--since", lo, "--until", hi])
    except (ValueError, SystemExit) as exc:
        # Accept a documented narrow input grammar, but never a false-success report.
        assert not canonical
        assert not isinstance(exc, SystemExit) or exc.code not in (0, None)
        assert not list((tmp_path / "reports").glob("shadow-*.md"))
        return
    if code != 0:
        assert not canonical
        assert not list((tmp_path / "reports").glob("shadow-*.md"))
        return
    report = next((tmp_path / "reports").glob("shadow-*.md")).read_text(encoding="utf-8")
    assert "有 `image_hash` 观察的判定**：1 条" in report, report
    assert "| 2026-01-02 00:00:00 |" in report, report
    assert "| 2026-01-03 00:00:00 |" not in report, report


@pytest.mark.parametrize(
    "lo,hi",
    [
        ("2026-01-03 00:00:00", "2026-01-02 00:00:00"),
        ("not-a-time", "2026-01-03 00:00:00"),
    ],
)
def test_invalid_or_reversed_window_must_not_issue_success(tmp_path, monkeypatch, lo, hi):
    db = make_shadow_db(tmp_path)
    monkeypatch.setattr(shadow_report, "OUT_DIR", tmp_path / "reports")
    try:
        code = shadow_report.main(["--db", str(db), "--since", lo, "--until", hi])
    except (ValueError, SystemExit) as exc:
        assert not isinstance(exc, SystemExit) or exc.code not in (0, None)
        assert not list((tmp_path / "reports").glob("shadow-*.md"))
        return
    assert code != 0
    assert not list((tmp_path / "reports").glob("shadow-*.md"))
