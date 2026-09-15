"""R-112 N07 串联回归：抽样（标注）→ 转换 → 正式评测契约。

主审复现点：给 sample_draw 导出行填 label/truth_category 后，正式评测器
报 `ValueError: sample fields must match the documented metadata-only schema`。
本回归锁定转换器输出可直接通过 `EvaluationSample.from_dict` 校验。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

MOD_PATH = Path(__file__).resolve().parent.parent / "scripts" / "sample_to_eval.py"


def _load():
    spec = importlib.util.spec_from_file_location("sample_to_eval_r112", MOD_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row(
    mid: str,
    label: str = "",
    truth: str = "",
    verdict: str = "record_only",
    kind: str = "text",
) -> dict[str, object]:
    """模拟 sample_draw 导出 + 人工标注的行。"""
    return {
        "sample_id": mid.split(":")[-1],
        "message_id": mid,
        "provider": "onebot",
        "external_group_id": "123",
        "kind": kind,
        "system_verdict": verdict,
        "system_category": "ad",
        "system_confidence": 0.8,
        "reason": "",
        "text_preview": "",
        "media_kinds": [],
        "created_at": "2026-09-15 12:00:00",
        "label": label,
        "truth_category": truth,
    }


def test_convert_output_passes_evaluation_contract(tmp_path: Path) -> None:
    mod = _load()
    src = tmp_path / "sample.jsonl"
    rows = [
        _row("onebot:g:1", "confirmed_violation", "ad", "violation_high"),
        _row("onebot:g:2", "confirmed_normal", "other", "record_only"),
        _row("onebot:g:3"),  # 未标注：跳过
    ]
    src.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    dst = tmp_path / "eval.jsonl"
    stats = mod.convert_file(src, dst, model_revision="m1", rule_revision="r1")
    assert stats["converted"] == 2
    assert stats["skipped_unlabeled"] == 1
    assert stats["positives"] == 1 and stats["negatives"] == 1

    import app.reports.evaluation as ev

    loaded = [json.loads(line) for line in dst.read_text(encoding="utf-8").splitlines()]
    for item in loaded:
        ev.EvaluationSample.from_dict(item)  # 必须通过正式契约
    assert loaded[0]["category_source"] == "manual_truth"
    assert loaded[0]["latency_source"] == "none" and loaded[0]["latency_ms"] is None
    assert loaded[0]["model_revision"] == "m1" and loaded[0]["rule_revision"] == "r1"


def test_label_without_category_is_rejected() -> None:
    mod = _load()
    with pytest.raises(ValueError):
        mod.convert_row(
            _row("onebot:g:9", "confirmed_violation", ""),
            model_revision="m",
            rule_revision="r",
        )


def test_illegal_label_is_rejected() -> None:
    mod = _load()
    with pytest.raises(ValueError):
        mod.convert_row(
            _row("onebot:g:9", "maybe_bad", "ad"),
            model_revision="m",
            rule_revision="r",
        )


def test_unlabeled_row_returns_none_not_error() -> None:
    mod = _load()
    assert mod.convert_row(_row("onebot:g:9"), model_revision="m", rule_revision="r") is None


def test_forward_record_kind_maps_to_unknown() -> None:
    """评测契约无 forward_record；转换器按最保守映射 unknown（R-112 后实测发现）。"""
    mod = _load()
    out = mod.convert_row(
        _row("onebot:g:9", "confirmed_violation", "ad", kind="forward_record"),
        model_revision="m",
        rule_revision="r",
    )
    assert out is not None
    assert out["kind"] == "unknown"
