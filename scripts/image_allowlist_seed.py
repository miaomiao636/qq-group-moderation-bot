"""图片哈希白名单种子导入（负责人 2026-09-19：「确定放行」的图 + 历史放行图）。

种子来源两类：
1. ``--samples-dir``（默认 ``docs/evidence/allowlist-samples``）：负责人手动确认「确定放行」的原图；
2. ``--from-history``：历史判定里 ``has_miniprogram_code=true`` 且 verdict=allow、且原图仍在
   ``data/media`` 的记录（相当于"负责人已认可的那类图"的真实样本）。

行为：算 64 位 dHash → ``INSERT OR IGNORE``（phash 唯一，重复导入不会重复入库）。
``--dry-run`` 只统计不写库。**只写白名单表，不改判定逻辑**（判定是否使用白名单由 shadow/enforce 开关决定）。
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

from app.moderation.image_hash import dhash64_file, to_hex  # noqa: E402

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def scan_samples(samples_dir: Path) -> list[tuple[Path, str, str]]:
    """返回 [(路径, 来源, 备注)]。"""
    if not samples_dir.is_dir():
        return []
    return [
        (path, "sample", path.stem[:64])
        for path in sorted(samples_dir.iterdir())
        if path.suffix.lower() in IMAGE_SUFFIXES
    ]


def scan_history(db: Path, media_dir: Path, *, limit: int = 5000) -> list[tuple[Path, str, str]]:
    """历史"带小程序码 + allow"且原图仍在盘的图片。只读数据库。"""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "select detail_json from shadow_decisions where kind='image' "
        "and verdict='allow' order by created_at desc limit ?",
        (limit,),
    ).fetchall()
    found: dict[str, tuple[Path, str, str]] = {}
    for (detail_json,) in rows:
        try:
            detail = json.loads(detail_json or "{}")
        except ValueError:
            continue
        results = detail.get("ai_results") or []
        if not any(isinstance(r, dict) and r.get("has_miniprogram_code") for r in results):
            continue
        for entry in detail.get("media_files") or []:
            name = entry.get("name") if isinstance(entry, dict) else None
            if not isinstance(name, str):
                continue
            path = media_dir / name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                found.setdefault(str(path), (path, "history", path.name[:64]))
    return list(found.values())


def import_seeds(
    *, db: Path, seeds: list[tuple[Path, str, str]], dry_run: bool, operator: str
) -> tuple[int, int, int]:
    """写入白名单；返回 (新增, 已存在(重复), 计算失败)。"""
    added = duplicate = failed = 0
    con = None if dry_run else sqlite3.connect(db)
    try:
        for path, source, note in seeds:
            phash = dhash64_file(path)
            if phash is None:
                failed += 1
                print(f"  [跳过] 无法计算哈希：{path.name}")
                continue
            value = to_hex(phash)
            if dry_run:
                added += 1
                print(f"  [dry-run] {value} {source} {path.name}")
                continue
            assert con is not None
            cursor = con.execute(
                "insert or ignore into image_allowlist "
                "(phash, note, source, enabled, created_at, created_by) values (?,?,?,1,?,?)",
                (value, f"{source}:{note}"[:64], source, datetime.now(UTC).isoformat(), operator),
            )
            if cursor.rowcount:
                added += 1
            else:
                duplicate += 1
        if con is not None:
            con.commit()
    finally:
        if con is not None:
            con.close()
    return added, duplicate, failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="图片哈希白名单种子导入")
    parser.add_argument("--db", default=str(ROOT / "data" / "moderation.db"))
    parser.add_argument("--media-dir", default=str(ROOT / "data" / "media"))
    parser.add_argument(
        "--samples-dir", default=str(ROOT / "docs" / "evidence" / "allowlist-samples")
    )
    parser.add_argument("--from-history", action="store_true", help="同时导入历史放行图")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--operator", default="human:seed")
    args = parser.parse_args(argv)

    seeds = scan_samples(Path(args.samples_dir))
    print(f"负责人样本：{len(seeds)} 张")
    if args.from_history:
        history = scan_history(Path(args.db), Path(args.media_dir))
        print(f"历史放行图（原图仍在盘）：{len(history)} 张")
        seeds += history
    added, duplicate, failed = import_seeds(
        db=Path(args.db), seeds=seeds, dry_run=args.dry_run, operator=args.operator
    )
    print(f"SEED_DONE added={added} duplicate={duplicate} failed={failed} dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
