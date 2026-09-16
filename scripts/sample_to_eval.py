#!/usr/bin/env python3
"""把人工标注后的线上抽样 JSONL 转换为正式评测输入（R-112 N07）。

背景：`scripts/sample_draw.py` 导出的样本带展示字段（system_* / text_preview /
media_kinds 等），与 `app.reports.evaluation.EvaluationSample` 的 11 字段
"metadata-only" 契约不同——填完 label/truth_category 直接喂评测器会被
`ValueError: sample fields must match the documented metadata-only schema`
拒绝。本脚本做显式转换，**逐条经正式评测契约校验**，输出可直接评测。

用法：
  python scripts/sample_to_eval.py \\
      --input  data/sample_pool/sample_20260915_1532.jsonl \\
      --output data/sample_pool/eval_20260915.jsonl \\
      --model-revision qwen3.8-flash --rule-revision t204-v13

契约与限制（R-112 N07 主审要求）：
- label 只来自人工；truth_category 只来自人工——写了 label 却没类别时直接报错；
- 未标注行（label 为空）跳过并计数报告，不静默、不凭空补历史真值；
- verdict = 系统判定（system_verdict）；延迟未测：latency_ms=null 且
  latency_source="none"（不得用 0 或 AI 子链耗时冒充端到端延迟）；
- unavailable **必须由上游（sample_draw）从存储详情恢复后给出**（R-113 F02：
  缺字段直接拒绝——未知不得默认可用）；已知降级为 "degraded"，正常为 ""；
- model_revision / rule_revision 由参数给定（判定窗口内的实际版本）。
  期间换过模型/提示词时须按消息时间分段转换，**不得把当前版本回填给历史样本**；
- 输出**独占创建**（R-113 F03）：拒绝与输入同源、拒绝覆盖已存在文件；
  确需重跑用显式 --force；写入先落临时文件再原子替换，出错不破坏已有字节。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.reports.evaluation import EvaluationSample  # noqa: E402

_LABELS = {"confirmed_violation", "confirmed_normal", "false_positive"}
# 评测契约 kind 枚举不含的样本类型 → 最保守映射（仅影响记录口径，不影响判定指标）
_KIND_MAP = {
    "forward_record": "unknown",
    "voice": "audio",
}  # R-113 F07：parser 产 voice、契约枚举为 audio


def convert_row(
    row: dict[str, object], *, model_revision: str, rule_revision: str
) -> dict[str, object] | None:
    """单行转换：未标注（label 空）返回 None；已标但不完整/非法抛 ValueError。"""
    label = str(row.get("label") or "").strip()
    if not label:
        return None
    if label not in _LABELS:
        raise ValueError(f"非法人工标签 {label!r}（sample_id={row.get('sample_id')}）")
    truth = str(row.get("truth_category") or "").strip()
    if not truth:
        raise ValueError(
            f"缺少人工 truth_category——不得用系统类别顶替（sample_id={row.get('sample_id')}）"
        )
    if "unavailable" not in row:
        raise ValueError(
            f"缺少 unavailable 字段——须先由 sample_draw 从存储详情恢复降级状态，"
            f"未知不得默认可用（sample_id={row.get('sample_id')}）"
        )
    fields: dict[str, object] = {
        "sample_id": str(row.get("message_id") or row.get("sample_id") or ""),
        "label": label,
        "verdict": str(row.get("system_verdict") or "").strip(),
        "category": truth,
        "category_source": "manual_truth",
        "kind": _KIND_MAP.get(str(row.get("kind") or ""), str(row.get("kind") or "")),
        "latency_ms": None,
        "latency_source": "none",
        "unavailable": str(row.get("unavailable") or ""),
        "model_revision": model_revision,
        "rule_revision": rule_revision,
    }
    EvaluationSample.from_dict(fields)  # 逐条过正式契约校验
    return fields


def convert_file(
    src: Path, dst: Path, *, model_revision: str, rule_revision: str, force: bool = False
) -> dict[str, int]:
    src_resolved = src.resolve()
    dst_resolved = dst.resolve()
    if dst_resolved == src_resolved or (
        dst_resolved.exists()
        and src_resolved.exists()
        and os.path.samefile(src_resolved, dst_resolved)
    ):
        raise ValueError("输出不得与输入同源（拒绝覆盖原件）")
    if dst_resolved.exists() and not force:
        raise ValueError(f"输出已存在：{dst}（默认拒绝覆盖；确需重跑请显式 --force）")
    out_rows: list[dict[str, object]] = []
    skipped = 0
    total = 0
    with src.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total += 1
            item = convert_row(
                json.loads(line), model_revision=model_revision, rule_revision=rule_revision
            )
            if item is None:
                skipped += 1
            else:
                out_rows.append(item)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        for item in out_rows:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    os.replace(tmp, dst)  # 原子替换：中途失败不破坏已有字节（R-113 F03）
    positives = sum(1 for r in out_rows if r["label"] == "confirmed_violation")
    stats = {
        "total": total,
        "converted": len(out_rows),
        "skipped_unlabeled": skipped,
        "positives": positives,
        "negatives": len(out_rows) - positives,
    }
    print(f"[write] {dst}  converted={len(out_rows)} skipped_unlabeled={skipped}")
    print(f"[stats] {json.dumps(stats, ensure_ascii=False)}")
    if skipped:
        print(f"[提示] {skipped} 行未标注已跳过——全部标注后重跑，评测分母才完整。")
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="抽样标注 → 正式评测输入（显式转换）")
    ap.add_argument("--input", required=True, help="人工标注后的样本池 JSONL")
    ap.add_argument("--output", required=True, help="输出的评测输入 JSONL")
    ap.add_argument("--model-revision", required=True, help="判定窗口内的模型版本标识")
    ap.add_argument("--rule-revision", required=True, help="判定窗口内的规则/提示词版本标识")
    ap.add_argument("--force", action="store_true", help="显式允许覆盖已存在的输出（默认拒绝）")
    args = ap.parse_args()
    try:
        convert_file(
            Path(args.input),
            Path(args.output),
            model_revision=args.model_revision,
            rule_revision=args.rule_revision,
            force=args.force,
        )
    except ValueError as exc:
        print(f"[ERR] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
