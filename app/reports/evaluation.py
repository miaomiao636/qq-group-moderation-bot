"""离线验收统计：只读脱敏标签/判定 JSONL，不调用模型、QQ 或生产数据库。

用法：python -m app.reports.evaluation --input samples.jsonl --output report.json
输出只有聚合指标；数据真实来源、盲测独立性和 Windows 实测需人工另行核验。

契约 v2（主审 A01–A03 整改）：
- A01：category 必须来自独立人工真值（category_source=manual_truth）；
  模型预测类别的回放产物被正式聚合器明确拒绝，必须先经人工合并。
- A02：降级/异常样本不得从分母消失——unavailable 样本保留在端到端自动召回
  分母中（其真实判定为未自动识别/转人工），并单独报告数量与完整性门槛。
- A03：延迟来源不做推断；门槛在端到端覆盖不足时显示为未测（None），
  报告输出按类型/整体的延迟有效样本数与缺失数。
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FIELDS = {
    "sample_id",
    "label",
    "verdict",
    "category",
    "category_source",
    "kind",
    "latency_ms",
    "latency_source",
    "unavailable",
    "model_revision",
    "rule_revision",
}
_LABELS = {"confirmed_violation", "confirmed_normal", "false_positive"}
_VERDICTS = {"allow", "record_only", "violation_high"}
_KINDS = {"text", "image", "gif", "video", "audio", "file", "share_card", "mixed", "unknown"}
_CATEGORIES = {"ad", "fraud", "porn", "violence", "flood", "other"}
_LATENCY_SOURCES = {"inbox", "none"}
_CATEGORY_SOURCES = {"manual_truth", "model_predicted"}
_UNAVAILABLE = {"", "degraded", "error"}
# A03：端到端延迟门槛的可判通过最低覆盖（未测占比过高时不得判通过）
_LATENCY_COVERAGE_REQUIRED = 0.95
# A02：不可用样本（降级/异常）占比超过该值时，测量判为不完整
_UNAVAILABLE_RATIO_MAX = 0.05


@dataclass(frozen=True)
class EvaluationSample:
    sample_id: str
    label: str
    verdict: str
    category: str
    category_source: str
    kind: str
    latency_ms: float | None
    latency_source: str
    unavailable: str
    model_revision: str
    rule_revision: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> EvaluationSample:
        if set(value) != _FIELDS:
            raise ValueError("sample fields must match the documented metadata-only schema")
        for name in _FIELDS - {"latency_ms", "unavailable"}:
            if (
                not isinstance(value[name], str)
                or not value[name].strip()
                or len(value[name]) > 128
            ):
                raise ValueError(f"invalid {name}")
        if not isinstance(value["unavailable"], str) or len(value["unavailable"]) > 16:
            raise ValueError("invalid unavailable")
        for name, allowed in (
            ("label", _LABELS),
            ("verdict", _VERDICTS),
            ("kind", _KINDS),
            ("category", _CATEGORIES),
            ("category_source", _CATEGORY_SOURCES),
            ("latency_source", _LATENCY_SOURCES),
            ("unavailable", _UNAVAILABLE),
        ):
            if value[name] not in allowed:
                raise ValueError(f"invalid {name}")
        # A02：不可用样本的真实判定只能是"未自动识别/转人工"，不得伪装为处罚或放行
        if value["unavailable"] and value["verdict"] != "record_only":
            raise ValueError("unavailable samples must carry verdict=record_only")
        latency = value["latency_ms"]
        if value["latency_source"] == "none":
            # 未测就是未测：不允许用 AI 子链耗时或 0 冒充端到端延迟（R07/S07）。
            if latency is not None:
                raise ValueError("latency_ms must be null when latency_source is none")
        elif type(latency) not in (int, float) or not math.isfinite(latency) or latency < 0:
            raise ValueError("invalid latency_ms")
        return cls(**value)


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _metrics(rows: list[EvaluationSample]) -> dict[str, Any]:
    positives = [r for r in rows if r.label == "confirmed_violation"]
    tp = sum(r.verdict == "violation_high" for r in positives)
    fp = sum(r.label != "confirmed_violation" and r.verdict == "violation_high" for r in rows)
    fn = len(positives) - tp
    tn = len(rows) - tp - fp - fn
    latency = sorted(r.latency_ms for r in rows if r.latency_ms is not None)
    return {
        "samples": len(rows),
        "positive_samples": len(positives),
        "negative_samples": len(rows) - len(positives),
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "true_negative": tn,
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(tp, tp + fn),
        "false_positive_rate": _ratio(fp, fp + tn),
        "flagged_recall": _ratio(sum(r.verdict != "allow" for r in positives), len(positives)),
        "manual_review_rate": _ratio(sum(r.verdict == "record_only" for r in rows), len(rows)),
        "p95_latency_ms": latency[math.ceil(len(latency) * 0.95) - 1] if latency else None,
        # A03：延迟有效样本/缺失数与覆盖率（不能只看"存在一个数值"）
        "latency_measured_samples": len(latency),
        "latency_missing_samples": len(rows) - len(latency),
        "latency_coverage": _ratio(len(latency), len(rows)),
        # A02：供应商故障/回放异常导致的不可用样本数（保留在分母内）
        "unavailable_samples": sum(1 for r in rows if r.unavailable),
    }


def summarize_samples(rows: list[EvaluationSample]) -> dict[str, Any]:
    if len({r.sample_id for r in rows}) != len(rows):
        raise ValueError("duplicate sample_id; duplicates cannot increase evidence counts")
    if len({(r.model_revision, r.rule_revision) for r in rows}) > 1:
        raise ValueError("mixed model/rule versions must be evaluated in separate reports")
    # A01：正式聚合器只接受人工真值类别——回放默认输出（模型预测类别）必须先合并。
    if any(r.category_source != "manual_truth" for r in rows):
        raise ValueError(
            "model-predicted categories cannot enter formal aggregation; "
            "merge with manual truth first"
        )
    metrics = _metrics(rows)
    kinds = {
        kind: _metrics([r for r in rows if r.kind == kind])
        for kind in sorted({r.kind for r in rows})
    }
    categories = {
        category: _metrics([r for r in rows if r.category == category])
        for category in sorted({r.category for r in rows})
    }
    unavailable_ratio = _ratio(metrics["unavailable_samples"], metrics["samples"])
    checks: dict[str, bool | None] = {
        "precision_99pct": metrics["precision"] >= 0.99
        if metrics["precision"] is not None
        else None,
        "recall_85pct": metrics["recall"] >= 0.85 if metrics["recall"] is not None else None,
        "measurement_complete": (
            (unavailable_ratio or 0.0) <= _UNAVAILABLE_RATIO_MAX if rows else None
        ),
    }
    for category in ("ad", "fraud"):
        recall = categories.get(category, {}).get("recall")
        checks[f"{category}_recall_90pct"] = recall >= 0.90 if recall is not None else None
    for kind, limit in {"text": 3000, "image": 15000, "gif": 15000, "video": 90000}.items():
        data = kinds.get(kind, {})
        p95 = data.get("p95_latency_ms")
        coverage = data.get("latency_coverage")
        # A03：门槛三态——超限=False；未测或覆盖不足=未测(None)；达标且覆盖足够=True
        if p95 is None:
            checks[f"{kind}_latency"] = None
        elif p95 > limit:
            checks[f"{kind}_latency"] = False
        elif coverage is None or coverage < _LATENCY_COVERAGE_REQUIRED:
            checks[f"{kind}_latency"] = None
        else:
            checks[f"{kind}_latency"] = True
    return {
        "schema_version": "r105-eval-v2",
        "metrics": metrics,
        "by_kind": kinds,
        "by_category": categories,
        "threshold_checks": checks,
        "latency_coverage_required": _LATENCY_COVERAGE_REQUIRED,
        "unavailable_ratio_max": _UNAVAILABLE_RATIO_MAX,
        "model_revisions": sorted({r.model_revision for r in rows}),
        "rule_revisions": sorted({r.rule_revision for r in rows}),
        "sample_coverage": {
            name: {
                "positive_20": data["positive_samples"] >= 20,
                "negative_30": data["negative_samples"] >= 30,
            }
            for name, data in categories.items()
        },
        "release_decision": "REQUIRES_HUMAN_REVIEW",
        "limitations": [
            "人工真值及留出集独立性必须另行核验；重复模板应按近重复族隔离。",
            "端到端自动召回包含降级/异常样本（unavailable，按未自动识别计）；"
            "供应商成功响应条件下的模型质量需另列子集报告。",
            "人工复核(record_only)不计入自动处罚召回，单独报告发现召回。",
            "延迟门槛仅在端到端测量覆盖达标时可判通过；未测显示为 null。",
            "百分比是样本点估计，小样本不能证明长期99%精确率。",
            "不证明真实消息接收、动作执行、Windows恢复或通知到达。",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("output must not overwrite input")
    rows = []
    with args.input.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("sample must be an object")
                rows.append(EvaluationSample.from_dict(value))
            except (ValueError, TypeError) as exc:
                # 错误只给行号，不回显可能含原始内容的输入。
                parser.error(f"invalid metadata sample at line {number}: {type(exc).__name__}")
    try:
        report = summarize_samples(rows)
    except ValueError as exc:
        parser.error(str(exc))
    # 不覆盖已有取证报告；重新运行必须显式选择另一个输出文件。
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"Aggregated {len(rows)} labeled samples; human acceptance is still required.")


if __name__ == "__main__":
    main()
