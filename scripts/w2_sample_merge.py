"""把人工盲标结果合并为评测工具所需的「元数据-only」JSONL。

盲标流程：
1. `_w2_export.py` 产出 `<out>-labeling.jsonl`（含内容，不含系统判定）与 `<out>-system.jsonl`。
2. 标注人在 `-labeling.jsonl` 每行填 `label`（confirmed_violation / confirmed_normal / false_positive）。
3. 本脚本合并 `-labeling.jsonl`(label) + `-system.jsonl`(verdict/category/延迟) →
   评测工具 `app.reports.evaluation` 所需的严格字段 JSONL。

约束（与评测工具 schema 一致）：label/verdict/kind/category 枚举校验、latency_ms≥0、
model_revision/rule_revision 非空、sample_id 唯一、单一模型/规则版本。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_reconfigure = getattr(sys.stdout, "reconfigure", None)
if _reconfigure is not None:
    _reconfigure(encoding="utf-8")

LABELS = {"confirmed_violation", "confirmed_normal", "false_positive"}
VERDICTS = {"allow", "record_only", "violation_high"}
KINDS = {"text", "image", "gif", "video", "audio", "file", "share_card", "mixed", "unknown"}
CATEGORIES = {"ad", "fraud", "porn", "violence", "flood", "other"}
# 类别回落映射
CAT_MAP = {"": "other", "normal": "other", "null": "other", "None": "other"}


def _load(path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            obj = json.loads(line)
            out[obj["sample_id"]] = obj
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--labeling", default="data/w2_candidates-labeling.jsonl")
    ap.add_argument("--system", default="data/w2_candidates-system.jsonl")
    ap.add_argument("--out", default="data/w2_samples.jsonl")
    ap.add_argument("--strict", action="store_true", help="遇到未标注行即报错")
    args = ap.parse_args()

    labeling = _load(Path(args.labeling))
    system = _load(Path(args.system))

    rows, skipped = [], 0
    for sid, lab in labeling.items():
        label = (lab.get("label") or "").strip()
        if not label:
            if args.strict:
                raise SystemExit(f"未标注: {sid}")
            skipped += 1
            continue
        if label not in LABELS:
            raise SystemExit(f"非法 label '{label}' @ {sid}")
        sysrow = system.get(sid)
        if not sysrow:
            raise SystemExit(f"system 缺失: {sid}")
        kind = sysrow["kind"] if sysrow["kind"] in KINDS else "unknown"
        cat = CAT_MAP.get(sysrow.get("category") or "", sysrow.get("category") or "other")
        if cat not in CATEGORIES:
            cat = "other"
        verdict = sysrow["verdict"]
        if verdict not in VERDICTS:
            raise SystemExit(f"非法 verdict '{verdict}' @ {sid}")
        lat = sysrow.get("latency_ms")
        if lat is None:
            lat = sysrow.get("latency_ai_ms")
        if lat is None:
            raise SystemExit(f"无延迟数据 @ {sid}（inbox 与 ai 均缺）")
        rows.append(
            {
                "sample_id": sid,
                "label": label,
                "verdict": verdict,
                "category": cat,
                "kind": kind,
                "latency_ms": float(lat),
                "model_revision": sysrow["model_revision"][:128],
                "rule_revision": sysrow["rule_revision"][:128],
            }
        )

    if len({r["sample_id"] for r in rows}) != len(rows):
        raise SystemExit("sample_id 重复")
    if len({(r["model_revision"], r["rule_revision"]) for r in rows}) > 1:
        raise SystemExit("混入多个模型/规则版本，必须分开评测")

    Path(args.out).write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    print(f"已生成 {len(rows)} 条 → {args.out}（跳过未标注 {skipped}）")


if __name__ == "__main__":
    main()
