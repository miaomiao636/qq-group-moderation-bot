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
        "unavailable": "",
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


# ---------- R-113 F02/F03/F07 回归 ----------


def test_missing_unavailable_is_rejected() -> None:
    """F02：缺 unavailable 字段直接拒绝——未知不得默认可用。"""
    mod = _load()
    row = _row("onebot:g:9", "confirmed_violation", "ad")
    del row["unavailable"]
    with pytest.raises(ValueError):
        mod.convert_row(row, model_revision="m", rule_revision="r")


def test_degraded_availability_passthrough() -> None:
    """F02：已知降级状态透传到正式契约字段。"""
    mod = _load()
    row = _row("onebot:g:9", "confirmed_violation", "ad")
    row["unavailable"] = "degraded"
    out = mod.convert_row(row, model_revision="m", rule_revision="r")
    assert out is not None and out["unavailable"] == "degraded"


def test_voice_kind_maps_to_audio() -> None:
    """F07：parser 产出 voice、契约枚举 audio。"""
    mod = _load()
    out = mod.convert_row(
        _row("onebot:g:9", "confirmed_violation", "ad", kind="voice"),
        model_revision="m",
        rule_revision="r",
    )
    assert out is not None and out["kind"] == "audio"


def test_real_parser_voice_flows_to_converter_and_contract() -> None:
    """F07 串联：真实 OneBot record fixture → parser → 转换器 → 正式契约。"""
    from app.adapters.onebot.parser import OneBotMessageSource

    fixture = Path(__file__).parent / "fixtures" / "onebot" / "group_message_voice.json"
    event = json.loads(fixture.read_text(encoding="utf-8"))["event"]
    msg = OneBotMessageSource().parse_group_message(event)
    assert msg.kind == "voice"

    import app.reports.evaluation as ev

    mod = _load()
    row = {
        "sample_id": msg.external_message_id,
        "message_id": msg.message_id,
        "kind": msg.kind,
        "system_verdict": "record_only",
        "label": "confirmed_normal",
        "truth_category": "other",
        "unavailable": "",
    }
    out = mod.convert_row(row, model_revision="m", rule_revision="r")
    assert out is not None
    ev.EvaluationSample.from_dict(out)
    assert out["kind"] == "audio"


def test_same_source_output_is_rejected(tmp_path: Path) -> None:
    """F03：输入输出同源拒绝（含路径别名）。"""
    mod = _load()
    src = tmp_path / "a.jsonl"
    src.write_text(
        json.dumps(_row("onebot:g:1", "confirmed_normal", "other"), ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        mod.convert_file(src, src, model_revision="m", rule_revision="r")


def test_existing_output_rejected_without_force_and_bytes_preserved(tmp_path: Path) -> None:
    """F03：已存在输出默认拒绝且不破坏已有字节；force=True 显式覆盖。"""
    mod = _load()
    src = tmp_path / "a.jsonl"
    src.write_text(
        json.dumps(_row("onebot:g:1", "confirmed_normal", "other"), ensure_ascii=False),
        encoding="utf-8",
    )
    dst = tmp_path / "b.jsonl"
    dst.write_text("keep-me", encoding="utf-8")
    with pytest.raises(ValueError):
        mod.convert_file(src, dst, model_revision="m", rule_revision="r")
    assert dst.read_text(encoding="utf-8") == "keep-me"

    stats = mod.convert_file(src, dst, model_revision="m", rule_revision="r", force=True)
    assert stats["converted"] == 1
