# ruff: noqa: E402, I001, F401, F811
# Reviewer round-6 probe pack (85b0c0b), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
"""85b0c0b review probes: synthetic-only import/replay/export contracts.

Run from the reviewed checkout with PYTHONPATH=.:tests and pytest -p conftest.
These assertions are desired safe contracts, not patches to production code.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from app.moderation.image_hash import dhash64_file, to_hex
from scripts import image_allowlist_replay as replay_tool
from scripts import image_allowlist_seed as seed_tool
from scripts import image_review_export as export_tool


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    db = tmp_path / "synthetic.db"
    media = tmp_path / "media"
    samples = tmp_path / "samples"
    media.mkdir()
    samples.mkdir()
    default_exclusions = tmp_path / "default-exclude.txt"
    default_exclusions.write_text("# synthetic empty exclusions\n", encoding="utf-8")
    monkeypatch.setattr(replay_tool, "EXCLUDE_FILE", default_exclusions)
    monkeypatch.setattr(export_tool, "EXCLUDE_FILE", default_exclusions)
    with sqlite3.connect(db) as con:
        con.executescript(
            "CREATE TABLE shadow_decisions (message_id TEXT, created_at TEXT, kind TEXT, "
            "verdict TEXT, category TEXT, external_group_id TEXT, detail_json TEXT);"
            "CREATE TABLE image_allowlist (id INTEGER PRIMARY KEY, phash TEXT NOT NULL UNIQUE, "
            "note TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', "
            "enabled INTEGER NOT NULL DEFAULT 1, hit_count INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT '');"
        )
    return db, media, samples, default_exclusions


def make_image(path: Path) -> int:
    image = Image.new("L", (64, 64))
    for x in range(64):
        for y in range(64):
            image.putpixel((x, y), (x * 4 + y * 3) % 256)
    image.save(path)
    value = dhash64_file(path)
    assert value is not None
    return value


def add_decision(db: Path, name: str, *, verdict: str = "record_only") -> None:
    detail = {"media_files": [{"name": name}], "rule_hits": [], "ai_results": []}
    if verdict == "allow":
        detail["ai_results"] = [{"source": "vision", "has_miniprogram_code": True}]
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO shadow_decisions VALUES (?,?,?,?,?,?,?)",
            (name, "2026-09-19 00:00:00", "image", verdict, "ad", "synthetic", json.dumps(detail)),
        )


def enabled_hashes(db: Path) -> set[str]:
    with sqlite3.connect(db) as con:
        return {row[0] for row in con.execute("SELECT phash FROM image_allowlist WHERE enabled=1")}


def tool_hashes(builder, samples, db, media) -> set[str]:
    return {to_hex(value) for value, _source in builder(samples, db, media)}


@pytest.mark.parametrize("builder", [replay_tool.load_whitelist, export_tool.build_whitelist])
def test_default_import_does_not_authorize_history_only_images(sandbox, builder):
    db, media, samples, exclusions = sandbox
    make_image(media / "history-only.png")
    add_decision(db, "history-only.png", verdict="allow")
    assert (
        seed_tool.main(
            [
                "--db",
                str(db),
                "--media-dir",
                str(media),
                "--samples-dir",
                str(samples),
                "--exclude-file",
                str(exclusions),
            ]
        )
        == 0
    )
    assert enabled_hashes(db) == set(), (
        "No --from-history: default import intentionally adds nothing"
    )
    assert tool_hashes(builder, samples, db, media) == enabled_hashes(db)


@pytest.mark.parametrize("builder", [replay_tool.load_whitelist, export_tool.build_whitelist])
def test_manual_disable_is_respected_by_offline_active_set(sandbox, builder):
    db, media, samples, _ = sandbox
    value = make_image(samples / "approved.png")
    seed_tool.import_seeds(
        db=db, seeds=seed_tool.scan_samples(samples), dry_run=False, operator="review"
    )
    assert enabled_hashes(db) == {to_hex(value)}
    with sqlite3.connect(db) as con:
        con.execute("UPDATE image_allowlist SET enabled=0 WHERE phash=?", (to_hex(value),))
    assert tool_hashes(builder, samples, db, media) == enabled_hashes(db)


@pytest.mark.parametrize("builder", [replay_tool.load_whitelist, export_tool.build_whitelist])
def test_custom_exclusion_used_for_import_is_respected_in_same_set(sandbox, tmp_path, builder):
    db, media, samples, _ = sandbox
    value = make_image(samples / "explicitly-rejected.png")
    custom = tmp_path / "reviewer-exclusions.txt"
    custom.write_text(to_hex(value) + "\n", encoding="utf-8")
    assert (
        seed_tool.main(
            [
                "--db",
                str(db),
                "--media-dir",
                str(media),
                "--samples-dir",
                str(samples),
                "--exclude-file",
                str(custom),
            ]
        )
        == 0
    )
    assert enabled_hashes(db) == set()
    assert tool_hashes(builder, samples, db, media) == enabled_hashes(db)


def test_excluded_existing_row_absent_from_current_seeds_is_disabled(sandbox):
    db, media, _, _ = sandbox
    path = media / "old.png"
    value = make_image(path)
    seed_tool.import_seeds(
        db=db, seeds=[(path, "sample", "synthetic")], dry_run=False, operator="review"
    )
    assert enabled_hashes(db) == {to_hex(value)}
    seed_tool.import_seeds(
        db=db, seeds=[], dry_run=False, operator="review", excluded={to_hex(value)}
    )
    assert enabled_hashes(db) == set()


def test_exclusion_dry_run_does_not_disable_existing_row(sandbox):
    db, media, _, _ = sandbox
    path = media / "old.png"
    value = make_image(path)
    seed_tool.import_seeds(
        db=db, seeds=[(path, "sample", "synthetic")], dry_run=False, operator="review"
    )
    before = db.read_bytes()
    seed_tool.import_seeds(
        db=db, seeds=[], dry_run=True, operator="review", excluded={to_hex(value)}
    )
    assert db.read_bytes() == before
    assert enabled_hashes(db) == {to_hex(value)}


def test_export_copy_failure_is_nonzero_and_preserves_source(
    sandbox, tmp_path, monkeypatch, capsys
):
    db, media, samples, _ = sandbox
    source = media / "source.png"
    make_image(source)
    make_image(samples / "approved.png")
    add_decision(db, source.name)
    before = source.read_bytes()

    def fail_copy(*_args, **_kwargs):
        raise PermissionError("synthetic permission denied")

    monkeypatch.setattr(export_tool.shutil, "copy2", fail_copy)
    result = export_tool.main(
        [
            "--db",
            str(db),
            "--media-dir",
            str(media),
            "--samples-dir",
            str(samples),
            "--out",
            str(tmp_path / "output"),
        ]
    )
    output = capsys.readouterr().out
    assert result != 0
    assert "REVIEW_EXPORT_FAILED" in output and "REVIEW_EXPORT_OK" not in output
    assert source.read_bytes() == before


def test_export_success_preserves_existing_output_and_source(sandbox, tmp_path):
    db, media, samples, _ = sandbox
    source = media / "source.png"
    make_image(source)
    make_image(samples / "approved.png")
    add_decision(db, source.name)
    output = tmp_path / "output"
    output.mkdir()
    earlier = output / "img-old-synthetic.png"
    earlier.write_bytes(b"synthetic previous review artifact")
    before = source.read_bytes(), earlier.read_bytes()
    assert (
        export_tool.main(
            [
                "--db",
                str(db),
                "--media-dir",
                str(media),
                "--samples-dir",
                str(samples),
                "--out",
                str(output),
            ]
        )
        == 0
    )
    assert (source.read_bytes(), earlier.read_bytes()) == before
    assert len(list(output.glob("batch-*/IMAGE_REVIEW.md"))) == 1
