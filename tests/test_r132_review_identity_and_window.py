# ruff: noqa: E402, I001, F401, F811, SIM105, S101
# A2 REGISTERED ADAPTATION: original bytes are sealed under docs/evidence/authority-a2-20260922/legacy-probes/.
# Historical nodeids are retained; current contracts and every changed AST node are registered in docs/2026-09-22-authority-a2-adaptations.md.
# Reviewer round-9 probe pack (dbd80a5), promoted into the repo suite.
# Only this header was added, PLUS one documented platform adaptation:
#   `test_shadow_window_cli_is_executable_and_half_open` 用 `read_text()`（无 encoding）
#   读取**本仓库 UTF-8 报告**；Windows 默认 GBK 会抛 UnicodeDecodeError（macOS 默认 UTF-8 通过）。
#   现补 `encoding="utf-8"`——只影响读取方式，断言与意图逐字未改。
"""Synthetic evidence-only probes. dbd80a5 falls back to frozen e3f321c verifier.

No production DB, real image, QQ, model provider, or Windows service is touched.
"""

import hashlib
import importlib.util
import io
import json
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from scripts import shadow_report


@pytest.fixture
def verifier(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path.cwd() / "scripts"))
    # Repair reruns must exercise the current checkout, not remain pinned to the
    # old failing implementation. Fixed dbd80a5 has no verifier yet, so only that
    # review falls back to the supplied e3f321c source. Assertions are unchanged.
    source = Path.cwd() / "scripts" / "verify_approved_identity.py"
    if not source.is_file():
        source = (
            Path(__file__).resolve().parents[2] / "scratch" / "verify_approved_identity_e3f321c.py"
        )
    spec = importlib.util.spec_from_file_location("identity_e3f_review", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "reviews"
    root.mkdir()
    monkeypatch.setattr(module, "REVIEW_ROOT", root)
    monkeypatch.setattr(module, "OUT_DIR", tmp_path / "outputs")
    return module


def image_bytes():
    out = io.BytesIO()
    Image.new("RGB", (16, 16), (100, 140, 190)).save(out, format="PNG")
    return out.getvalue()


def make_batch(
    verifier,
    name="batch-20260101T000000Z",
    *,
    sha=None,
    manifest_hash=None,
    verdict="放行",
    decisions_batch=None,
):
    batch = verifier.REVIEW_ROOT / name
    batch.mkdir()
    raw = image_bytes()
    phash = verifier.to_hex(verifier.dhash64(raw))
    (batch / "synthetic.png").write_bytes(raw)
    expected_sha = hashlib.sha256(raw).hexdigest() if sha is None else sha
    (batch / "IMAGE_REVIEW.md").write_text(
        f"| 1 | synthetic | `synthetic.png` | test | `{expected_sha}` | "
        f"`{manifest_hash or phash}` |\n",
        encoding="utf-8",
    )
    (batch / "DECISIONS.json").write_text(
        json.dumps(
            {
                "batch": decisions_batch or name,
                "decisions": {"1": verdict},
            }
        ),
        encoding="utf-8",
    )
    return batch, phash


def make_db(tmp_path, phash=None, batch=None):
    db = tmp_path / "synthetic.db"
    with sqlite3.connect(db) as con:
        con.execute(
            "CREATE TABLE image_allowlist (id INTEGER PRIMARY KEY, phash TEXT UNIQUE, note TEXT DEFAULT '', source TEXT DEFAULT '', created_at TEXT NOT NULL, enabled INTEGER DEFAULT 1, created_by TEXT DEFAULT '', hit_count INTEGER DEFAULT 0)"
        )
        if phash:
            con.execute(
                "INSERT INTO image_allowlist(phash,note,created_at,enabled,source) VALUES (?, ?, ?, 1, '')",
                (phash, f"review:{batch.name}:no=1", "2026-01-02 00:00:00"),
            )
    from tests.authority_fixtures import initialize_authority

    initialize_authority(db)
    return db


def run_identity(verifier, db, *extra):
    before = db.read_bytes()
    snapshot = verifier.rejection_snapshot_path(db)
    snapshot_before = snapshot.read_bytes() if snapshot.exists() else None
    code = verifier.main(["--db", str(db), *extra])
    report = json.loads(next(verifier.OUT_DIR.glob("identity-*.json")).read_text())
    assert db.read_bytes() == before
    assert (snapshot.read_bytes() if snapshot.exists() else None) == snapshot_before
    return code, report


def test_valid_identity_control_is_read_only(verifier, tmp_path):
    batch, phash = make_batch(verifier)
    db = make_db(tmp_path, phash, batch)
    from scripts.image_decision_authority import export_snapshot

    export_snapshot(db)
    code, report = run_identity(verifier, db)
    assert code == 0 and report["allowed_checked"] == 1 and not report["mismatches"]


@pytest.mark.parametrize("sha", ["", "-"])
def test_missing_manifest_sha_cannot_prove_approved_bytes(verifier, tmp_path, sha):
    batch, phash = make_batch(verifier, sha=sha)
    db = make_db(tmp_path, phash, batch)
    code, report = run_identity(verifier, db)
    assert code != 0, report


def test_manifest_dhash_mismatch_is_not_ignored(verifier, tmp_path):
    batch, phash = make_batch(verifier, manifest_hash="ffffffffffffffff")
    db = make_db(tmp_path, phash, batch)
    code, report = run_identity(verifier, db)
    assert code != 0, report


def test_decisions_batch_identity_must_match_manifest_batch(verifier, tmp_path):
    batch, phash = make_batch(verifier, decisions_batch="batch-19990101T000000Z")
    db = make_db(tmp_path, phash, batch)
    code, report = run_identity(verifier, db)
    assert code != 0, report


def test_explicit_batch_cannot_replace_row_provenance(verifier, tmp_path):
    original, phash = make_batch(verifier, verdict="撤回")
    replacement, _ = make_batch(verifier, "batch-20260102T000000Z", verdict="放行")
    db = make_db(tmp_path, phash, original)
    code, report = run_identity(verifier, db, "--batch", str(replacement))
    assert code != 0, report


def test_corrupt_rejection_snapshot_must_not_report_overall_success(verifier, tmp_path):
    batch, phash = make_batch(verifier)
    db = make_db(tmp_path, phash, batch)
    verifier.rejection_snapshot_path(db).write_text("{broken-json")
    code, report = run_identity(verifier, db)
    assert report["rejection_state"] == "corrupt"
    assert code != 0, report


def test_rejection_must_bind_current_snapshot_source_not_any_old_batch(verifier, tmp_path):
    _old, phash = make_batch(verifier, verdict="撤回")
    current, _ = make_batch(verifier, "batch-20260102T000000Z", verdict="放行")
    db = make_db(tmp_path)
    verifier.rejection_snapshot_path(db).write_text(
        json.dumps(
            {
                phash: {
                    "state": "rejected",
                    "source": f"review:{current.name}",
                    "operator": "synthetic-owner",
                    "at": "2026-01-02T01:00:00Z",
                    "history": [],
                }
            }
        )
    )
    from tests.authority_fixtures import decide

    decide(db, phash, "rejected", f"review:{current.name}")
    code, report = run_identity(verifier, db)
    assert code != 0, report


def test_latest_reapproval_source_must_be_verified(verifier, tmp_path):
    original, phash = make_batch(verifier, verdict="放行")
    current, _ = make_batch(verifier, "batch-20260102T000000Z", verdict="撤回")
    db = make_db(tmp_path, phash, original)
    verifier.rejection_snapshot_path(db).write_text(
        json.dumps(
            {
                phash: {
                    "state": "approved",
                    "source": f"review:{current.name}",
                    "operator": "synthetic-owner",
                    "at": "2026-01-02T01:00:00Z",
                    "history": [],
                }
            }
        )
    )
    from tests.authority_fixtures import decide

    decide(db, phash, "allowed", f"review:{current.name}")
    code, report = run_identity(verifier, db)
    assert code != 0, report


@pytest.mark.parametrize(
    "extra",
    [
        ["--since", "2026-01-02 00:00:00", "--until", "2026-01-03 00:00:00"],
        ["--since", "2026-01-02 00:00:00"],
        ["--until", "2026-01-03 00:00:00"],
    ],
)
def test_shadow_window_cli_is_executable_and_half_open(tmp_path, monkeypatch, extra):
    db = tmp_path / "shadow.db"
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
    before = db.read_bytes()
    monkeypatch.setattr(shadow_report, "OUT_DIR", tmp_path / "output")
    assert shadow_report.main(["--db", str(db), *extra]) == 0
    assert db.read_bytes() == before
    report = next((tmp_path / "output").glob("shadow-*.md")).read_text(encoding="utf-8")
    if "--since" in extra and "--until" in extra:
        assert "有 `image_hash` 观察的判定**：1 条" in report, report
        assert "累计判定 3 条" not in report, report
