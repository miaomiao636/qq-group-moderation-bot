"""端到端延迟证据：从 onebot_inbox（DONE）统计生产 SHADOW 流量的处理延迟。

口径（docs/w2-sample-prep.md）：事件进入系统 → 处理完成 = updated_at - created_at。
- 全量：所有 DONE 事件（含禁用群跳过等轻路径）。
- 按类型：经 event_key 关联 shadow_decisions 取 kind（inbox 的 payload 在运行时
  已按保留策略裁剪，不再含原始消息段；类型来源以判定表为准）。

这是 A03 要求的端到端来源证据（latency_source=inbox），与 replay 压力口径完全分离。

用法：
    python scripts/w2_e2e_latency_report.py --since "2026-09-10 10:00:00" --out data/e2e_latency.json
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

_reconfigure = getattr(sys.stdout, "reconfigure", None)
if _reconfigure is not None:
    _reconfigure(encoding="utf-8")

DB = "data/moderation.db"
_SECONDS_EXPR = "(julianday(i.updated_at) - julianday(i.created_at)) * 86400.0"


def _stats(values: list[float]) -> dict[str, object]:
    ordered = sorted(values)
    if not ordered:
        return {"samples": 0}

    def at(fraction: float) -> float:
        return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] * 1000

    return {
        "samples": len(ordered),
        "min_ms": round(ordered[0] * 1000),
        "p50_ms": round(at(0.50)),
        "p95_ms": round(at(0.95)),
        "max_ms": round(ordered[-1] * 1000),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default="2026-09-10 10:00:00", help="UTC 起（含）")
    ap.add_argument("--out", default="data/e2e_latency.json")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    window = conn.execute(
        "SELECT MIN(created_at), MAX(created_at), COUNT(*) FROM onebot_inbox "
        "WHERE status='DONE' AND created_at >= ?",
        (args.since,),
    ).fetchone()

    overall_values = [
        row[0]
        for row in conn.execute(
            f"SELECT {_SECONDS_EXPR} FROM onebot_inbox i "
            "WHERE i.status='DONE' AND i.updated_at IS NOT NULL AND i.created_at >= ?",
            (args.since,),
        )
        if row[0] is not None and row[0] >= 0
    ]

    by_kind: dict[str, list[float]] = {}
    for kind, seconds in conn.execute(
        f"SELECT s.kind, {_SECONDS_EXPR} FROM onebot_inbox i "
        "JOIN shadow_decisions s "
        "ON i.event_key = 'onebot:' || i.self_id || ':' || s.external_message_id "
        "WHERE i.status='DONE' AND i.updated_at IS NOT NULL AND i.created_at >= ?",
        (args.since,),
    ):
        if seconds is not None and seconds >= 0:
            by_kind.setdefault(kind, []).append(seconds)

    report = {
        "schema": "w2-e2e-latency-v1",
        "latency_source": "inbox",
        "note": (
            "生产 SHADOW 流量端到端延迟（事件进入→处理完成）。按类型分层经判定表关联；"
            "inbox payload 已按保留策略裁剪，类型以 shadow_decisions.kind 为准。"
            "与 replay 压力口径完全分离（A03）。"
        ),
        "since_utc": args.since,
        "window_utc": {"first": window[0], "last": window[1], "done_events": window[2]},
        "overall": _stats(overall_values),
        "by_kind": {kind: _stats(values) for kind, values in sorted(by_kind.items())},
    }
    Path(args.out).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("overall:", json.dumps(report["overall"], ensure_ascii=False))
    for kind, data in report["by_kind"].items():
        print(f"  {kind:10s} {data}")


if __name__ == "__main__":
    main()
