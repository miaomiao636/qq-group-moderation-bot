"""Internal regressions: every report must use a single SQLite read snapshot."""

import sqlite3
from datetime import UTC, datetime

import pytest
from scripts import shadow_report, window_stats

START = datetime(2026, 9, 21, tzinfo=UTC)
END = datetime(2026, 9, 22, tzinfo=UTC)


def database(tmp_path):
    path = tmp_path / "synthetic.db"
    with sqlite3.connect(path) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
        CREATE TABLE provider_group_settings(provider TEXT, external_group_id TEXT, action_enabled INTEGER);
        INSERT INTO provider_group_settings VALUES('onebot', 'group-test', 1);
        CREATE TABLE shadow_decisions(created_at TEXT, verdict TEXT, kind TEXT,
          provider TEXT, external_group_id TEXT, external_message_id TEXT,
          message_id TEXT, detail_json TEXT);
        CREATE TABLE action_intents(created_at TEXT, status TEXT);
        CREATE TABLE action_logs(created_at TEXT, action TEXT, ok INTEGER, err_code INTEGER,
          attempts INTEGER, provider TEXT, external_group_id TEXT, message_id TEXT);
        """)
    return path


def insert_decision(connect, path):
    with connect(path) as con:
        con.execute(
            "INSERT INTO shadow_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-09-21 12:00:00",
                "record_only",
                "image",
                "onebot",
                "group-test",
                "message-test",
                "onebot:10000001:message-test",
                '{"image_hash":{"mode":"shadow","checked":1,"matched":false}}',
            ),
        )


def install_concurrent_writer(monkeypatch, path, trigger):
    connect = sqlite3.connect
    readers = []

    class Reader(sqlite3.Connection):
        fired = False

        def execute(self, sql, *args, **kwargs):
            if trigger in sql.lower() and not self.fired:
                self.fired = True
                insert_decision(connect, path)
            return super().execute(sql, *args, **kwargs)

    def wrapped(*args, **kwargs):
        con = connect(*args, **kwargs, factory=Reader)
        readers.append(con)
        return con

    monkeypatch.setattr(sqlite3, "connect", wrapped)
    return readers


def test_window_report_keeps_one_snapshot_and_closes(tmp_path, monkeypatch):
    path = database(tmp_path)
    (tmp_path / ".env").write_text("ONEBOT_SELF_ID=10000001\n", encoding="utf-8")
    monkeypatch.setattr(window_stats, "ROOT", tmp_path)
    readers = install_concurrent_writer(monkeypatch, path, "select verdict")
    result = window_stats.collect(db=path, start=START, end=END)
    assert result["decisions_by_verdict"] == [], "Concurrent commit leaked into existing snapshot"
    assert result["per_group"] == []
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        readers[0].execute("SELECT 1")


def test_shadow_report_counts_and_rows_share_snapshot(tmp_path, monkeypatch):
    path = database(tmp_path)
    output = tmp_path / "reports"
    monkeypatch.setattr(shadow_report, "OUT_DIR", output)
    readers = install_concurrent_writer(monkeypatch, path, "select count(*)")
    assert (
        shadow_report.main(
            ["--db", str(path), "--since", START.isoformat(), "--until", END.isoformat()]
        )
        == 0
    )
    report = next(output.glob("*.md")).read_text(encoding="utf-8")
    assert "窗口内判定 0 条" in report, "Counts include a commit absent from observation rows"
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        readers[0].execute("SELECT 1")


def test_window_connection_closes_after_query_error(tmp_path, monkeypatch):
    path = database(tmp_path)
    with sqlite3.connect(path) as con:
        con.execute("DROP TABLE action_intents")
    (tmp_path / ".env").write_text("ONEBOT_SELF_ID=10000001\n", encoding="utf-8")
    monkeypatch.setattr(window_stats, "ROOT", tmp_path)
    readers = install_concurrent_writer(monkeypatch, path, "__never__")
    with pytest.raises(sqlite3.OperationalError, match="action_intents"):
        window_stats.collect(db=path, start=START, end=END)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        readers[0].execute("SELECT 1")


def test_window_output_does_not_overwrite_evidence(tmp_path, monkeypatch):
    path = database(tmp_path)
    (tmp_path / ".env").write_text("ONEBOT_SELF_ID=10000001\n", encoding="utf-8")
    monkeypatch.setattr(window_stats, "ROOT", tmp_path)
    output = tmp_path / "reports"
    output.mkdir()
    report = output / "window-20260921T000000Z-20260922T000000Z.md"
    report.write_text("SEALED_EXISTING_EVIDENCE", encoding="utf-8")
    with pytest.raises(FileExistsError):
        window_stats.main(
            [
                "--db",
                str(path),
                "--start",
                START.isoformat(),
                "--end",
                END.isoformat(),
                "--out",
                str(output),
                "--prompt-version",
                "synthetic",
            ]
        )
    assert report.read_text(encoding="utf-8") == "SEALED_EXISTING_EVIDENCE"


def test_shadow_output_does_not_overwrite_evidence(tmp_path, monkeypatch):
    path = database(tmp_path)
    output = tmp_path / "reports"
    output.mkdir()

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return START

    monkeypatch.setattr(shadow_report, "datetime", FrozenDatetime)
    monkeypatch.setattr(shadow_report, "OUT_DIR", output)
    report = output / "shadow-20260921T000000Z.md"
    report.write_text("SEALED_EXISTING_EVIDENCE", encoding="utf-8")
    with pytest.raises(FileExistsError):
        shadow_report.main(["--db", str(path)])
    assert report.read_text(encoding="utf-8") == "SEALED_EXISTING_EVIDENCE"


def test_window_naive_timestamp_means_utc():
    assert window_stats._parse_utc("2026-09-21 12:00:00") == datetime(2026, 9, 21, 12, tzinfo=UTC)


@pytest.mark.parametrize("end", [START, START.replace(year=2025)])
def test_window_rejects_empty_or_reversed_range(tmp_path, end):
    with pytest.raises(ValueError, match="start"):
        window_stats.collect(db=tmp_path / "not-opened.db", start=START, end=end)
