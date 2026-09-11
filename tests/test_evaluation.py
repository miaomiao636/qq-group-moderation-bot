"""独立报告只计算已提供的人工真值，不宣称Windows或模型实测完成。"""

from __future__ import annotations

import pytest
from app.reports.evaluation import EvaluationSample, summarize_samples


def sample(
    identity: str, label: str, verdict: str, latency_ms: float | None = 10
) -> EvaluationSample:
    return EvaluationSample.from_dict(
        {
            "sample_id": identity,
            "label": label,
            "verdict": verdict,
            "category": "ad",
            "kind": "image",
            "latency_ms": latency_ms,
            "latency_source": "inbox" if latency_ms is not None else "none",
            "model_revision": "test-model",
            "rule_revision": "test-rule",
        }
    )


def test_confusion_and_review_queue_are_distinct() -> None:
    report = summarize_samples(
        [
            sample("a", "confirmed_violation", "violation_high"),
            sample("b", "confirmed_normal", "violation_high"),
            sample("c", "confirmed_violation", "record_only"),
            sample("d", "confirmed_normal", "allow", 20000),
        ]
    )
    assert report["metrics"]["precision"] == 0.5
    assert report["metrics"]["recall"] == 0.5
    assert report["metrics"]["flagged_recall"] == 1.0
    assert report["metrics"]["manual_review_rate"] == 0.25
    assert report["by_kind"]["image"]["p95_latency_ms"] == 20000
    assert report["threshold_checks"]["precision_99pct"] is False
    assert report["release_decision"] == "REQUIRES_HUMAN_REVIEW"


def test_empty_data_and_zero_predictions_do_not_pass() -> None:
    report = summarize_samples([])
    assert report["metrics"]["precision"] is None
    assert report["threshold_checks"]["precision_99pct"] is None
    assert report["limitations"]


def test_duplicate_samples_cannot_inflate_acceptance_counts() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        summarize_samples([sample("same", "confirmed_normal", "allow")] * 2)


def test_mixed_model_or_rule_versions_must_be_evaluated_separately() -> None:
    from dataclasses import replace

    first = sample("first", "confirmed_normal", "allow")
    with pytest.raises(ValueError, match="version"):
        summarize_samples([first, replace(first, sample_id="second", model_revision="new-model")])


@pytest.mark.parametrize("label", ["unknown_recall", "other_recall", "model_said_normal"])
def test_unknown_labels_are_not_truth(label: str) -> None:
    with pytest.raises(ValueError, match="label"):
        sample("a", label, "allow")


def test_raw_message_fields_rejected() -> None:
    with pytest.raises(ValueError, match="fields"):
        EvaluationSample.from_dict({"raw_content": "private material"})


@pytest.mark.parametrize("latency", [-1, float("nan"), float("inf"), True])
def test_invalid_latency_cannot_pollute_metrics(latency: float) -> None:
    with pytest.raises(ValueError, match="latency"):
        sample("a", "confirmed_normal", "allow", latency)
