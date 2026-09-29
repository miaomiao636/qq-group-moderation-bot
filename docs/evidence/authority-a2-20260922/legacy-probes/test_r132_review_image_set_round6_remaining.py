# ruff: noqa: E402, I001, F401, F811, SIM105
# Reviewer round-7 probe pack (2ff2a8a), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
"""Synthetic-only follow-up probes for 2ff2a8a collection state contracts.

No production database, genuine image, external service, or application edits.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.moderation.image_hash import to_hex
from scripts import image_allowlist_replay as replay_tool
from scripts import image_allowlist_seed as seed_tool
from scripts import image_review_export as export_tool
from tests.test_r132_review_image_tool_set_consistency import (
    add_decision,
    enabled_hashes,
    make_image,
    sandbox,
    tool_hashes,
)


@pytest.mark.parametrize("custom_excluded", [False, True])
def test_export_uses_active_set_when_referenced_sample_was_not_imported(
    sandbox, tmp_path, custom_excluded
):
    db, media, samples, _ = sandbox
    value = make_image(samples / "sample.png")
    make_image(media / "observed.png")
    add_decision(db, "observed.png")
    if custom_excluded:
        custom = tmp_path / "human-rejected.txt"
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
    assert tool_hashes(replay_tool.load_whitelist, samples, db, media) == set()
    # Default active-set evidence must not silently become a candidate-set review.
    # In the second case the owner explicitly rejected this very hash at import.
    assert tool_hashes(export_tool.build_whitelist, samples, db, media) == set()


@pytest.mark.parametrize("shape", ["missing_table", "missing_enabled", "missing_phash"])
def test_unreadable_set_is_explicitly_unavailable_not_empty(sandbox, shape):
    db, _, _, _ = sandbox
    with sqlite3.connect(db) as con:
        con.execute("DROP TABLE image_allowlist")
        if shape == "missing_enabled":
            con.execute("CREATE TABLE image_allowlist (phash TEXT)")
        elif shape == "missing_phash":
            con.execute("CREATE TABLE image_allowlist (enabled INTEGER)")
    assert seed_tool.effective_hashes(db) is None


@pytest.mark.parametrize("shape", ["missing_table", "missing_enabled", "missing_phash"])
def test_candidate_export_does_not_represent_read_failure_as_ready_for_enforce(
    sandbox, tmp_path, shape
):
    db, media, samples, _ = sandbox
    make_image(samples / "sample.png")
    make_image(media / "observed.png")
    add_decision(db, "observed.png")
    with sqlite3.connect(db) as con:
        con.execute("DROP TABLE image_allowlist")
        if shape == "missing_enabled":
            con.execute("CREATE TABLE image_allowlist (phash TEXT)")
        elif shape == "missing_phash":
            con.execute("CREATE TABLE image_allowlist (enabled INTEGER)")
    assert seed_tool.effective_hashes(db) is None
    out = tmp_path / "review"
    result = export_tool.main(
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
    assert result == 0  # Candidate-only export may succeed if clearly labelled.
    reports = list(out.glob("batch-*/IMAGE_REVIEW.md"))
    assert len(reports) == 1
    report = reports[0].read_text(encoding="utf-8")
    assert "候选" in report and "全部确认后即可切 enforce" not in report, (
        "Unavailable active set was silently exported as a normal whitelist approval; "
        "candidate source labels are discarded by the report renderer."
    )
