#!/usr/bin/env python3
"""线上样本抽样（"新 W2 → 线上抽样复测"的抽样工具）。

从生产库抽取**未参与调参**的近期影子记录，导出待标注样本池：
- 仅读生产库；输出到 `data/sample_pool/`（本地使用，不进 git——原文副本
  按原消息时间 15 天政策管理）；
- 标注真值（label / truth_category）后可用 `app.reports.evaluation` 复测。

用法：
    .venv\\Scripts\\python.exe scripts/sample_draw.py --since "2026-09-15" --count 150
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
OUT_DIR = ROOT / "data" / "sample_pool"
PROD_DB = ROOT / "data" / "moderation.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-15", help="起始日期（含），默认今天")
    ap.add_argument("--count", type=int, default=150)
    ap.add_argument("--seed", type=int, default=20260915)
    args = ap.parse_args()

    c = sqlite3.connect(f"file:{PROD_DB.as_posix()}?mode=ro", uri=True)
    rows = c.execute(
        "SELECT message_id, provider, external_group_id, member_openid, kind, verdict,"
        " category, confidence, reason, detail_json, created_at FROM shadow_decisions"
        " WHERE created_at >= ? ORDER BY created_at",
        (f"{args.since} 00:00:00",),
    ).fetchall()
    c.close()
    print(f"eligible rows since {args.since}: {len(rows)}")

    by_kind: dict[str, list[tuple]] = {}
    for r in rows:
        by_kind.setdefault(str(r[4]), []).append(r)

    # 分层抽样：按 kind 分布比例抽取（不足则全取）
    rng = random.Random(args.seed)
    picked: list[tuple] = []
    total = len(rows)
    for _kind, group in sorted(by_kind.items()):
        quota = max(1, round(args.count * len(group) / max(total, 1)))
        picked.extend(rng.sample(group, min(quota, len(group))))
    if len(picked) > args.count:
        picked = rng.sample(picked, args.count)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out = OUT_DIR / f"sample_{stamp}.jsonl"
    dist: Counter[str] = Counter()
    verdicts: Counter[str] = Counter()
    with out.open("w", encoding="utf-8", newline="\n") as f:
        for r in sorted(picked, key=lambda x: x[10]):
            try:
                detail = json.loads(r[9] or "{}")
            except json.JSONDecodeError:
                detail = {}
            # R-113 F02：降级状态从存储详情恢复为 unavailable（未知不得默认可用）
            degraded = ""
            for ai in detail.get("ai_results") or []:
                if isinstance(ai, dict) and str(ai.get("degraded_reason") or "").strip():
                    degraded = "degraded"
                    break
            item = {
                "sample_id": str(r[0]).split(":")[-1],
                "message_id": str(r[0]),
                "provider": str(r[1]),
                "external_group_id": str(r[2]),
                "kind": str(r[4]),
                "system_verdict": str(r[5]),
                "system_category": str(r[6]),
                "system_confidence": float(r[7] or 0),
                "reason": str(r[8])[:200],
                "text_preview": str(detail.get("text_preview") or "")[:200],
                "media_kinds": detail.get("media_kinds") or [],
                "unavailable": degraded,
                "created_at": str(r[10]),
                "label": "",
                "truth_category": "",
            }
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
            dist[str(r[4])] += 1
            verdicts[str(r[5])] += 1
    print(f"[write] {out}  n={sum(dist.values())}")
    print("[dist by kind]", dict(dist))
    print("[dist by system verdict]", dict(verdicts))
    print("标注方法：在样本池 JSONL 上填 label(confirmed_violation/confirmed_normal/…) 与")
    print("truth_category(ad/fraud/porn/violence/flood/other)，标完后用评测工具复测。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
