# ruff: noqa: E402, I001, F401, F811, SIM105
# Reviewer round-8 probe pack (8299ce8), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
"""Synthetic evidence-report checks; no production DB, media, API, or actions."""

import json
import sqlite3

from scripts import shadow_report


def render_report(tmp_path, monkeypatch, observations):
    db = tmp_path / "synthetic-shadow.db"
    with sqlite3.connect(db) as con:
        con.execute(
            "CREATE TABLE shadow_decisions (created_at TEXT, verdict TEXT, "
            "external_group_id TEXT, detail_json TEXT, message_id TEXT, kind TEXT)"
        )
        # An old pre-shadow record must not become a shadow-period denominator.
        con.execute(
            "INSERT INTO shadow_decisions VALUES (?, ?, ?, ?, ?, ?)",
            ("2000-01-01 00:00:00", "allow", "synthetic-old", "{}", "old", "image"),
        )
        for index, observation in enumerate(observations):
            con.execute(
                "INSERT INTO shadow_decisions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"2026-09-19 13:40:{index:02d}",
                    "record_only",
                    "synthetic-group",
                    json.dumps({"image_hash": observation}),
                    f"synthetic-{index}",
                    "image",
                ),
            )
    before = db.read_bytes()
    output = tmp_path / "reports"
    monkeypatch.setattr(shadow_report, "OUT_DIR", output)
    assert shadow_report.main(["--db", str(db), "--limit", "20"]) == 0
    assert db.read_bytes() == before
    return next(output.glob("shadow-*.md")).read_text(encoding="utf-8")


def test_unbounded_history_is_not_presented_as_shadow_window_denominator(tmp_path, monkeypatch):
    report = render_report(
        tmp_path,
        monkeypatch,
        [{"mode": "shadow", "checked": 1, "matched": False}],
    )
    assert "有 `image_hash` 观察的判定**：1 条" in report
    # Either implement explicit time boundaries, or truthfully label this all-retained-data.
    assert "窗口内判定总数 2" not in report, report


def test_unavailable_and_observer_error_are_retained_and_counted(tmp_path, monkeypatch):
    report = render_report(
        tmp_path,
        monkeypatch,
        [
            {"mode": "shadow", "checked": 1, "matched": False},
            {"mode": "shadow", "checked": 1, "matched": False, "unavailable": "db_failed"},
            {"mode": "shadow", "unavailable": "observer_error"},
        ],
    )
    assert "有 `image_hash` 观察的判定**：3 条" in report
    assert "标记 unavailable：2 条" in report
    assert "db_failed" in report and "observer_error" in report
    assert "命中（matched）**：0 条" in report


def test_matched_with_unavailable_preserves_both_facts_not_proven_allow(tmp_path, monkeypatch):
    report = render_report(
        tmp_path,
        monkeypatch,
        [
            {
                "mode": "shadow",
                "checked": 2,
                "matched": True,
                "distance": 1,
                "would_allow": True,
                "unavailable": "read_failed",
            }
        ],
    )
    assert "标记 unavailable：1 条" in report and "read_failed" in report
    assert "would_allow**：1 条" in report
    # The script presently only aggregates raw observations; enforce does not exist.
    assert "会进入 enforce 放行" not in report, report


def test_first_frame_scope_reaches_actual_report(tmp_path, monkeypatch):
    report = render_report(
        tmp_path,
        monkeypatch,
        [
            {
                "mode": "shadow",
                "checked": 1,
                "matched": True,
                "distance": 0,
                "would_allow": True,
                "frame_scope": "first_frame",
            }
        ],
    )
    assert "首帧" in report or "first_frame" in report, report
