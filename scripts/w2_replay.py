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
    """R06/S06：外置规则的摘要——必须复用运行时同款路径解析。

    之前用 ``parents[2]`` 会指向仓库父目录，导致规则存在却记录 ``missing``。
    配置了规则文件却解析不到时直接中止正式评测，禁止把缺失摘要写成通过。
    """
    if not settings.ai_prompt_rules_file:
        return "none"
    from app.runtime.ai_wiring import _resolve_rules_path

    rules_path = _resolve_rules_path(settings)
    if not rules_path.is_file():
        raise SystemExit(
            f"S06：规则文件已配置但无法解析: {rules_path} —— 摘要 missing 时评测结果无效，已中止"
        )
    return hashlib.sha256(rules_path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()[:16]


def _worktree_dirty() -> bool:
    """S06：记录是否存在未提交的工作树改动，供版本溯源核对。"""
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, check=True, timeout=10
        )
        return bool(out.stdout.strip())
    except Exception:  # noqa: BLE001
        return True


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
    degraded_count = 0
    cached_count = 0
    real_call_samples = 0
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
            # S08：供应商故障会被 AIReviewService 捕获为 degraded，不抛异常——
            # 失败统计必须把它计入，否则失败率门禁形同虚设。
            degraded = any(r.degraded_reason for r in _results)
            if degraded and verdict != "ERROR":
                degraded_count += 1
            cached = any(r.cache_hit for r in _results)
            if cached:
                cached_count += 1
            if verdict != "ERROR" and not degraded and not cached:
                real_call_samples += 1
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
                    "degraded": degraded,
                    "cached": cached,
                    # S06/R06：逐样本绑定版本，合并器可直接校验，不再回退清单之外的信息。
                    "model_revision": model_revision,
                    "rule_revision": f"rules+{rules_digest}",
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
                    # S07：回放没有 inbox 端到端证据，延迟必须显式未测；
                    # AI 子链耗时只留在 debug 供诊断，不得进入端到端门槛。
                    "latency_ms": None,
                    "latency_source": "none",
                    "model_revision": model_revision,
                    "rule_revision": f"rules+{rules_digest}",
                }
            )
            print(f"  #{i:3d} [{verdict:14s}] {item['kind']:10s} ai={latency_ai}ms")

    _write(args.out, rows)
    _write(args.debug, debug)
    total_unusable = failed + degraded_count
    _write_json(
        args.manifest,
        {
            "code_sha": _code_sha(),
            "worktree_dirty": _worktree_dirty(),
            "primary_model": settings.ai_vision_model,
            "review_model": settings.ai_review_model or "unconfigured",
            "prompt_version": settings.ai_prompt_version,
            "rules_digest": rules_digest,
            "direct_threshold": settings.ai_primary_direct_threshold,
            "gray_zone_low": settings.ai_secondary_review_low,
            "model_revision": model_revision,
            "samples": len(rows),
            "failed": failed,
            "degraded": degraded_count,
            "cache_hit_samples": cached_count,
            "real_call_samples": real_call_samples,
            "unusable": total_unusable,
            "fail_rate": round(total_unusable / len(rows), 4) if rows else 0.0,
            "note": (
                "S07：samples.latency_ms 显式 null（latency_source=none）——回放无端到端证据；"
                "AI 子链耗时见 debug.latency_ai_ms，仅用于诊断。"
                "端到端延迟须由 onebot_inbox（updated_at-created_at）另行测量。"
            ),
        },
    )
    print(
        f"\n已写出 {len(rows)} 条 → {args.out}"
        f"（异常 {failed}，降级 {degraded_count}，缓存命中 {cached_count}，清单 → {args.manifest}）"
    )
    if rows and total_unusable / len(rows) > FAIL_RATE_LIMIT:
        raise SystemExit(
            f"R08/S08：不可用（异常+降级）{total_unusable / len(rows):.0%} 超过 "
            f"{FAIL_RATE_LIMIT:.0%}，测量无效"
        )


if __name__ == "__main__":
    asyncio.run(main())
