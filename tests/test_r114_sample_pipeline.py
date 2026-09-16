"""R-114 F02/F03 回归：抽样状态恢复与独占输出（主审关闭标准）。

F02：五类反例（缺 ai_results / 损坏 JSON / parse_error / null / [{}]）不得默认可用；
     显式 [] 与正常/降级元素 → 对应值；转换器拒绝非法类型（null/布尔/数字/容器）。
F03：输入撞临时名、既有他人临时文件、目标竞争、失败保留字节。
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_DRAW = _ROOT / "scripts" / "sample_draw.py"
_TO_EVAL = _ROOT / "scripts" / "sample_to_eval.py"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- F02：sample_draw 状态恢复 ----------


@pytest.mark.parametrize(
    "raw",
    [
        None,  # 详情 NULL
        "",  # 空详情
        "{not-json",  # 损坏 JSON
        "[]",  # 非对象
        ["parse_error"],  # 非字符串
        '{"parse_error": "boom"}',  # pipeline 解析失败记录
        "{}",  # 缺 ai_results
        '{"ai_results": null}',  # null
        '{"ai_results": 3}',  # 非列表
        '{"ai_results": [{}]}',  # 空对象元素
        '{"ai_results": ["x"]}',  # 非对象元素
        # R-114 F02 残余：degraded_reason 必须为字符串（缺失/非字符串均不可默认可用）
        '{"ai_results": [{"model_id": "m"}]}',  # 缺 degraded_reason
        '{"ai_results": [{"model_id": "m", "degraded_reason": null}]}',
        '{"ai_results": [{"model_id": "m", "degraded_reason": false}]}',
        '{"ai_results": [{"model_id": "m", "degraded_reason": 0}]}',
        '{"ai_results": [{"model_id": "m", "degraded_reason": []}]}',
        '{"ai_results": [{"model_id": "m", "degraded_reason": {}}]}',
    ],
)
def test_unavailable_from_detail_rejects_unknown_or_broken(raw: object) -> None:
    draw = _load(_DRAW, "sample_draw_r114_reject")
    assert draw.unavailable_from_detail(raw) == "error"


def test_unavailable_from_detail_legal_cases() -> None:
    draw = _load(_DRAW, "sample_draw_r114_legal")
    # 未调用 AI：显式空列表 = 正常
    assert draw.unavailable_from_detail('{"ai_results": []}') == ""
    assert draw.unavailable_from_detail("") == "error"
    assert (
        draw.unavailable_from_detail('{"ai_results": [{"model_id": "m", "degraded_reason": ""}]}')
        == ""
    )
    assert (
        draw.unavailable_from_detail(
            '{"ai_results": [{"model_id": "m", "degraded_reason": "timeout"}]}'
        )
        == "degraded"
    )
    # 清理后（purged）形态：元素保留字段、degraded_reason 语义占位 → 仍可恢复为降级
    assert (
        draw.unavailable_from_detail(
            '{"purged": true, "ai_results": [{"raw_response_sha256": "x",'
            ' "degraded_reason": "raw_retention_expired_degraded"}]}'
        )
        == "degraded"
    )


# ---------- F02：sample_to_eval 严格校验 ----------


def _row(mid: str = "onebot:g:1", **over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "sample_id": mid.split(":")[-1],
        "message_id": mid,
        "provider": "onebot",
        "external_group_id": "123",
        "kind": "text",
        "system_verdict": "record_only",
        "system_category": "ad",
        "system_confidence": 0.8,
        "reason": "",
        "text_preview": "",
        "media_kinds": [],
        "unavailable": "",
        "created_at": "2026-09-15 12:00:00",
        "label": "confirmed_violation",
        "truth_category": "ad",
    }
    row.update(over)
    return row


@pytest.mark.parametrize("bad", [None, False, 0, [], {}, True, 1.5])
def test_convert_row_rejects_non_enum_unavailable(bad: object) -> None:
    mod = _load(_TO_EVAL, "sample_to_eval_r114_reject")
    with pytest.raises(ValueError):
        mod.convert_row(_row(unavailable=bad), model_revision="m", rule_revision="r")


def test_convert_row_accepts_legal_enum_values() -> None:
    mod = _load(_TO_EVAL, "sample_to_eval_r114_accept")
    for value in ("", "degraded", "error"):
        out = mod.convert_row(_row(unavailable=value), model_revision="m", rule_revision="r")
        assert out is not None and out["unavailable"] == value


# ---------- F03：独占输出 ----------


def _labeled_line(mid: str) -> str:
    return json.dumps(
        {
            "sample_id": mid.split(":")[-1],
            "message_id": mid,
            "provider": "onebot",
            "external_group_id": "123",
            "kind": "text",
            "system_verdict": "record_only",
            "system_category": "ad",
            "system_confidence": 0.8,
            "reason": "",
            "text_preview": "",
            "media_kinds": [],
            "unavailable": "",
            "created_at": "2026-09-15 12:00:00",
            "label": "confirmed_violation",
            "truth_category": "ad",
        },
        ensure_ascii=False,
    )


def test_input_named_like_tmp_is_never_replaced(tmp_path: Path) -> None:
    """输入本身就叫 eval.jsonl.tmp（旧实现会截断并移走它）。"""
    mod = _load(_TO_EVAL, "sample_to_eval_r114_f03a")
    src = tmp_path / "eval.jsonl.tmp"
    src.write_text(_labeled_line("onebot:g:1") + "\n", encoding="utf-8")
    before = src.read_bytes()
    dst = tmp_path / "eval.jsonl"
    mod.convert_file(src, dst, model_revision="m", rule_revision="r")
    assert src.exists() and src.read_bytes() == before
    assert dst.exists()


def test_preexisting_tmp_file_is_not_touched(tmp_path: Path) -> None:
    """目录里其他操作的 .tmp 文件不被当作本次临时文件。"""
    mod = _load(_TO_EVAL, "sample_to_eval_r114_f03b")
    src = tmp_path / "in.jsonl"
    src.write_text(_labeled_line("onebot:g:1") + "\n", encoding="utf-8")
    other = tmp_path / "out.jsonl.tmp"
    other.write_text("someone-elses-tmp", encoding="utf-8")
    mod.convert_file(src, tmp_path / "out.jsonl", model_revision="m", rule_revision="r")
    assert other.read_text(encoding="utf-8") == "someone-elses-tmp"


def test_target_race_is_rejected_and_bytes_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exists 检查后、发布前被他人创建目标：默认拒绝且不覆盖（原子独占发布）。"""
    mod = _load(_TO_EVAL, "sample_to_eval_r114_f03c")
    src = tmp_path / "in.jsonl"
    src.write_text(_labeled_line("onebot:g:1") + "\n", encoding="utf-8")
    dst = tmp_path / "out.jsonl"
    real_link = os.link

    def racy_link(src_path: object, dst_path: object) -> None:
        dst.write_text("raced-in", encoding="utf-8")  # 竞争者在检查之后创建目标
        real_link(src_path, dst_path)

    monkeypatch.setattr(os, "link", racy_link)
    with pytest.raises(ValueError):
        mod.convert_file(src, dst, model_revision="m", rule_revision="r")
    assert dst.read_text(encoding="utf-8") == "raced-in"


def test_publish_failure_preserves_existing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """force 替换时底层发布失败：已有目标字节保留、本次临时文件被清理。"""
    mod = _load(_TO_EVAL, "sample_to_eval_r114_f03d")
    src = tmp_path / "in.jsonl"
    src.write_text(_labeled_line("onebot:g:1") + "\n", encoding="utf-8")
    dst = tmp_path / "out.jsonl"
    dst.write_text("keep-me", encoding="utf-8")

    def broken_replace(a: object, b: object) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        mod.convert_file(src, dst, model_revision="m", rule_revision="r", force=True)
    assert dst.read_text(encoding="utf-8") == "keep-me"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith("out.jsonl.")]
    assert leftovers == []
