"""W2 独立样本候选导出（本地运行，含内容供人工盲标）。

延迟口径（本工具明确记录，避免歧义）：
- 主口径 `latency_ms` = `onebot_inbox.updated_at - created_at`，即「事件入队 → 处理完成」
  的端到端耗时，含排队、媒体下载、AI 条件复核与本地判定。DONE 行保留
  `DECISION_RETENTION_DAYS`（默认180天），仅 W0 live inbox 之后的事件可用。
- 回退口径 `latency_ai_ms` = `ai_usage_logs.latency_ms`，仅单次 AI 调用耗时，
  **不含**排队/下载/判定，与主口径不可混用。
- `latency_source` 字段标明来源（inbox / ai_fallback / missing）。

版本口径：必须显式传入采样窗口冻结的 model/rule revision；数据库没有逐消息的
完整版本证据，本工具仅记录人工声明（operator_declared），不验证其真实性。
正式验收前须另行核验窗口内的实际部署、提示词与规则版本，不能声明当前版本代替。

脱敏：输出不含群ID/用户ID/原始事件；media 仅给本机相对路径（供盲标人查看）。

输出：
- <out>-labeling.jsonl  盲标用（含内容，**不含**系统判定，避免标注偏见）
- <out>-system.jsonl    系统输出（判定/类别/置信度/延迟，供合并）
- <out>-manifest.json   覆盖统计
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_reconfigure = getattr(sys.stdout, "reconfigure", None)
if _reconfigure is not None:
    _reconfigure(encoding="utf-8")

MEDIA_DIR = Path("data/media")


def _sid(provider: str, group: str, mid: str) -> str:
    raw = f"{provider}|{group}|{mid}".encode()
    return "s" + hashlib.sha256(raw).hexdigest()[:15]


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _media_paths(detail: dict[str, Any]) -> list[str]:
    out = []
    for item in detail.get("media_files") or []:
        name = item.get("name") if isinstance(item, dict) else None
        if name and (MEDIA_DIR / name).exists():
            out.append(str(MEDIA_DIR / name))
    return out


def _ai_summary(detail: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in detail.get("ai_results") or []:
        out.append(
            {
                "src": r.get("source"),
                "cat": r.get("category"),
                "conf": r.get("confidence"),
            }
        )
    return out


def _revision(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 128:
        raise argparse.ArgumentTypeError("冻结版本声明须为 1–128 个字符，不能留空")
    return value


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", required=True, help="窗口起点 UTC，如 '2026-09-10 10:00:00'")
    ap.add_argument("--out", default="data/w2_candidates")
    ap.add_argument(
        "--model-revision",
        required=True,
        type=_revision,
        help="人工声明的冻结模型/提示词版本，须另行核验",
    )
    ap.add_argument(
        "--rule-revision", required=True, type=_revision, help="人工声明的冻结规则版本，须另行核验"
    )
    ap.add_argument("--limit", type=int, default=0, help="0=全部")
    args = ap.parse_args()

    output_paths = [
        Path(f"{args.out}-labeling.jsonl"),
        Path(f"{args.out}-system.jsonl"),
        Path(f"{args.out}-manifest.json"),
    ]
    for path in output_paths:
        if path.exists():
            raise SystemExit(f"输出文件已存在：{path}；请更换 --out，避免覆盖人工标注或已有证据")

    c = sqlite3.connect("file:data/moderation.db?mode=ro", uri=True)
    sql = (
        "SELECT s.provider, s.external_group_id, s.external_message_id, s.message_id, "
        "s.kind, s.verdict, s.category, s.confidence, s.detail_json, s.created_at "
        "FROM shadow_decisions s WHERE s.created_at >= ? "
        "ORDER BY s.id DESC"
    )
    if args.limit:
        sql += f" LIMIT {args.limit}"
    rows = c.execute(sql, (args.since,)).fetchall()

    labeling: list[dict[str, Any]] = []
    system: list[dict[str, Any]] = []
    stats: dict[str, Any] = {
        "total": 0,
        "by_kind": {},
        "by_verdict": {},
        "latency_src": {},
        "model_revision": args.model_revision,
        "rule_revision": args.rule_revision,
        "revision_source": "operator_declared",
    }
    for provider, group, ext_mid, mid, kind, verdict, cat, conf, detail_raw, _created in rows:
        try:
            detail = json.loads(detail_raw or "{}")
        except json.JSONDecodeError:
            detail = {}
        sid = _sid(provider or "?", group or "?", ext_mid or mid)

        # 仅 DONE 的更新时间代表处理完成；PROCESSING/PENDING/DEAD 不能作为端到端证据。
        inbox = c.execute(
            "SELECT created_at, updated_at FROM onebot_inbox WHERE event_key = ? AND status = 'DONE'",
            (mid,),
        ).fetchone()
        latency_ms, latency_src = None, "missing"
        if inbox:
            t0, t1 = _parse_dt(inbox[0]), _parse_dt(inbox[1])
            if t0 and t1:
                latency_ms = round((t1 - t0).total_seconds() * 1000)
                latency_src = "inbox"
        ai_ms = c.execute(
            "SELECT latency_ms FROM ai_usage_logs WHERE message_id = ? ORDER BY id LIMIT 1",
            (ext_mid,),
        ).fetchone()
        latency_ai = ai_ms[0] if ai_ms else None
        if latency_ms is None and latency_ai:
            latency_src = "ai_fallback"

        text = (detail.get("text_preview") or "").strip()
        media = _media_paths(detail)

        labeling.append(
            {
                "sample_id": sid,
                "kind": kind,
                "text": text,
                "media": media,
                "label": "",  # 待人工填写
                "truth_category": "",  # 待人工填写，不暴露系统预测类别
            }
        )
        system.append(
            {
                "sample_id": sid,
                "kind": kind,
                "verdict": verdict,
                "category": cat or "other",
                "confidence": conf,
                "latency_ms": latency_ms,
                "latency_ai_ms": latency_ai,
                "latency_source": latency_src,
                "ai": _ai_summary(detail),
                "unavailable": "degraded"
                if any(
                    isinstance(result, dict) and result.get("degraded_reason")
                    for result in detail.get("ai_results") or []
                )
                else "",
                "model_revision": args.model_revision,
                "rule_revision": args.rule_revision,
            }
        )
        stats["total"] += 1
        stats["by_kind"][kind] = stats["by_kind"].get(kind, 0) + 1
        stats["by_verdict"][verdict] = stats["by_verdict"].get(verdict, 0) + 1
        stats["latency_src"][latency_src] = stats["latency_src"].get(latency_src, 0) + 1

    # 独占创建，防止导出读取期间新建的文件被覆盖。
    with output_paths[0].open("x", encoding="utf-8") as f:
        f.write(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in labeling)
            + ("\n" if labeling else "")
        )
    with output_paths[1].open("x", encoding="utf-8") as f:
        f.write(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in system) + ("\n" if system else "")
        )
    with output_paths[2].open("x", encoding="utf-8") as f:
        f.write(json.dumps(stats, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
