"""导出「哈希白名单会改变判定」的图片审核清单（只读 + 复制原图到审核目录）。

负责人只需对**每张唯一图片**给一次结论（放行 / 撤回），不必逐条消息看。

产出：
- ``docs/evidence/image-review/IMAGE_REVIEW.md``：一张图一行，含命中次数、原判定分布、距离、
  样例消息、以及留空的「负责人结论」列；
- 同目录下 ``img-XX_<hash>.jpg/png``：从 ``data/media`` **复制**的原始图片（只复制，不动原文件）。

用法：
    uv run python scripts/image_review_export.py            # 默认阈值 <= 2（最保守）
    uv run python scripts/image_review_export.py --max-distance 4
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.moderation.image_hash import best_match, dhash64_file, to_hex  # noqa: E402
from image_allowlist_seed import load_excluded, scan_history, scan_samples  # noqa: E402

# A05：回放/导出必须与导入共用**同一套集合**（含负责人排除清单）
EXCLUDE_FILE = ROOT / "docs" / "evidence" / "image-review" / "exclude_hashes.txt"

BLOCKED_CATEGORIES = {"porn", "violence"}
HARD_EVIDENCE = {"R001", "R003", "R006"}


def build_whitelist(samples_dir: Path, db: Path, media_dir: Path) -> list[tuple[int, str]]:
    """生效名单 = 样本 + 历史放行图 **减去** 负责人排除清单（与导入共用同一集合）。"""
    excluded = load_excluded(EXCLUDE_FILE)
    seen: dict[int, str] = {}
    for path, source, note in scan_samples(samples_dir) + scan_history(db, media_dir):
        phash = dhash64_file(path)
        if phash is None or to_hex(phash) in excluded:
            continue
        seen.setdefault(phash, f"{source}:{note}")
    return list(seen.items())


def collect(
    *, db: Path, media_dir: Path, whitelist: list[tuple[int, str]], max_distance: int, limit: int
) -> dict[int, dict[str, object]]:
    """按"命中的白名单哈希"聚合出需要负责人审核的图片。"""
    candidates = [(index, value) for index, (value, _label) in enumerate(whitelist)]
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "select message_id, created_at, verdict, category, external_group_id, detail_json "
        "from shadow_decisions where kind='image' order by created_at desc limit ?",
        (limit,),
    ).fetchall()

    groups: dict[int, dict[str, object]] = {}
    for message_id, created_at, verdict, category, group, detail_json in rows:
        try:
            detail = json.loads(detail_json or "{}")
        except ValueError:
            continue
        names = [
            entry.get("name")
            for entry in detail.get("media_files") or []
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        ]
        rule_ids = {
            entry.get("rule_id")
            for entry in detail.get("rule_hits") or []
            if isinstance(entry, dict)
        }
        blocked = category in BLOCKED_CATEGORIES or any(
            rid in HARD_EVIDENCE or str(rid).startswith("DR_") for rid in rule_ids
        )
        for name in names:
            path = media_dir / name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            if not path.is_file():
                continue
            phash = dhash64_file(path)
            if phash is None:
                continue
            found = best_match(phash, candidates, max_distance=max_distance)
            if found is None:
                continue
            entry = groups.setdefault(
                found[0],
                {
                    "hash": whitelist[found[0]][0],
                    "label": whitelist[found[0]][1],
                    "count": 0,
                    "would_change": 0,
                    "verdicts": Counter(),
                    "categories": Counter(),
                    "distances": [],
                    "samples": [],
                    "image": str(path),
                },
            )
            entry["count"] = int(entry["count"]) + 1  # type: ignore[arg-type]
            entry["verdicts"][verdict] += 1  # type: ignore[index]
            entry["categories"][str(category)] += 1  # type: ignore[index]
            entry["distances"].append(found[1])  # type: ignore[union-attr]
            if verdict != "allow" and not blocked:
                entry["would_change"] = int(entry["would_change"]) + 1  # type: ignore[arg-type]
                if len(entry["samples"]) < 5:  # type: ignore[arg-type]
                    entry["samples"].append(f"{message_id} @{created_at} ({group})")  # type: ignore[union-attr]
    return {index: entry for index, entry in groups.items() if int(entry["would_change"]) > 0}  # type: ignore[arg-type]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出需负责人审核的图片清单（只读 + 复制原图）")
    parser.add_argument("--db", default=str(ROOT / "data" / "moderation.db"))
    parser.add_argument("--media-dir", default=str(ROOT / "data" / "media"))
    parser.add_argument(
        "--samples-dir", default=str(ROOT / "docs" / "evidence" / "allowlist-samples")
    )
    parser.add_argument("--out", default=str(ROOT / "docs" / "evidence" / "image-review"))
    parser.add_argument("--max-distance", type=int, default=2)
    parser.add_argument("--limit", type=int, default=4000)
    args = parser.parse_args(argv)

    whitelist = build_whitelist(Path(args.samples_dir), Path(args.db), Path(args.media_dir))
    groups = collect(
        db=Path(args.db),
        media_dir=Path(args.media_dir),
        whitelist=whitelist,
        max_distance=args.max_distance,
        limit=args.limit,
    )

    out_dir = Path(args.out).resolve()
    media_root = Path(args.media_dir).resolve()
    samples_root = Path(args.samples_dir).resolve()
    # A02：输出目录**不得**与输入目录重合或位于其内——旧实现会在这里通配删除原图。
    if (
        out_dir in (media_root, samples_root)
        or media_root in out_dir.parents
        or samples_root in out_dir.parents
    ):
        print(f"REVIEW_EXPORT_FAILED 输出目录与输入目录重合/位于其内：{out_dir}")
        return 2
    # 每个批次独立目录（不删除任何既有文件）；失败即非零退出。
    batch = out_dir / f"batch-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    batch.mkdir(parents=True, exist_ok=False)

    lines = [
        "# 需负责人审核的图片清单（哈希白名单会改变判定）",
        "",
        f"- 生成时刻（UTC）：{datetime.now(UTC).isoformat(timespec='seconds')}",
        f"- 命中阈值：汉明距离 <= {args.max_distance}；扫描范围：最近 {args.limit} 条图片判定",
        f"- 待审核图片：**{len(groups)} 张**（唯一图片；同图的多条消息已折叠）",
        "",
        "> 请对**每张图**给一个结论（**放行 / 撤回**），我按你的结论增删白名单条目。",
        "> 图片就在本目录下（`img-XX_<hash>.jpg/png`），点开对照即可。",
        "",
        "| 编号 | 图片文件 | 命中次数 | 其中会改变判定 | 原判定分布 | 类别分布 | 距离 | 样例消息 | 负责人结论（放行/撤回） |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    with_images = 0
    copy_failures = 0
    for order, (_index, entry) in enumerate(
        sorted(groups.items(), key=lambda kv: -int(kv[1]["would_change"])), start=1
    ):  # type: ignore[arg-type]
        source = Path(str(entry["image"]))
        suffix = source.suffix.lower() or ".jpg"
        target = batch / f"img-{order:02d}_{to_hex(int(entry['hash']))}{suffix}"
        try:
            shutil.copy2(source, target)
            with_images += 1
        except OSError as exc:  # A02：复制失败必须显式失败，不能谎报完整
            copy_failures += 1
            print(f"  [失败] 复制原图失败：{source}（{exc}）")
            target = source
        verdicts = ", ".join(f"{key}:{value}" for key, value in entry["verdicts"].most_common())  # type: ignore[union-attr]
        categories = ", ".join(
            f"{key}:{value}"
            for key, value in entry["categories"].most_common()  # type: ignore[union-attr]
        )
        distances = entry["distances"]  # type: ignore[assignment]
        samples = "<br>".join(str(x) for x in entry["samples"])  # type: ignore[union-attr]
        lines.append(
            f"| {order:02d} | `{target.name}` | {entry['count']} | **{entry['would_change']}** | "
            f"{verdicts} | {categories or '-'} | "
            f"{min(distances)}~{max(distances)} | {samples or '-'} |  |"
        )
    lines += [
        "",
        f"（已复制 {with_images} 张原图到本目录；原始文件仍在 `data/media`，本操作不修改数据。）",
        "",
        "## 填完之后",
        "",
        "- 标「撤回」的图：我从白名单里**删除**对应哈希（或按你的要求收紧阈值）；",
        "- 标「放行」的图：保留；",
        "- 全部确认后即可切 enforce（放行），并保留色情/暴力与本地硬证据例外。",
    ]
    (batch / "IMAGE_REVIEW.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if copy_failures:
        print(f"REVIEW_EXPORT_FAILED {copy_failures} 张原图复制失败（批次目录 {batch}）")
        return 2
    print(f"REVIEW_EXPORT_OK {batch / 'IMAGE_REVIEW.md'}")
    print(f"待审核图片={len(groups)} 已复制原图={with_images} 阈值<={args.max_distance}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
