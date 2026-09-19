"""把负责人在审核批次里的结论落库（放行 → enabled=1；撤回 → 只进拒绝快照）。

用法：
    uv run python scripts/apply_review_decisions.py --batch <目录> [--dry-run]

约定：
- 批次目录里必须有 `IMAGE_REVIEW.md`（含 `| 编号 | 状态 | 图片文件 | ...` 表）与 `DECISIONS.json`
  （`{"decisions": {"01": "放行"|"撤回", ...}}`）；
- **放行**：把该图片的 dHash 写入 `image_allowlist`（`enabled=1`）；
- **撤回**：**不写行**，只写拒绝快照（`<db>.rejections.json`）——保证导出/回放的候选收集
  不会再把它拉回来（R6-02-R）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.moderation.image_hash import dhash64_file, to_hex  # noqa: E402
from image_allowlist_seed import import_seeds, record_rejection  # noqa: E402

REVIEW_ROOT = ROOT / "docs" / "evidence" / "image-review"
DEFAULT_DB = ROOT / "data" / "moderation.db"
ROW = re.compile(r"^\| (\d\d) \| (.*?) \| `(.*?)` \|")


def load_batch(batch: Path) -> tuple[dict[str, str], dict[str, str]]:
    """→ (编号→图片文件名, 编号→结论)"""
    files: dict[str, str] = {}
    for line in (batch / "IMAGE_REVIEW.md").read_text(encoding="utf-8").splitlines():
        m = ROW.match(line)
        if m:
            files[m.group(1)] = m.group(3)
    decisions: dict[str, str] = json.loads((batch / "DECISIONS.json").read_text(encoding="utf-8"))[
        "decisions"
    ]
    return files, decisions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="落库负责人审核结论")
    parser.add_argument("--batch", default=None)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    batch = (
        Path(args.batch)
        if args.batch
        else max(REVIEW_ROOT.glob("batch-*"), key=lambda p: p.stat().st_mtime)
    )
    db = Path(args.db)
    files, decisions = load_batch(batch)

    approved: list[tuple[Path, str, str]] = []
    rejected: set[str] = set()
    for no, verdict in sorted(decisions.items()):
        name = files.get(no)
        if not name:
            print(f"  [跳过] 编号 {no} 在清单里找不到图片")
            continue
        path = batch / name
        phash = dhash64_file(path)
        if phash is None:
            print(f"  [跳过] 算不出哈希：{name}")
            continue
        if verdict == "放行":
            approved.append((path, "review-2026-09-19", f"{batch.name}:no={no}"))
        elif verdict == "撤回":
            rejected.add(to_hex(phash))
        else:
            print(f"  [未定] {no} = {verdict}（本次不动）")

    print(f"批次 {batch.name}：放行 {len(approved)} 张、撤回 {len(rejected)} 张")
    if args.dry_run:
        return 0
    result: tuple[int, int, int, int] = import_seeds(
        db=db,
        seeds=approved,
        dry_run=False,
        operator="负责人-2026-09-19",
        excluded=rejected,
    )
    for value in sorted(rejected):
        record_rejection(db, value, source=f"review:{batch.name}", operator="负责人-2026-09-19")
    added, duplicate, failed, skipped = result
    print(f"IMPORT_OK 新增={added} 已存在={duplicate} 失败={failed} 排除={skipped}")
    return 0


def _noop(_value: Any) -> None:  # pragma: no cover - 类型锚点
    return None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
