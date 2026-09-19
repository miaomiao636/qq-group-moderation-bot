"""图片哈希白名单「历史回放对比」（只读，主审式取证）。

回答一个问题：**如果当时就启用哈希白名单放行，历史上哪些判定会被改变？**

口径与 D-039 小程序码放行一致（白名单只提供"负责人认可的图"这一条证据）：
- 命中白名单 → 若该消息**不含**色情/暴力类别，且**没有**本地硬证据
  （R001 黑名单词 / R003 联系方式 / R006 分享卡片 / `DR_` 动态规则）→ 会被放行；
- 命中但被上述例外拦住 → 记为「安全例外生效」；
- 未命中 → 不影响（判定不变）。

输出差异清单（含 message_id / 群 / 原判定 / 类别 / 距离），供人工逐条确认后再决定是否切 enforce。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.moderation.image_hash import DEFAULT_MAX_DISTANCE, best_match, dhash64_file  # noqa: E402
from image_allowlist_seed import scan_history, scan_samples  # noqa: E402

BLOCKED_CATEGORIES = {"porn", "violence"}
HARD_EVIDENCE = {"R001", "R003", "R006"}


def load_whitelist(samples_dir: Path, db: Path, media_dir: Path) -> list[tuple[int, str]]:
    """构建白名单 [(phash, 来源说明)]（同一张图只保留一条）。"""
    seen: dict[int, str] = {}
    for path, source, note in scan_samples(samples_dir) + scan_history(db, media_dir):
        phash = dhash64_file(path)
        if phash is not None:
            seen.setdefault(phash, f"{source}:{note}")
    return list(seen.items())


def replay(
    *, db: Path, media_dir: Path, whitelist: list[tuple[int, str]], max_distance: int, limit: int
) -> dict[str, object]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "select message_id, created_at, verdict, category, external_group_id, detail_json "
        "from shadow_decisions where kind='image' order by created_at desc limit ?",
        (limit,),
    ).fetchall()

    would_change: list[dict[str, object]] = []
    exceptions: list[dict[str, object]] = []
    not_matched = matched_ok = 0
    for message_id, created_at, verdict, category, group, detail_json in rows:
        try:
            detail = json.loads(detail_json or "{}")
        except ValueError:
            continue
        files = [
            entry.get("name")
            for entry in detail.get("media_files") or []
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        ]
        candidates = [(index, value) for index, (value, _label) in enumerate(whitelist)]
        hit: tuple[int, int, str] | None = None
        for name in files:
            path = media_dir / name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            if not path.is_file():
                continue
            phash = dhash64_file(path)
            if phash is None:
                continue
            found = best_match(phash, candidates, max_distance=max_distance)
            if found is not None:
                hit = (found[0], found[1], whitelist[found[0]][1])
                break
        if hit is None:
            not_matched += 1
            continue
        hit_ids = hit[1]
        rule_ids = {
            entry.get("rule_id")
            for entry in detail.get("rule_hits") or []
            if isinstance(entry, dict)
        }
        blocked = category in BLOCKED_CATEGORIES or any(
            rid in HARD_EVIDENCE or str(rid).startswith("DR_") for rid in rule_ids
        )
        record = {
            "message_id": message_id,
            "created_at": created_at,
            "group": group,
            "verdict": verdict,
            "category": category,
            "distance": hit_ids,
            "whitelist": hit[2],
            "blocked_by": sorted(
                ({"category"} if category in BLOCKED_CATEGORIES else set())
                | {str(r) for r in rule_ids if r in HARD_EVIDENCE or str(r).startswith("DR_")}
            ),
        }
        if blocked:
            exceptions.append(record)
        elif verdict in ("allow",):
            matched_ok += 1
        else:
            would_change.append(record)
    return {
        "scanned": len(rows),
        "whitelist_size": len(whitelist),
        "not_matched": not_matched,
        "matched_already_allow": matched_ok,
        "exceptions": exceptions,
        "would_change": would_change,
    }


def render(result: dict[str, object], *, window: str, max_distance: int) -> str:
    lines = [
        "# 图片哈希白名单：历史回放对比（只读）",
        "",
        f"- 生成时刻（UTC）：{datetime.now(UTC).isoformat(timespec='seconds')}",
        f"- 对比范围：{window}（最近 N 条图片判定）",
        f"- 白名单规模：{result['whitelist_size']} 条哈希；命中阈值：汉明距离 <= {max_distance}",
        f"- 扫描记录：{result['scanned']} 条",
        "",
        "## 结论摘要",
        "",
        f"- **会改变结论的**（命中白名单、原判非 allow、且不落入例外）：**{len(result['would_change'])} 条**",
        f"- 命中但被**安全例外**拦住（色情/暴力 或 本地硬证据）：{len(result['exceptions'])} 条",
        f"- 命中的本来就是 allow（结论不变）：{result['matched_already_allow']} 条",
        f"- 未命中白名单（结论不变）：{result['not_matched']} 条",
        "",
        "## 会改变结论的明细（需人工逐条确认）",
        "",
        "| 消息 | 时间(UTC) | 群 | 原判定 | 类别 | 距离 | 命中来源 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    if result["would_change"]:
        for row in result["would_change"][:200]:
            lines.append(
                f"| {row['message_id']} | {row['created_at']} | {row['group']} | "
                f"{row['verdict']} | {row['category']} | {row['distance']} | {row['whitelist']} |"
            )
    else:
        lines.append("| （无） | | | | | | |")
    lines += [
        "",
        "## 安全例外拦住命中的明细（证明例外在生效）",
        "",
        "| 消息 | 原判定 | 类别 | 被谁拦住 |",
        "| --- | --- | --- | --- |",
    ]
    if result["exceptions"]:
        for row in result["exceptions"][:100]:
            lines.append(
                f"| {row['message_id']} | {row['verdict']} | {row['category']} | "
                f"{','.join(str(x) for x in row['blocked_by']) or '-'} |"
            )
    else:
        lines.append("| （无） | | | |")
    lines += [
        "",
        "## 判读方式",
        "",
        "- `会改变结论的` 为 0（或全部确认为「该类图确实应放行」）→ 可以切 enforce；",
        "- 若清单里有你**认为应该撤回**的图 → 说明白名单/阈值过宽，收紧阈值或删除对应条目；",
        "- 本报告不改任何数据、不参与线上判定。",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="图片哈希白名单历史回放对比（只读）")
    parser.add_argument("--db", default=str(ROOT / "data" / "moderation.db"))
    parser.add_argument("--media-dir", default=str(ROOT / "data" / "media"))
    parser.add_argument(
        "--samples-dir", default=str(ROOT / "docs" / "evidence" / "allowlist-samples")
    )
    parser.add_argument("--max-distance", type=int, default=DEFAULT_MAX_DISTANCE)
    parser.add_argument("--limit", type=int, default=4000)
    parser.add_argument("--out", default=str(ROOT / "docs" / "evidence" / "stats"))
    args = parser.parse_args(argv)

    whitelist = load_whitelist(Path(args.samples_dir), Path(args.db), Path(args.media_dir))
    result = replay(
        db=Path(args.db),
        media_dir=Path(args.media_dir),
        whitelist=whitelist,
        max_distance=args.max_distance,
        limit=args.limit,
    )
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"image-hash-replay-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.md"
    out.write_text(
        render(result, window=f"最近 {args.limit} 条图片判定", max_distance=args.max_distance),
        encoding="utf-8",
    )
    print(f"REPLAY_OK {out}")
    print(
        f"whitelist={result['whitelist_size']} scanned={result['scanned']} "
        f"would_change={len(result['would_change'])} exceptions={len(result['exceptions'])} "
        f"matched_allow={result['matched_already_allow']} not_matched={result['not_matched']}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
