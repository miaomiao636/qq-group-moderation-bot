# ruff: noqa: E402, I001, F401, F811, SIM105
# A2 REGISTERED ADAPTATION: original bytes are sealed under docs/evidence/authority-a2-20260922/legacy-probes/.
# Historical nodeids are retained; current contracts and every changed AST node are registered in docs/2026-09-22-authority-a2-adaptations.md.
# Reviewer round-6 probe pack (85b0c0b), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
"""Independent review probes. All databases/images are synthetic in pytest tmp_path.

Run from reviewed checkout with PYTHONPATH=.:scripts and pytest -o addopts=''.
Assertions describe the expected safe tooling contract; failures are review evidence.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from scripts import image_allowlist_replay as replay_tool
from scripts import image_allowlist_seed as seed_tool
from scripts import image_review_export as export_tool
from app.moderation.image_hash import dhash64_file, to_hex


@pytest.fixture
def sandbox(tmp_path):
    db = tmp_path / "synthetic.db"
    media = tmp_path / "media"
    samples = tmp_path / "samples"
    media.mkdir()
    samples.mkdir()
    with sqlite3.connect(db) as con:
        con.executescript(
            "CREATE TABLE shadow_decisions (message_id TEXT, created_at TEXT, kind TEXT, "
            "verdict TEXT, category TEXT, external_group_id TEXT, detail_json TEXT);"
            "CREATE TABLE image_allowlist (id INTEGER PRIMARY KEY, phash TEXT NOT NULL UNIQUE, "
            "note TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', "
            "enabled INTEGER NOT NULL DEFAULT 1, hit_count INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT '');"
        )
    from tests.authority_fixtures import initialize_authority

    initialize_authority(db)
    return db, media, samples


def synthetic_image(path: Path, color: str = "blue") -> int:
    Image.new("RGB", (48, 48), color=color).save(path)
    value = dhash64_file(path)
    assert value is not None
    return value


def decision(
    db, name, verdict="record_only", *, category="", detail=None, when="2026-09-19 02:00:00"
):
    value = {"media_files": [{"name": name}], "rule_hits": [], "ai_results": []}
    if detail:
        value.update(detail)
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO shadow_decisions VALUES (?,?,?,?,?,?,?)",
            (name, when, "image", verdict, category, "synthetic-group", json.dumps(value)),
        )


def test_seed_duplicate_control(sandbox):
    db, media, _ = sandbox
    path = media / "one.png"
    synthetic_image(path)
    kwargs = dict(db=db, seeds=[(path, "sample", "synthetic")], dry_run=False, operator="review")
    assert seed_tool.import_seeds(**kwargs) == (1, 0, 0, 0)
    assert seed_tool.import_seeds(**kwargs) == (0, 1, 0, 0)


def test_seed_excludes_first_insert_control(sandbox):
    db, media, _ = sandbox
    path = media / "one.png"
    value = synthetic_image(path)
    assert seed_tool.import_seeds(
        db=db,
        seeds=[(path, "sample", "synthetic")],
        dry_run=False,
        operator="review",
        excluded={to_hex(value)},
    ) == (0, 0, 0, 1)
    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT phash,enabled,decision_state,decision_version FROM image_allowlist"
        ).fetchall() == [(to_hex(value), 0, "rejected", 1)]


def test_exclusion_after_import_must_not_leave_enabled_entry(sandbox):
    db, media, _ = sandbox
    path = media / "one.png"
    value = synthetic_image(path)
    kwargs = dict(db=db, seeds=[(path, "sample", "synthetic")], dry_run=False, operator="review")
    assert seed_tool.import_seeds(**kwargs) == (1, 0, 0, 0)
    assert seed_tool.import_seeds(**kwargs, excluded={to_hex(value)}) == (0, 0, 0, 1)
    with sqlite3.connect(db) as con:
        assert (
            con.execute("SELECT COUNT(*) FROM image_allowlist WHERE enabled=1").fetchone()[0] == 0
        )


def test_replay_uses_same_excluded_seed_set(sandbox):
    db, media, samples = sandbox
    path = samples / "one.png"
    value = synthetic_image(path)
    media_path = media / "one.png"
    synthetic_image(media_path)
    decision(db, media_path.name)
    # The seed importer accepts the explicit reviewer exclusion and writes no row.
    seed_tool.import_seeds(
        db=db,
        seeds=seed_tool.scan_samples(samples),
        dry_run=False,
        operator="review",
        excluded={to_hex(value)},
    )
    # Replay reconstructs its set independently; it must not suggest excluded content can allow.
    whitelist = replay_tool.load_whitelist(samples, db, media)
    result = replay_tool.replay(
        db=db, media_dir=media, whitelist=whitelist, max_distance=2, limit=100
    )
    assert result["would_change"] == []


def test_unresolved_other_attachment_is_not_a_predicted_allow(sandbox):
    db, media, _ = sandbox
    path = media / "one.png"
    value = synthetic_image(path)
    decision(
        db,
        path.name,
        detail={
            "media_files": [{"name": path.name}, {"name": "missing.png"}],
            "evidence_vetoes": ["media_missing"],
            "ai_results": [
                {
                    "source": "vision",
                    "category": None,
                    "confidence": 0.99,
                    "has_miniprogram_code": True,
                    "needs_review": False,
                    "degraded_reason": "",
                },
                {
                    "source": "degraded",
                    "category": None,
                    "confidence": 0,
                    "needs_review": True,
                    "degraded_reason": "timeout",
                },
            ],
        },
    )
    result = replay_tool.replay(
        db=db, media_dir=media, whitelist=[(value, "synthetic-approved")], max_distance=2, limit=100
    )
    assert result["would_change"] == []


def test_export_does_not_hide_distinct_bytes_behind_one_seed(sandbox):
    db, media, _ = sandbox
    first = media / "newer-blue.png"
    second = media / "older-red.png"
    phash = synthetic_image(first, "blue")
    assert synthetic_image(second, "red") == phash
    assert first.read_bytes() != second.read_bytes()
    decision(db, first.name, when="2026-09-19 03:00:00")
    decision(db, second.name, when="2026-09-19 02:00:00")
    groups = export_tool.collect(
        db=db, media_dir=media, whitelist=[(phash, "approved-seed")], max_distance=2, limit=100
    )
    assert len(groups) == 2, (
        "Review each distinct image, not only one representative per matching seed"
    )


def test_export_must_not_delete_source_when_out_equals_media(sandbox):
    db, media, samples = sandbox
    source = media / "img-original.png"
    synthetic_image(source)
    synthetic_image(samples / "approved.png")
    before = source.read_bytes()
    decision(db, source.name)
    args = [
        "--db",
        str(db),
        "--media-dir",
        str(media),
        "--samples-dir",
        str(samples),
        "--out",
        str(media),
        "--max-distance",
        "2",
    ]
    try:
        export_tool.main(args)
    except (ValueError, SystemExit):
        pass  # Explicit refusal before any write is acceptable.
    assert source.is_file() and source.read_bytes() == before


def test_replay_readonly_control(sandbox):
    db, media, _ = sandbox
    path = media / "one.png"
    value = synthetic_image(path)
    decision(db, path.name)
    before = db.read_bytes(), path.read_bytes()
    result = replay_tool.replay(
        db=db, media_dir=media, whitelist=[(value, "approved-seed")], max_distance=2, limit=100
    )
    assert len(result["would_change"]) == 1
    assert before == (db.read_bytes(), path.read_bytes())
