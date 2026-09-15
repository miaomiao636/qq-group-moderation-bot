"""W2 工具链契约测试（S02/S05/S07/S08）：导出→盲标→回放→合并→聚合，全程合成数据。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

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


def test_s02_missing_truth_category_kept_as_unknown(tmp_path) -> None:
    """S02: 缺 truth_category 的样本保留进整体指标（category=unknown），
    不回退 other 混入类别指标，也不得丢弃缩小召回分母。"""
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
    # 两条都保留：整体召回分母不缩小
    assert sorted(r["sample_id"] for r in rows) == ["s1", "s2"]
    unknown_row = next(r for r in rows if r["sample_id"] == "s2")
    assert unknown_row["category"] == "unknown"
    # strict 模式仍拒绝未标注真值的数据集
    with pytest.raises(SystemExit):
        _merge(tmp_path, system, labels, strict=True)
    report = summarize_samples([EvaluationSample.from_dict(r) for r in rows])
    # 整体召回 = 2/2 全命中 = 1.0（unknown 样本参与整体指标）
    assert report["metrics"]["recall"] == 1.0
    # 类别细分：ad 只含 s1；unknown 单列无门槛
    assert report["by_category"]["ad"]["recall"] == 1.0
    assert "unknown" in report["by_category"]
    assert "ad_recall_90pct" in report["threshold_checks"]


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


def test_a02_degraded_samples_stay_in_end_to_end_denominator(tmp_path) -> None:
    """A02: 降级样本保留在端到端召回分母（按未自动识别计），不得删除缩分母。

    复现场景：40 条违规中 20 命中、20 因模型故障降级转人工；另有 60 条正常。
    端到端自动召回必须为 20/40=50%，而不是把 20 条降级删掉后的 100%。
    """
    system = [_system_row(f"h{i}", "violation_high") for i in range(20)]
    system += [_system_row(f"d{i}", "record_only", degraded=True) for i in range(20)]
    system += [_system_row(f"n{i}", "record_only") for i in range(60)]
    labels = [
        {
            "sample_id": f"h{i}",
            "label": "confirmed_violation",
            "truth_category": "ad",
            "kind": "text",
        }
        for i in range(20)
    ]
    labels += [
        {
            "sample_id": f"d{i}",
            "label": "confirmed_violation",
            "truth_category": "ad",
            "kind": "text",
        }
        for i in range(20)
    ]
    labels += [
        {
            "sample_id": f"n{i}",
            "label": "confirmed_normal",
            "truth_category": "other",
            "kind": "text",
        }
        for i in range(60)
    ]
    out = _merge(tmp_path, system, labels)
    rows = [EvaluationSample.from_dict(r) for r in _read(out)]
    assert len(rows) == 100  # 分母完整：降级样本没有被删除
    report = summarize_samples(rows)
    assert report["metrics"]["recall"] == 0.5
    assert report["metrics"]["unavailable_samples"] == 20
    assert report["metrics"]["positive_samples"] == 40
    assert report["threshold_checks"]["measurement_complete"] is False


def test_a01_predicted_categories_rejected_by_formal_aggregator() -> None:
    """A01: 回放默认输出（model_predicted）不得直接进入正式聚合器。"""
    row = {
        "sample_id": "s1",
        "label": "confirmed_violation",
        "verdict": "violation_high",
        "category": "ad",
        "category_source": "model_predicted",
        "kind": "text",
        "latency_ms": None,
        "latency_source": "none",
        "unavailable": "",
        "model_revision": "deepseek-flash@t204-v7",
        "rule_revision": "rules+abc123",
    }
    with pytest.raises(ValueError, match="model-predicted"):
        summarize_samples([EvaluationSample.from_dict(row)])
    # 合并人工真值后的输出必须被接受
    row["category_source"] = "manual_truth"
    report = summarize_samples([EvaluationSample.from_dict(row)])
    assert report["metrics"]["recall"] == 1.0


def test_a03_legacy_latency_without_source_is_not_guessed(tmp_path) -> None:
    """A03: 无来源声明的旧记录即使有数值也必须判未测，不得猜成端到端。"""
    system = [_system_row("s1", "violation_high")]
    system[0]["latency_ms"] = 10.0  # 旧记录：有数值但没有 latency_source 声明
    labels = [
        {
            "sample_id": "s1",
            "label": "confirmed_violation",
            "truth_category": "ad",
            "kind": "text",
        }
    ]
    out = _merge(tmp_path, system, labels)
    rows = [EvaluationSample.from_dict(r) for r in _read(out)]
    assert rows[0].latency_source == "none"
    assert rows[0].latency_ms is None


def test_a03_latency_gate_requires_coverage() -> None:
    """A03: 100 条只有 1 条有端到端测量 → 覆盖率不足，延迟门槛不得判通过。"""
    rows = []
    for i in range(100):
        measured = i == 0
        rows.append(
            EvaluationSample.from_dict(
                {
                    "sample_id": f"s{i}",
                    "label": "confirmed_normal",
                    "verdict": "allow",
                    "category": "other",
                    "category_source": "manual_truth",
                    "kind": "text",
                    "latency_ms": 10.0 if measured else None,
                    "latency_source": "inbox" if measured else "none",
                    "unavailable": "",
                    "model_revision": "m",
                    "rule_revision": "r",
                }
            )
        )
    report = summarize_samples(rows)
    assert report["metrics"]["latency_measured_samples"] == 1
    assert report["metrics"]["latency_missing_samples"] == 99
    assert report["metrics"]["latency_coverage"] == 0.01
    assert report["threshold_checks"]["text_latency"] is None


@pytest.mark.parametrize("system_filename", ["w2_debug.jsonl", "w2_samples.jsonl"])
async def test_r107_replay_outputs_keep_unavailable_through_formal_merge(
    tmp_path, monkeypatch, system_filename
) -> None:
    """Both real replay exports must preserve unavailable markers through merge/eval."""
    replay = _load_script("w2_replay")
    task_data = tmp_path / "data"
    task_data.mkdir()
    labels = [
        {
            "sample_id": f"s{i}",
            "label": "confirmed_violation" if i < 3 else "confirmed_normal",
            "truth_category": "ad",
            "kind": "text",
            "text": f"synthetic sample {i}",
            "media": [],
        }
        for i in range(10)
    ]
    _write(task_data / "w2_labels_all.jsonl", labels)

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class FakeService:
        vision_moderator = object()
        calls = 0

        async def review_message(self, session, msg, local, **kwargs):
            self.calls += 1
            if self.calls == 3:
                raise RuntimeError("synthetic unavailable")
            degraded = self.calls == 2
            verdict = (
                "violation_high" if self.calls == 1 else "record_only" if degraded else "allow"
            )
            category = "ad" if self.calls == 1 else None
            result = SimpleNamespace(
                source="text",
                review_role="primary",
                model_id="test-primary",
                category=category,
                confidence=0.95,
                needs_review=degraded,
                cache_hit=False,
                degraded_reason="provider_call_failed" if degraded else "",
            )
            return SimpleNamespace(verdict=verdict, category=category, confidence=0.95), [result]

    settings = SimpleNamespace(
        ai_vision_model="test-primary",
        ai_review_model="test-review",
        ai_prompt_version="test-v1",
        ai_prompt_rules_file="",
        ai_primary_direct_threshold=0.90,
        ai_secondary_review_low=0.60,
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["w2_replay.py"])
    monkeypatch.setattr(replay, "get_settings", lambda: settings)
    monkeypatch.setattr(replay, "SessionLocal", FakeSession)
    monkeypatch.setattr(replay, "build_default_ai_review_service", FakeService)
    await replay.main()

    manifest = json.loads((task_data / "w2_manifest.json").read_text(encoding="utf-8"))
    assert manifest["unusable"] == 2
    assert manifest["fail_rate"] == 0.2
    predicted = [EvaluationSample.from_dict(row) for row in _read(task_data / "w2_samples.jsonl")]
    with pytest.raises(ValueError, match="model-predicted"):
        summarize_samples(predicted)

    output = _merge(tmp_path, _read(task_data / system_filename), labels, strict=True)
    report = summarize_samples([EvaluationSample.from_dict(row) for row in _read(output)])
    assert report["metrics"]["samples"] == 10
    assert report["metrics"]["positive_samples"] == 3
    assert report["metrics"]["recall"] == pytest.approx(1 / 3)
    assert report["metrics"]["unavailable_samples"] == 2
    assert report["threshold_checks"]["measurement_complete"] is False


@pytest.mark.parametrize("unavailable", [None, True, "unknown_failure"])
def test_r107_merge_rejects_invalid_unavailable_markers(tmp_path, unavailable) -> None:
    """Malformed failure metadata cannot be silently converted into availability."""
    system = [{**_system_row("s1", "record_only"), "unavailable": unavailable}]
    labels = [
        {
            "sample_id": "s1",
            "label": "confirmed_violation",
            "truth_category": "ad",
            "kind": "text",
        }
    ]
    with pytest.raises(SystemExit, match="unavailable"):
        _merge(tmp_path, system, labels, strict=True)
