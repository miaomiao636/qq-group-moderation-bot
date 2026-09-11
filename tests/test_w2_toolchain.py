"""W2 工具链契约测试（S02/S05/S07/S08）：导出→盲标→回放→合并→聚合，全程合成数据。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from app.reports.evaluation import EvaluationSample, summarize_samples

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )
    return path


def _read(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _system_row(sid: str, verdict: str, *, degraded: bool = False) -> dict:
    return {
        "sample_id": sid,
        "label": "confirmed_violation",
        "truth_category": "ad",
        "verdict": verdict,
        "predicted_category": "ad" if verdict == "violation_high" else "other",
        "confidence": 0.95,
        "kind": "text",
        "latency_ai_ms": 1234,
        "error_kind": "",
        "degraded": degraded,
        "cached": False,
        "model_revision": "deepseek-flash@t204-v7",
        "rule_revision": "rules+abc123",
        "results": [],
    }


def _merge(tmp_path: Path, system_rows: list[dict], labels_rows: list[dict], strict=False) -> Path:
    module = _load_script("w2_sample_merge")
    labels = _write(tmp_path / "labels.jsonl", labels_rows)
    system = _write(tmp_path / "system.jsonl", system_rows)
    out = tmp_path / "samples.jsonl"
    argv = [
        "w2_sample_merge",
        "--labels",
        str(labels),
        "--system",
        str(system),
        "--out",
        str(out),
    ]
    if strict:
        argv.append("--strict")
    old_argv = sys.argv
    sys.argv = argv
    try:
        module.main()
    finally:
        sys.argv = old_argv
    return out


def test_s02_missed_ad_counts_in_ad_recall_not_other(tmp_path) -> None:
    """S02 复现: 两条人工确认广告、系统命中一条漏一条 → 整体召回 50%，广告召回 50%。"""
    system = [
        _system_row("s1", "violation_high"),
        _system_row("s2", "record_only"),
    ]
    labels = [
        {
            "sample_id": "s1",
            "label": "confirmed_violation",
            "truth_category": "ad",
            "kind": "text",
        },
        {
            "sample_id": "s2",
            "label": "confirmed_violation",
            "truth_category": "ad",
            "kind": "text",
        },
    ]
    out = _merge(tmp_path, system, labels)
    rows = [EvaluationSample.from_dict(r) for r in _read(out)]
    report = summarize_samples(rows)
    assert report["metrics"]["recall"] == 0.5
    # 漏判的广告必须留在 ad 组的分母里，而不是被移动到 other
    assert report["by_category"]["ad"]["recall"] == 0.5
    assert "other" not in report["by_category"]
    assert report["threshold_checks"]["ad_recall_90pct"] is False


def test_s02_missing_truth_category_is_excluded_not_other(tmp_path) -> None:
    """S02: 缺 truth_category 的样本被排除，而不是回退 other 混入类别指标。"""
    system = [_system_row("s1", "violation_high"), _system_row("s2", "violation_high")]
    labels = [
        {
            "sample_id": "s1",
            "label": "confirmed_violation",
            "truth_category": "ad",
            "kind": "text",
        },
        {"sample_id": "s2", "label": "confirmed_violation", "truth_category": "", "kind": "text"},
    ]
    out = _merge(tmp_path, system, labels)
    rows = _read(out)
    assert [r["sample_id"] for r in rows] == ["s1"]
    with pytest.raises(SystemExit):
        _merge(tmp_path, system, labels, strict=True)


def test_s07_replay_latency_is_none_and_thresholds_unmeasured(tmp_path) -> None:
    """S07: 回放无端到端证据 → latency_ms=null、延迟门槛为未测（None），不得按 AI 耗时算过。"""
    system = [_system_row("s1", "violation_high")]
    labels = [
        {
            "sample_id": "s1",
            "label": "confirmed_violation",
            "truth_category": "ad",
            "kind": "text",
        }
    ]
    # 合入端到端字段缺失的输入（回放口径）
    for row in system:
        row["latency_ms"] = None
        row["latency_source"] = "none"
    out = _merge(tmp_path, system, labels)
    rows = [EvaluationSample.from_dict(r) for r in _read(out)]
    assert rows[0].latency_ms is None
    assert rows[0].latency_source == "none"
    report = summarize_samples(rows)
    assert report["metrics"]["p95_latency_ms"] is None
    assert report["threshold_checks"]["text_latency"] is None


def test_s07_end_to_end_latency_requires_inbox_source(tmp_path) -> None:
    """S07: 只有 latency_source=inbox 的端到端证据才能参与门槛，且被保留。"""
    system = [_system_row("s1", "violation_high")]
    labels = [
        {
            "sample_id": "s1",
            "label": "confirmed_violation",
            "truth_category": "ad",
            "kind": "text",
        }
    ]
    system[0]["latency_ms"] = 2500.0
    system[0]["latency_source"] = "inbox"
    out = _merge(tmp_path, system, labels)
    rows = [EvaluationSample.from_dict(r) for r in _read(out)]
    report = summarize_samples(rows)
    assert report["metrics"]["p95_latency_ms"] == 2500.0
    assert report["threshold_checks"]["text_latency"] is True
    # 非法组合被拒: source=none 却带数值
    with pytest.raises(ValueError):
        EvaluationSample.from_dict(
            {**rows[0].__dict__, "latency_source": "none", "latency_ms": 1.0}
        )


def test_s05_legacy_debug_without_revision_is_rejected(tmp_path) -> None:
    """S05: 旧版 debug（无 model_revision/rule_revision）必须报错，不得静默混拼。"""
    system = [_system_row("s1", "violation_high")]
    del system[0]["model_revision"]
    labels = [
        {
            "sample_id": "s1",
            "label": "confirmed_violation",
            "truth_category": "ad",
            "kind": "text",
        }
    ]
    with pytest.raises(SystemExit):
        _merge(tmp_path, system, labels)


def test_s08_degraded_samples_excluded_from_eval(tmp_path) -> None:
    """S08: 供应商降级的样本不得进入评测（既不算漏判也不算放行）。"""
    system = [
        _system_row("s1", "violation_high"),
        _system_row("s2", "record_only", degraded=True),
    ]
    labels = [
        {
            "sample_id": "s1",
            "label": "confirmed_violation",
            "truth_category": "ad",
            "kind": "text",
        },
        {
            "sample_id": "s2",
            "label": "confirmed_violation",
            "truth_category": "ad",
            "kind": "text",
        },
    ]
    out = _merge(tmp_path, system, labels)
    assert [r["sample_id"] for r in _read(out)] == ["s1"]
