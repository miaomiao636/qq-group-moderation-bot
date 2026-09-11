"""端到端延迟证据：从 onebot_inbox（DONE）统计生产 SHADOW 流量的处理延迟。

口径（docs/w2-sample-prep.md）：事件进入系统 → 处理完成 = updated_at - created_at。
- 全量：所有 DONE 事件（含禁用群跳过等轻路径）。
- 按类型：经完整内部 event_key 一对一关联 shadow_decisions 取 kind（inbox 的 payload 在运行时
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
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

_reconfigure = getattr(sys.stdout, "reconfigure", None)
if _reconfigure is not None:
    _reconfigure(encoding="utf-8")

DB = str(Path(__file__).resolve().parents[1] / "data" / "moderation.db")
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
    ap.add_argument("--since", required=True, help="UTC 起（含）")
    ap.add_argument("--until", help="UTC 止（不含）；默认本次取证开始时间")
    ap.add_argument("--database", default=DB, help="只读 SQLite 数据库/一致性备份路径")
    ap.add_argument("--out", default="data/e2e_latency.json")
    args = ap.parse_args()
    try:
        since = _utc(args.since)
        until = _utc(args.until) if args.until else datetime.now(UTC).replace(tzinfo=None)
    except ValueError as exc:
        ap.error(str(exc))
    if since >= until:
        ap.error("since 必须早于 until")
    out = Path(args.out)
    if out.exists():
        ap.error("输出文件已存在；请使用新的证据文件名")
    database = Path(args.database).resolve()
    if not database.is_file():
        ap.error("数据库不存在；取证不会创建数据库")
    # Read-only connection plus one SELECT gives a consistent population snapshot;
    # never use immutable=1 for a live WAL database, as it can miss committed WAL data.
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            f"SELECT i.status, i.created_at, {_SECONDS_EXPR}, s.message_id, s.kind "
            "FROM onebot_inbox i LEFT JOIN shadow_decisions s "
            "ON s.message_id = i.event_key AND s.provider = 'onebot' "
            "WHERE i.created_at >= ? AND i.created_at < ?",
            (since.isoformat(sep=" "), until.isoformat(sep=" ")),
        ).fetchall()
    status_counts = Counter(row[0] for row in rows)
    done = [row for row in rows if row[0] == "DONE"]
    valid = [row for row in done if row[2] is not None and math.isfinite(row[2]) and row[2] >= 0]
    overall_values = [row[2] for row in valid]
    by_kind: dict[str, list[float]] = {}
    for _, _, seconds, message_id, kind in valid:
        if message_id is not None:
            by_kind.setdefault(kind or "unknown", []).append(seconds)
    matched = sum(row[3] is not None for row in done)
    timestamps = [row[1] for row in done]
    report = {
        "schema": "w2-e2e-latency-v2",
        "latency_source": "inbox",
        "note": (
            "DONE 事件入队→流水线完成的耗时（含转人工/降级）；overall 包含未监管群等轻路径。"
            "按类型仅使用完整内部事件键与 OneBot 判定一对一匹配的 DONE；"
            "不是自动识别成功率或真人接管耗时。与 replay 口径分离，不自动判验收通过。"
        ),
        "since_utc": since.isoformat(sep=" "),
        "until_utc": until.isoformat(sep=" "),
        "window_utc": {
            "first": min(timestamps) if timestamps else None,
            "last": max(timestamps) if timestamps else None,
            "done_events": len(done),
        },
        "coverage": {
            "inbox_events": len(rows),
            "status_counts": dict(sorted(status_counts.items())),
            "done_with_decision": matched,
            "done_without_decision": len(done) - matched,
            "done_invalid_latency": len(done) - len(valid),
            "kind_measured_samples": sum(map(len, by_kind.values())),
        },
        "overall": _stats(overall_values),
        "by_kind": {kind: _stats(values) for kind, values in sorted(by_kind.items())},
        "release_decision": "REQUIRES_HUMAN_REVIEW",
        "limitations": [
            "未完成/失败事件不进入完成耗时分位，数量单列；不得以幸存 DONE 代替全量服务质量。",
            "未关联判定不能推断为未监管；也可能是数据缺失。历史清理过的事件不在本窗口内。",
            "聚合未绑定逐事件模型/规则版本，不能替代冻结版本的独立 W2。",
            "本报告不含分阶段耗时，不能单凭 p95 归因为 AI、排队或二审。",
        ],
    }
    with out.open("x", encoding="utf-8") as output:
        output.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print("overall:", json.dumps(report["overall"], ensure_ascii=False))
    for kind, data in report["by_kind"].items():
        print(f"  {kind:10s} {data}")


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


if __name__ == "__main__":
    main()
