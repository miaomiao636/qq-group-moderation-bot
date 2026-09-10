"""离线验收统计：只读脱敏标签/判定 JSONL，不调用模型、QQ 或生产数据库。

用法：python -m app.reports.evaluation --input samples.jsonl --output report.json
输出只有聚合指标；数据真实来源、盲测独立性和 Windows 实测需人工另行核验。
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
    "kind",
    "latency_ms",
    "model_revision",
    "rule_revision",
}
_LABELS = {"confirmed_violation", "confirmed_normal", "false_positive"}
_VERDICTS = {"allow", "record_only", "violation_high"}
_KINDS = {"text", "image", "gif", "video", "audio", "file", "share_card", "mixed", "unknown"}
_CATEGORIES = {"ad", "fraud", "porn", "violence", "flood", "other"}


@dataclass(frozen=True)
class EvaluationSample:
    sample_id: str
    label: str
    verdict: str
    category: str
    kind: str
    latency_ms: float
    model_revision: str
    rule_revision: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> EvaluationSample:
        if set(value) != _FIELDS:
            raise ValueError("sample fields must match the documented metadata-only schema")
        for name in _FIELDS - {"latency_ms"}:
            if (
                not isinstance(value[name], str)
                or not value[name].strip()
                or len(value[name]) > 128
            ):
                raise ValueError(f"invalid {name}")
        for name, allowed in (
            ("label", _LABELS),
            ("verdict", _VERDICTS),
            ("kind", _KINDS),
            ("category", _CATEGORIES),
        ):
            if value[name] not in allowed:
                raise ValueError(f"invalid {name}")
        latency = value["latency_ms"]
        if type(latency) not in (int, float) or not math.isfinite(latency) or latency < 0:
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
    latency = sorted(r.latency_ms for r in rows)
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
    }


def summarize_samples(rows: list[EvaluationSample]) -> dict[str, Any]:
    if len({r.sample_id for r in rows}) != len(rows):
        raise ValueError("duplicate sample_id; duplicates cannot increase evidence counts")
    if len({(r.model_revision, r.rule_revision) for r in rows}) > 1:
        raise ValueError("mixed model/rule versions must be evaluated in separate reports")
    metrics = _metrics(rows)
    kinds = {
        kind: _metrics([r for r in rows if r.kind == kind])
        for kind in sorted({r.kind for r in rows})
    }
    categories = {
        category: _metrics([r for r in rows if r.category == category])
        for category in sorted({r.category for r in rows})
    }
    checks: dict[str, bool | None] = {
        "precision_99pct": metrics["precision"] >= 0.99
        if metrics["precision"] is not None
        else None,
        "recall_85pct": metrics["recall"] >= 0.85 if metrics["recall"] is not None else None,
    }
    for category in ("ad", "fraud"):
        recall = categories.get(category, {}).get("recall")
        checks[f"{category}_recall_90pct"] = recall >= 0.90 if recall is not None else None
    for kind, limit in {"text": 3000, "image": 15000, "gif": 15000, "video": 90000}.items():
        p95 = kinds.get(kind, {}).get("p95_latency_ms")
        checks[f"{kind}_latency"] = p95 <= limit if p95 is not None else None
    return {
        "schema_version": "r105-eval-v1",
        "metrics": metrics,
        "by_kind": kinds,
        "by_category": categories,
        "threshold_checks": checks,
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
            "人工复核(record_only)不计入自动处罚召回，单独报告发现召回。",
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
