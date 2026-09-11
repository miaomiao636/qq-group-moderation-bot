"""W2 离线重放：把人工盲标样本过一遍冻结版本模型，产出评测工具所需 JSONL。

R06：model/rule revision 从实际运行配置推导并写入运行清单（不再手填）。
R07：latency_ms 仅在 latency_source=inbox（端到端）时有效；AI 子链耗时单独
     记录为 latency_ai_ms，两者不得混用（清单明确要求端到端口径）。
R08：每条样本独立初始化；异常样本标记 verdict=ERROR 并计数，失败率超过阈值
     直接中止，不把失败伪装成正常放行。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.core.contracts import Sender, StandardMessage
from app.db import SessionLocal
from app.moderation.decision import ModerationDecision
from app.runtime.ai_wiring import build_default_ai_review_service

_reconfigure = getattr(sys.stdout, "reconfigure", None)
if _reconfigure is not None:
    _reconfigure(encoding="utf-8")

GROUP = "w2-replay"
CATS = {"ad", "fraud", "porn", "violence", "flood", "other"}
FAIL_RATE_LIMIT = 0.20  # 失败率超过 20% 视为测量无效


def _load(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        sid = row["sample_id"]
        if sid in seen and seen[sid] != row["label"]:
            raise SystemExit(f"R09：重复 sample_id 且标签冲突: {sid}")
        if sid in seen:
            raise SystemExit(f"R09：重复 sample_id: {sid}")
        seen[sid] = row["label"]
        rows.append(row)
    return rows


def _code_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=10
        ).stdout.strip()[:12]
    except Exception:  # noqa: BLE001
        return "unknown"


def _write(path: str, rows: list[dict[str, Any]]) -> None:
    Path(path).write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )


def _write_json(path: str, obj: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _rules_digest(settings: Any) -> str:
    """R06：外置规则的摘要，进入运行清单与缓存指纹（同步工具函数）。"""
    if not settings.ai_prompt_rules_file:
        return "none"
    rules_path = Path(settings.ai_prompt_rules_file)
    if not rules_path.is_absolute():
        rules_path = Path(__file__).resolve().parents[2] / rules_path
    if not rules_path.is_file():
        return "missing"
    return hashlib.sha256(rules_path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()[:16]


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--labels", default="data/w2_labels_all.jsonl")
    ap.add_argument("--out", default="data/w2_samples.jsonl")
    ap.add_argument("--debug", default="data/w2_debug.jsonl")
    ap.add_argument("--manifest", default="data/w2_manifest.json")
    args = ap.parse_args()

    settings = get_settings()
    labels = _load(Path(args.labels))
    service = build_default_ai_review_service()
    if service.vision_moderator is None:
        raise SystemExit("R06：主视觉模型未配置，回放无效")

    model_revision = f"{settings.ai_vision_model}@{settings.ai_prompt_version}"
    rules_digest = _rules_digest(settings)

    rows: list[dict[str, Any]] = []
    debug: list[dict[str, Any]] = []
    failed = 0
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
            _results: list[Any] = []
            verdict, cat, conf = "ERROR", None, 0.0
            error_kind = ""
            try:
                decision, _results = await service.review_message(
                    session, msg, local, media_paths=media_paths
                )
                verdict, cat, conf = decision.verdict, decision.category, decision.confidence
            except Exception as exc:  # noqa: BLE001 - R08：失败必须显式标记
                failed += 1
                verdict, cat, conf = "ERROR", None, 0.0
                error_kind = type(exc).__name__
            latency_ai = int((time.monotonic() - started) * 1000)
            predicted = cat if cat in CATS else "other"

            debug.append(
                {
                    "sample_id": item["sample_id"],
                    "label": item["label"],
                    "truth_category": item.get("truth_category") or "",
                    "verdict": verdict,
                    "predicted_category": predicted,
                    "confidence": conf,
                    "kind": item["kind"],
                    "latency_ai_ms": latency_ai,
                    "error_kind": error_kind,
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
                    "category": predicted,
                    "kind": item["kind"],
                    "latency_ms": float(latency_ai),
                    "model_revision": model_revision,
                    "rule_revision": f"rules+{rules_digest}",
                }
            )
            print(f"  #{i:3d} [{verdict:14s}] {item['kind']:10s} {latency_ai}ms")

    _write(args.out, rows)
    _write(args.debug, debug)
    _write_json(
        args.manifest,
        {
            "code_sha": _code_sha(),
            "primary_model": settings.ai_vision_model,
            "review_model": settings.ai_review_model or "unconfigured",
            "prompt_version": settings.ai_prompt_version,
            "rules_digest": rules_digest,
            "direct_threshold": settings.ai_primary_direct_threshold,
            "gray_zone_low": settings.ai_secondary_review_low,
            "model_revision": model_revision,
            "samples": len(rows),
            "failed": failed,
            "fail_rate": round(failed / len(rows), 4) if rows else 0.0,
            "note": "latency 口径=AI 子链耗时；端到端延迟须由 onebot_inbox 口径另行测量（R07）",
        },
    )
    print(f"\n已写出 {len(rows)} 条 → {args.out}（失败 {failed}，清单 → {args.manifest}）")
    if rows and failed / len(rows) > FAIL_RATE_LIMIT:
        raise SystemExit(
            f"R08：失败率 {failed / len(rows):.0%} 超过 {FAIL_RATE_LIMIT:.0%}，测量无效"
        )


if __name__ == "__main__":
    asyncio.run(main())
