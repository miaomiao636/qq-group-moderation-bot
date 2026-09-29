# ruff: noqa: E402, I001, F401, F811, SIM105
# A2 REGISTERED ADAPTATION: original bytes are sealed under docs/evidence/authority-a2-20260922/legacy-probes/.
# Historical nodeids are retained; current contracts and every changed AST node are registered in docs/2026-09-22-authority-a2-adaptations.md.
# Reviewer round-8 probe pack (b7d7e78), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
"""b7d7e78: synthetic probes beyond builder-only R6-02 assertions."""

from __future__ import annotations

import sqlite3

import pytest
from PIL import Image

from app.moderation.image_hash import dhash64_file, hamming64, to_hex
from scripts import image_allowlist_replay as replay_tool
from scripts import image_allowlist_seed as seed_tool
from scripts import image_review_export as export_tool
from tests.test_r132_review_image_tool_set_consistency import (
    add_decision,
    make_image,
    sandbox,
)


def _collect(db, media, samples):
    return export_tool.collect(
        db=db,
        media_dir=media,
        whitelist=export_tool.build_whitelist(samples, db, media),
        max_distance=2,
        limit=100,
    )


def _report(db, media, samples, out):
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
                str(out),
            ]
        )
        == 0
    )
    paths = list(out.glob("batch-*/IMAGE_REVIEW.md"))
    assert len(paths) == 1
    return paths[0].read_text(encoding="utf-8")


def test_unmatched_candidate_not_counted_as_active_would_change(sandbox):
    db, media, samples, _ = sandbox
    make_image(media / "candidate.png")
    add_decision(db, "candidate.png")
    assert export_tool.build_whitelist(samples, db, media) == []
    groups = _collect(db, media, samples)
    assert len(groups) == 1
    entry = next(iter(groups.values()))
    assert entry["label"] == "未在生效名单（候选）"
    assert entry["would_change"] == 0, "Unmatched candidate cannot change an active decision."


@pytest.mark.parametrize("rejection", ["default_exclude", "custom_exclude", "disabled_row"])
def test_candidate_collection_preserves_explicit_rejection(sandbox, tmp_path, rejection):
    db, media, samples, default_exclusion = sandbox
    value = make_image(samples / "rejected.png")
    make_image(media / "rejected.png")
    add_decision(db, "rejected.png")
    if rejection == "default_exclude":
        default_exclusion.write_text(to_hex(value) + "\n", encoding="utf-8")
    elif rejection == "custom_exclude":
        custom = tmp_path / "explicit-rejection.txt"
        custom.write_text(to_hex(value) + "\n", encoding="utf-8")
        assert (
            seed_tool.main(
                [
                    "--db",
                    str(db),
                    "--samples-dir",
                    str(samples),
                    "--media-dir",
                    str(media),
                    "--exclude-file",
                    str(custom),
                ]
            )
            == 0
        )
    else:
        seed_tool.import_seeds(
            db=db,
            seeds=seed_tool.scan_samples(samples),
            dry_run=False,
            operator="synthetic",
        )
        from tests.authority_fixtures import decide

        for key in seed_tool.effective_hashes(db):
            decide(db, key, "rejected", "exclude")
    assert export_tool.build_whitelist(samples, db, media) == []
    assert _collect(db, media, samples) == {}, "Previously rejected image was reintroduced."


def test_candidate_report_header_matches_actual_rows(sandbox, tmp_path):
    db, media, samples, _ = sandbox
    make_image(media / "candidate.png")
    add_decision(db, "candidate.png")
    report = _report(db, media, samples, tmp_path / "output")
    assert "待审核图片：**1 张**" in report
    assert "生效名单为空且无候选" not in report


def test_candidate_report_renders_per_row_candidate_label(sandbox, tmp_path):
    db, media, samples, _ = sandbox
    make_image(media / "candidate.png")
    add_decision(db, "candidate.png")
    report = _report(db, media, samples, tmp_path / "output")
    item = next(line for line in report.splitlines() if line.startswith("| 01 |"))
    assert "候选" in item, "Internal entry label is discarded by Markdown rendering."


def test_nonempty_active_set_does_not_absorb_unmatched_candidate(sandbox):
    db, media, samples, _ = sandbox
    approved = make_image(samples / "approved.png")
    rejected = media / "unmatched.png"
    Image.new("L", (64, 64), 0).save(rejected)
    other = dhash64_file(rejected)
    assert other is not None and hamming64(approved, other) > 2
    seed_tool.import_seeds(
        db=db,
        seeds=seed_tool.scan_samples(samples),
        dry_run=False,
        operator="synthetic",
    )
    add_decision(db, rejected.name)
    assert len(export_tool.build_whitelist(samples, db, media)) == 1
    assert _collect(db, media, samples) == {}


def test_replay_candidate_report_discloses_unavailable_active_set(sandbox, tmp_path):
    db, media, samples, _ = sandbox
    make_image(samples / "sample.png")
    make_image(media / "candidate.png")
    add_decision(db, "candidate.png")
    with sqlite3.connect(db) as con:
        con.execute("DROP TABLE image_allowlist")
    out = tmp_path / "replay"
    assert (
        replay_tool.main(
            [
                "--db",
                str(db),
                "--media-dir",
                str(media),
                "--samples-dir",
                str(samples),
                "--max-distance",
                "2",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    reports = list(out.glob("*.md"))
    assert len(reports) == 1
    report = reports[0].read_text(encoding="utf-8")
    assert "missing_table" in report and "不是生效评估" in report, (
        "Replay exposes candidate source only inside a row, not the unavailable active state."
    )
