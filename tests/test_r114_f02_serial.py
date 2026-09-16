"""R-114 F02 串联回归：SQLite → 真实抽样入口 → 转换 → 正式聚合（主审探针转正）。

主审探针 ``qqbot-r114-f02-probe.py`` 正式化（其"helper 单测通过 ≠ 整个抽样入口可用"
的验收点保留）：仅使用临时 SQLite 与合成文件，不读取生产数据库。
本测试覆盖：六种非法 AI 降级字段、四种非对象详情（不得 AttributeError）、
五个合法控制（空列表 / 正常 / 降级 / purged 正常 / purged 降级）。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load_script(name: str) -> ModuleType:
    path = _ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"qqbot_r114_f02_serial_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _draw_and_evaluate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_detail: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    draw = _load_script("sample_draw")
    converter = _load_script("sample_to_eval")

    # 在调用 main 前替换所有输入/输出路径。此数据库仅含一条合成记录，
    # 不复制、不打开任何真实 data/moderation.db，也不启动应用或执行迁移。
    synthetic_db = tmp_path / "synthetic.db"
    output_dir = tmp_path / "drawn"
    monkeypatch.setattr(draw, "PROD_DB", synthetic_db)
    monkeypatch.setattr(draw, "OUT_DIR", output_dir)
    with sqlite3.connect(synthetic_db) as connection:
        connection.execute(
            "CREATE TABLE shadow_decisions ("
            "message_id TEXT, provider TEXT, external_group_id TEXT, member_openid TEXT, "
            "kind TEXT, verdict TEXT, category TEXT, confidence REAL, reason TEXT, "
            "detail_json TEXT, created_at TEXT)"
        )
        connection.execute(
            "INSERT INTO shadow_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "onebot:10000001:1",
                "onebot",
                "900000001",
                "800000001",
                "text",
                "record_only",
                "ad",
                0.5,
                "synthetic probe only",
                raw_detail,
                "2026-09-15 01:00:00",
            ),
        )

    monkeypatch.setattr(
        sys,
        "argv",
        ["sample_draw", "--since", "2026-09-15", "--count", "1"],
    )
    # 不捕获 AttributeError：若实际抽样入口崩溃，应由 pytest 明确判失败。
    assert draw.main() == 0
    outputs = list(output_dir.glob("*.jsonl"))
    assert len(outputs) == 1
    lines = outputs[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])

    # 仅补人工真值；绝不在测试中修补 unavailable、kind 或系统判定。
    row.update(label="confirmed_violation", truth_category="ad")
    converted = converter.convert_row(
        row,
        model_revision="synthetic-model",
        rule_revision="synthetic-rule",
    )
    assert converted is not None

    from app.reports.evaluation import EvaluationSample, summarize_samples

    report = summarize_samples([EvaluationSample.from_dict(converted)])
    return converted, report


def _assert_result(
    converted: dict[str, Any], report: dict[str, Any], expected_unavailable: str
) -> None:
    assert converted["unavailable"] == expected_unavailable
    assert converted["verdict"] == "record_only"
    assert converted["category_source"] == "manual_truth"
    assert report["metrics"]["samples"] == 1
    assert report["metrics"]["positive_samples"] == 1
    assert report["metrics"]["false_negative"] == 1
    assert report["metrics"]["unavailable_samples"] == int(bool(expected_unavailable))
    assert report["threshold_checks"]["measurement_complete"] is (not bool(expected_unavailable))


@pytest.mark.parametrize(
    "ai_result",
    [
        {"model_id": "synthetic-model"},
        {"model_id": "synthetic-model", "degraded_reason": None},
        {"model_id": "synthetic-model", "degraded_reason": False},
        {"model_id": "synthetic-model", "degraded_reason": 0},
        {"model_id": "synthetic-model", "degraded_reason": []},
        {"model_id": "synthetic-model", "degraded_reason": {}},
    ],
    ids=["missing", "null", "false", "zero", "list", "object"],
)
def test_bad_ai_state_is_error_in_real_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ai_result: dict[str, Any]
) -> None:
    raw = json.dumps({"ai_results": [ai_result]})
    converted, report = _draw_and_evaluate(tmp_path, monkeypatch, raw)
    _assert_result(converted, report, "error")


@pytest.mark.parametrize(
    "raw_detail",
    ["[]", "null", "12", '"scalar"'],
    ids=["list", "null", "number", "string"],
)
def test_nonobject_detail_is_error_not_attribute_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw_detail: str
) -> None:
    converted, report = _draw_and_evaluate(tmp_path, monkeypatch, raw_detail)
    _assert_result(converted, report, "error")


@pytest.mark.parametrize(
    ("detail", "expected_unavailable"),
    [
        ({"ai_results": []}, ""),
        ({"ai_results": [{"model_id": "synthetic-model", "degraded_reason": ""}]}, ""),
        (
            {"ai_results": [{"model_id": "synthetic-model", "degraded_reason": "timeout"}]},
            "degraded",
        ),
        (
            {
                "purged": True,
                "ai_results": [{"raw_response_sha256": "0" * 64, "degraded_reason": ""}],
            },
            "",
        ),
        (
            {
                "purged": True,
                "ai_results": [
                    {
                        "raw_response_sha256": "0" * 64,
                        "degraded_reason": "raw_retention_expired_degraded",
                    }
                ],
            },
            "degraded",
        ),
    ],
    ids=["empty-ai-list", "normal-ai", "degraded-ai", "purged-normal", "purged-degraded"],
)
def test_valid_controls_keep_their_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    detail: dict[str, Any],
    expected_unavailable: str,
) -> None:
    converted, report = _draw_and_evaluate(tmp_path, monkeypatch, json.dumps(detail))
    _assert_result(converted, report, expected_unavailable)
