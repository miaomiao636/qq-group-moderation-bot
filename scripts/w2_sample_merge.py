"""把人工盲标结果合并为评测工具所需的「元数据-only」JSONL。

R03：评测的 ``category`` 必须来自人工真值（truth_category），不得使用模型预测类别——
否则漏判样本会被归到 other，类别召回虚高。
R07：latency_ms 在重放口径下是 **AI 子链耗时**，不是端到端；端到端门槛必须用
``onebot_inbox``（updated_at-created_at）另行测量。本脚本输出的报告只用于
精确率/召回；延迟门槛的证据另有来源。
R09：重复 sample_id 在建字典前即检测，冲突直接报错（strict 与否都报）。
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
VERDICTS = {"allow", "record_only", "violation_high", "ERROR"}
KINDS = {"text", "image", "gif", "video", "audio", "file", "share_card", "mixed", "unknown"}
CATEGORIES = {"ad", "fraud", "porn", "violence", "flood", "other"}


def _load_list(path: Path) -> list[dict[str, Any]]:
    """R09：保留全部行，由调用方先做重复检测。"""
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _load_unique(path: Path, what: str) -> dict[str, dict[str, Any]]:
    """R09：先检测重复再建索引，冲突报错，绝不静默覆盖。"""
    out: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        sid = row["sample_id"]
        if sid in out:
            if out[sid] != row:
                raise SystemExit(f"R09：{what} 存在冲突的重复 sample_id: {sid}")
            continue
        out[sid] = row
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--labels", default="data/w2_labels_all.jsonl")
    ap.add_argument("--system", default="data/w2_debug.jsonl")
    ap.add_argument("--out", default="data/w2_samples.jsonl")
    ap.add_argument("--strict", action="store_true", help="遇到未标注行即报错")
    args = ap.parse_args()

    labels = _load_unique(Path(args.labels), "labels")
    system = _load_unique(Path(args.system), "system")

    rows, missing_category, errors, degraded = [], 0, 0, 0
    for sid, lab in labels.items():
        label = (lab.get("label") or "").strip()
        if not label:
            if args.strict:
                raise SystemExit(f"R09/未标注: {sid}")
            continue
        if label not in LABELS:
            raise SystemExit(f"非法 label '{label}' @ {sid}")
        sysrow = system.get(sid)
        if not sysrow:
            raise SystemExit(f"system 缺失: {sid}")
        if "model_revision" not in sysrow or "rule_revision" not in sysrow:
            # S05：旧版 replay debug 没有逐样本版本字段（_FIELDS 契约要求）。
            raise SystemExit(
                f"S05：system 行缺少 model_revision/rule_revision（{sid}）—— "
                "请使用新版 scripts/w2_replay.py 重新回放；跨版本混拼的输入不得评测"
            )
        if sysrow.get("verdict") == "ERROR":
            errors += 1
            continue  # R08：失败样本不进入评测，避免伪装成正常放行/漏判
        if sysrow.get("degraded"):
            degraded += 1
            continue  # S08：降级样本（供应商故障被捕获）同样不得进入评测
        truth_cat = (lab.get("truth_category") or "").strip()
        # S02：缺真值类别的样本不得回退 other——漏判广告会被归入 other 使类别召回虚高。
        # 缺真值即排除，并计入 missing_category 由报告声明。
        if truth_cat not in CATEGORIES:
            missing_category += 1
            if args.strict:
                raise SystemExit(f"S02/缺真值类别: {sid}")
            continue
        kind = sysrow["kind"] if sysrow["kind"] in KINDS else "unknown"
        verdict = sysrow["verdict"]
        if verdict not in VERDICTS:
            raise SystemExit(f"非法 verdict '{verdict}' @ {sid}")
        # S07：latency_ms 只接受 inbox 端到端证据；回放口径必须显式 none/null。
        latency_ms = sysrow.get("latency_ms")
        latency_source = sysrow.get("latency_source") or (
            "inbox" if isinstance(latency_ms, (int, float)) else "none"
        )
        if latency_source != "inbox":
            latency_ms = None
            latency_source = "none"
        rows.append(
            {
                "sample_id": sid,
                "label": label,
                "verdict": verdict,
                "category": truth_cat,
                "kind": kind,
                "latency_ms": latency_ms,
                "latency_source": latency_source,
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
    print(
        f"已生成 {len(rows)} 条 → {args.out}\n"
        f"  缺 truth_category（S02，已排除而非记 other）: {missing_category} 条\n"
        f"  回放异常样本（R08，已排除）: {errors} 条\n"
        f"  供应商降级样本（S08，已排除）: {degraded} 条\n"
        f"  延迟: latency_source=none（回放无端到端证据）；p95 门槛将显示为未测（S07）"
    )


if __name__ == "__main__":
    main()
