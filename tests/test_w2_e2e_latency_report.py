"""R-107: latency evidence must use exact event identity and show exclusions."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from scripts import w2_e2e_latency_report as report


@pytest.fixture
def latency_db(tmp_path: Path) -> Path:
    path = tmp_path / "moderation.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE onebot_inbox (event_key TEXT PRIMARY KEY, self_id TEXT, "
            "status TEXT, created_at TEXT, updated_at TEXT);"
            "CREATE TABLE shadow_decisions (message_id TEXT UNIQUE, provider TEXT, "
            "external_message_id TEXT, kind TEXT);"
        )
        conn.executemany(
            "INSERT INTO onebot_inbox VALUES (?, ?, ?, ?, ?)",
            [
                ("onebot:111:42", "111", "DONE", "2026-09-11 10:00:00", "2026-09-11 10:00:02"),
                ("onebot:111:43", "111", "DONE", "2026-09-11 10:01:00", "2026-09-11 10:01:01"),
                ("onebot:111:44", "111", "PENDING", "2026-09-11 10:02:00", "2026-09-11 10:02:00"),
                ("onebot:111:45", "111", "DEAD", "2026-09-11 10:03:00", "2026-09-11 10:03:20"),
                ("onebot:111:46", "111", "DONE", "2026-09-11 10:04:00", "2026-09-11 10:03:59"),
            ],
        )
        conn.executemany(
            "INSERT INTO shadow_decisions VALUES (?, ?, ?, ?)",
            [
                ("onebot:111:42", "onebot", "42", "text"),
                ("onebot:222:42", "onebot", "42", "image"),
                ("42", "qq_official", "42", "video"),
                ("onebot:111:46", "onebot", "46", "text"),
            ],
        )
    return path


def _run(monkeypatch: pytest.MonkeyPatch, path: Path, out: Path) -> dict:
    monkeypatch.setattr(report, "DB", str(path))
    monkeypatch.setattr(
        sys, "argv", ["report", "--since", "2026-09-11 00:00:00", "--out", str(out)]
    )
    report.main()
    return json.loads(out.read_text(encoding="utf-8"))


def test_exact_event_key_prevents_cross_account_and_provider_matches(
    monkeypatch: pytest.MonkeyPatch, latency_db: Path, tmp_path: Path
) -> None:
    data = _run(monkeypatch, latency_db, tmp_path / "report.json")
    assert set(data["by_kind"]) == {"text"}
    assert data["by_kind"]["text"]["samples"] == 1
    assert data["by_kind"]["text"]["p95_ms"] == 2000


def test_report_exposes_incomplete_and_unmatched_population(
    monkeypatch: pytest.MonkeyPatch, latency_db: Path, tmp_path: Path
) -> None:
    data = _run(monkeypatch, latency_db, tmp_path / "report.json")
    assert data["coverage"]["status_counts"] == {"DEAD": 1, "DONE": 3, "PENDING": 1}
    assert data["coverage"]["done_without_decision"] == 1
    assert data["coverage"]["done_invalid_latency"] == 1
    assert data["coverage"]["done_with_decision"] == 2
    assert data["coverage"]["kind_measured_samples"] == 1


def test_missing_database_is_never_created(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "absent.db"
    with pytest.raises((SystemExit, sqlite3.Error)):
        _run(monkeypatch, path, tmp_path / "report.json")
    assert not path.exists()


def test_existing_evidence_is_not_overwritten(
    monkeypatch: pytest.MonkeyPatch, latency_db: Path, tmp_path: Path
) -> None:
    out = tmp_path / "report.json"
    out.write_text("original evidence", encoding="utf-8")
    with pytest.raises((SystemExit, FileExistsError)):
        _run(monkeypatch, latency_db, out)
    assert out.read_text(encoding="utf-8") == "original evidence"


def test_explicit_window_database_and_read_only_snapshot(
    monkeypatch: pytest.MonkeyPatch, latency_db: Path, tmp_path: Path
) -> None:
    out = tmp_path / "bounded.json"
    before = latency_db.read_bytes()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "report",
            "--database",
            str(latency_db),
            "--since",
            "2026-09-11T18:00:00+08:00",
            "--until",
            "2026-09-11T18:01:00+08:00",
            "--out",
            str(out),
        ],
    )
    monkeypatch.chdir(tmp_path)
    report.main()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["coverage"]["inbox_events"] == 1
    assert data["overall"]["samples"] == 1
    assert latency_db.read_bytes() == before


def test_empty_window_never_claims_pass(
    monkeypatch: pytest.MonkeyPatch, latency_db: Path, tmp_path: Path
) -> None:
    out = tmp_path / "empty.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "report",
            "--database",
            str(latency_db),
            "--since",
            "2027-01-01",
            "--until",
            "2027-01-02",
            "--out",
            str(out),
        ],
    )
    report.main()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["overall"] == {"samples": 0}
    assert data["by_kind"] == {}
    assert data["release_decision"] == "REQUIRES_HUMAN_REVIEW"


@pytest.mark.parametrize("since,until", [("invalid", "2026-01-01"), ("2026-01-02", "2026-01-01")])
def test_invalid_window_rejected_before_output(
    monkeypatch: pytest.MonkeyPatch, latency_db: Path, tmp_path: Path, since: str, until: str
) -> None:
    out = tmp_path / "invalid.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "report",
            "--database",
            str(latency_db),
            "--since",
            since,
            "--until",
            until,
            "--out",
            str(out),
        ],
    )
    with pytest.raises(SystemExit):
        report.main()
    assert not out.exists()
