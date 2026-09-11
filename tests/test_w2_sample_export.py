"""独立 W2 导出契约：版本为显式声明、仅完成事件提供端到端延迟。"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def exporter():
    path = Path(__file__).resolve().parents[1] / "scripts" / "w2_sample_export.py"
    spec = importlib.util.spec_from_file_location("w2_sample_export", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "revisions",
    [
        [],
        ["--model-revision", "frozen-model"],
        ["--rule-revision", "frozen-rules"],
        ["--model-revision", "   ", "--rule-revision", "frozen-rules"],
        ["--model-revision", "frozen-model", "--rule-revision", ""],
        ["--model-revision", "x" * 129, "--rule-revision", "frozen-rules"],
    ],
)
def test_revisions_must_be_explicit_valid_declarations(exporter, monkeypatch, revisions):
    def forbidden_connect(_path):
        pytest.fail("版本声明校验必须在访问数据库前完成")

    monkeypatch.setattr(exporter, "sqlite3", SimpleNamespace(connect=forbidden_connect))
    monkeypatch.setattr(sys, "argv", ["export", "--since", "2026-01-01 00:00:00", *revisions])
    with pytest.raises(SystemExit) as exc:
        exporter.main()
    assert exc.value.code == 2


def _argv(out: Path) -> list[str]:
    return [
        "export",
        "--since",
        "2026-01-01 00:00:00",
        "--out",
        str(out),
        "--model-revision",
        "frozen-model@prompt-test",
        "--rule-revision",
        "frozen-rules-test",
    ]


def test_missing_database_is_not_created(exporter, monkeypatch, tmp_path):
    (tmp_path / "data").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", _argv(tmp_path / "candidates"))
    with pytest.raises(sqlite3.OperationalError):
        exporter.main()
    assert not (tmp_path / "data" / "moderation.db").exists()


@pytest.mark.parametrize("suffix", ["labeling.jsonl", "system.jsonl", "manifest.json"])
def test_existing_outputs_are_not_overwritten(exporter, monkeypatch, tmp_path, suffix):
    out = tmp_path / "candidates"
    existing = Path(f"{out}-{suffix}")
    existing.write_text("synthetic existing human work", encoding="utf-8")

    def forbidden_connect(*_args, **_kwargs):
        pytest.fail("输出覆盖检查必须在访问数据库前完成")

    monkeypatch.setattr(exporter, "sqlite3", SimpleNamespace(connect=forbidden_connect))
    monkeypatch.setattr(sys, "argv", _argv(out))
    with pytest.raises(SystemExit, match="输出文件已存在"):
        exporter.main()
    assert existing.read_text(encoding="utf-8") == "synthetic existing human work"
    assert list(tmp_path.iterdir()) == [existing]


@pytest.fixture
def export_synthetic(exporter, monkeypatch, tmp_path):
    """仅在内存 SQLite 中模拟导出所需三张表，不连接任何应用数据库。"""
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE shadow_decisions (
            id INTEGER PRIMARY KEY, provider TEXT, external_group_id TEXT,
            external_message_id TEXT, message_id TEXT, kind TEXT, verdict TEXT,
            category TEXT, confidence REAL, detail_json TEXT, created_at TEXT
        );
        CREATE TABLE onebot_inbox (
            event_key TEXT, created_at TEXT, updated_at TEXT, status TEXT
        );
        CREATE TABLE ai_usage_logs (
            id INTEGER PRIMARY KEY, message_id TEXT, latency_ms REAL
        );
        """
    )
    connection.execute(
        "INSERT INTO shadow_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            1,
            "onebot",
            "synthetic-group",
            "synthetic-message",
            "synthetic-event",
            "text",
            "allow",
            "other",
            0.8,
            json.dumps({"text_preview": "synthetic safe content"}),
            "2026-01-02 00:00:00",
        ),
    )

    def connect(path, **kwargs):
        assert path == "file:data/moderation.db?mode=ro"
        assert kwargs == {"uri": True}
        return connection

    monkeypatch.setattr(exporter, "sqlite3", SimpleNamespace(connect=connect))
    out = tmp_path / "candidates"
    monkeypatch.setattr(sys, "argv", _argv(out))

    def run(status="DONE", *, ai_latency=None, degraded_reason=""):
        connection.execute(
            "INSERT INTO onebot_inbox VALUES (?, ?, ?, ?)",
            ("synthetic-event", "2026-01-02 00:00:00", "2026-01-02 00:00:01", status),
        )
        if ai_latency is not None:
            connection.execute(
                "INSERT INTO ai_usage_logs VALUES (?, ?, ?)", (1, "synthetic-message", ai_latency)
            )
        if degraded_reason:
            connection.execute(
                "UPDATE shadow_decisions SET verdict = ?, detail_json = ?",
                (
                    "record_only",
                    json.dumps(
                        {
                            "text_preview": "synthetic safe content",
                            "ai_results": [
                                {"category": "other", "degraded_reason": degraded_reason}
                            ],
                        }
                    ),
                ),
            )
        exporter.main()
        return {
            suffix: json.loads(Path(f"{out}-{suffix}.{extension}").read_text(encoding="utf-8"))
            for suffix, extension in [
                ("labeling", "jsonl"),
                ("system", "jsonl"),
                ("manifest", "json"),
            ]
        }

    yield run
    connection.close()


def test_export_marks_declared_versions_and_keeps_blind_truth_empty(export_synthetic):
    result = export_synthetic()
    assert result["system"]["model_revision"] == "frozen-model@prompt-test"
    assert result["system"]["rule_revision"] == "frozen-rules-test"
    assert result["manifest"]["model_revision"] == "frozen-model@prompt-test"
    assert result["manifest"]["rule_revision"] == "frozen-rules-test"
    assert result["manifest"]["revision_source"] == "operator_declared"
    assert result["labeling"]["label"] == ""
    assert result["labeling"]["truth_category"] == ""
    assert "category" not in result["labeling"]
    assert "verdict" not in result["labeling"]


@pytest.mark.parametrize("status", ["PENDING", "PROCESSING", "DEAD"])
@pytest.mark.parametrize("ai_latency", [None, 10])
def test_unfinished_inbox_is_not_end_to_end_latency(export_synthetic, status, ai_latency):
    result = export_synthetic(status, ai_latency=ai_latency)
    assert result["system"]["latency_ms"] is None
    assert result["system"]["latency_ai_ms"] == ai_latency
    expected_source = "missing" if ai_latency is None else "ai_fallback"
    assert result["system"]["latency_source"] == expected_source
    assert result["manifest"]["latency_src"] == {expected_source: 1}


def test_done_inbox_keeps_end_to_end_latency_separate_from_ai(export_synthetic):
    result = export_synthetic("DONE", ai_latency=10)
    assert result["system"]["latency_ms"] == 1000
    assert result["system"]["latency_ai_ms"] == 10
    assert result["system"]["latency_source"] == "inbox"


@pytest.mark.parametrize("reason", ["", "synthetic_timeout", "raw_retention_expired_degraded"])
def test_shadow_export_keeps_degraded_marker_without_raw_reason(export_synthetic, reason):
    result = export_synthetic(degraded_reason=reason)
    assert result["system"]["unavailable"] == ("degraded" if reason else "")
    if reason:
        assert result["system"]["verdict"] == "record_only"
        assert reason not in json.dumps(result["system"])
