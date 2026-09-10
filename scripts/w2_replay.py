"""W2 离线重放：把人工盲标样本过一遍冻结版本模型，产出评测工具所需 JSONL。

- 复用生产 `AIReviewService.review_message`（含合并逻辑），保证与线上一致
- 本地决策基线固定为 allow（本批评测聚焦 AI 自动处罚建议链）
- `latency_ms` = 本次重放耗时（图片含读盘+base64+模型往返）；生产方式端到端延迟
  另由 `onebot_inbox` 口径测量，两者不混用
- 重放会写入生产 `ai_usage_logs` / `ai_cache`（真实调用，如实记录）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from app.core.contracts import Sender, StandardMessage
from app.db import SessionLocal
from app.moderation.decision import ModerationDecision
from app.runtime.ai_wiring import build_default_ai_review_service

_reconfigure = getattr(sys.stdout, "reconfigure", None)
if _reconfigure is not None:
    _reconfigure(encoding="utf-8")

GROUP = "w2-replay"
CATS = {"ad", "fraud", "porn", "violence", "flood", "other"}


def _load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _write(path: str, rows: list[dict[str, Any]]) -> None:
    Path(path).write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--labels", default="data/w2_labels.jsonl")
    ap.add_argument("--out", default="data/w2_samples.jsonl")
    ap.add_argument("--model-revision", default="deepseek-v4-flash-vision-exp@t204-v4")
    ap.add_argument("--rule-revision", default="ruleset6-v3")
    args = ap.parse_args()

    labels = _load(Path(args.labels))
    service = build_default_ai_review_service()
    rows: list[dict[str, Any]] = []
    debug: list[dict[str, Any]] = []
    async with SessionLocal() as session:
        for i, item in enumerate(labels, 1):
            mid = f"W2REPLAY_{uuid.uuid4().hex[:10]}"
            media_paths = [Path(p) for p in (item.get("media") or [])]
            msg = StandardMessage(
                message_id=mid,
                provider="onebot",
                external_group_id=GROUP,
                external_user_id="replay",
                external_message_id=mid,
                sender=Sender(member_openid="replay"),
                kind=item["kind"],
                text=item.get("text") or "",
            )
            local = ModerationDecision(
                message_id=mid,
                group_openid=GROUP,
                sender_member_openid="replay",
                verdict="allow",
                confidence=0.0,
                reason="replay baseline",
            )
            started = time.monotonic()
            try:
                decision, _results = await service.review_message(
                    session, msg, local, media_paths=media_paths
                )
                verdict, cat, conf = decision.verdict, decision.category, decision.confidence
            except Exception as exc:  # noqa: BLE001
                verdict, cat, conf = "allow", None, 0.0
                print(f"  #{i} ERROR {type(exc).__name__}: {exc}")
            latency = int((time.monotonic() - started) * 1000)
            category = cat if cat in CATS else "other"
            debug.append(
                {
                    "sample_id": item["sample_id"],
                    "label": item["label"],
                    "verdict": verdict,
                    "category": category,
                    "confidence": conf,
                    "kind": item["kind"],
                    "latency_ms": latency,
                    "results": [
                        {
                            "src": r.source,
                            "role": r.review_role,
                            "model": r.model_id,
                            "cat": r.category,
                            "conf": r.confidence,
                            "nr": r.needs_review,
                            "deg": r.degraded_reason,
                        }
                        for r in _results
                    ],
                }
            )
            rows.append(
                {
                    "sample_id": item["sample_id"],
                    "label": item["label"],
                    "verdict": verdict,
                    "category": category,
                    "kind": item["kind"],
                    "latency_ms": float(latency),
                    "model_revision": args.model_revision,
                    "rule_revision": args.rule_revision,
                }
            )
            mark = (
                "OK "
                if ((item["label"] == "confirmed_violation") == (verdict == "violation_high"))
                else "DIFF"
            )
            print(
                f"  #{i:2d} [{mark}] {item['kind']:6s} 人工={item['label']:18s} "
                f"系统={verdict:14s} {category:7s} {latency}ms"
            )

    _write(args.out, rows)
    _write("data/w2_debug.jsonl", debug)
    print(f"\n已写出 {len(rows)} 条 → {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
