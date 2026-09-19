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


def load_excluded(path: Path) -> set[str]:
    """负责人审核后**排除**的哈希（小写十六进制）；文件不存在则为空集。"""
    if not path.is_file():
        return set()
    excluded: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.split("#", 1)[0].strip().lower()
        if value:
            excluded.add(value)
    return excluded


def disabled_hashes(db: Path) -> set[str]:
    """负责人**显式停用/排除**的行（``enabled=0``）——离线审核范围必须把它们剔除。

    表缺失/不可读时返回空集（调用方另有 `effective_hashes` 判断"无法判定"）。
    """
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = con.execute("select phash from image_allowlist where enabled=0").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return set()
    return {str(row[0]).lower() for row in rows}


def effective_state(db: Path) -> tuple[set[str] | None, str]:
    """生效名单 + **读取状态原因**（主审 R6-02）：``("ok")`` / ``("missing_table")`` /
    ``("bad_schema")`` / ``("unreadable")``。

    三态底层契约不变（表存在但为空 = 空集；缺表/缺列 = ``None`` = unavailable），
    但**上层报告必须写出原因**，不能把"缺迁移 / 坏结构"与"生效名单就是空的"混成一句结论。
    """
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None, "unreadable"
    try:
        try:
            rows = con.execute("select phash from image_allowlist where enabled=1").fetchall()
        except sqlite3.OperationalError as exc:
            return None, "missing_table" if "no such table" in str(exc).lower() else "bad_schema"
    finally:
        con.close()
    return {str(row[0]).lower() for row in rows}, "ok"


def effective_hashes(db: Path) -> set[str] | None:
    """**生效名单**：已导入且 ``enabled=1`` 的哈希集合（A05-R）。

    导入 / 回放 / 导出 / shadow **必须读同一集合**：有生效名单就用它；
    没有（尚未导入）时离线工具只能输出**候选**，不得声称"已批准 / 可直接启用"。
    """
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = con.execute("select phash from image_allowlist where enabled=1").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    # **表存在但为空 ≠ 表缺失**（主审探针）：空集合表示"生效名单存在且为空"（默认导入按设计什么都没加、
    # 或被负责人停用/排除），离线工具必须原样输出 ∅；只有表缺失/不可读（sqlite 错误）才返回 None
    # 表示"无法判定"，此时才退化为候选。此前 `values or None` 把两者混为一谈，于是**已被停用或被排除的
    # 样本会被重新当成候选报出来**，与"生效名单优先"的承诺不符。
    return {str(row[0]).lower() for row in rows}


def detail_blockers(detail: dict) -> list[str]:
    """**A06-R / R6-01 离线同源**：回放/导出必须与在线看**同一套**完整证据。

    在线判据是 `app.moderation.ai.attachment_reviews_unresolved`，覆盖：任一附件严重类别、
    任一附件未定论——**含二审异类 / 二审置信不足 / 二审与主审同模型（不独立）/ 孤儿二审**——
    以及 `evidence_vetoes`、附件缺失。
    离线此前只检查严重类别 / `needs_review` / `degraded_reason` / veto，于是"二审无效"的三类
    情形会被离线误报成"会改变判定"（主审 R6-01 实测 6 项）。现在**直接调同一个函数**。

    旧记录缺 `review_role`/`review_group` 等复核字段时无法判定是否已消疑 →
    标 `unresolved_unknown`（**保守**：计入例外），**绝不按默认阈值猜成已消疑**。
    """
    blockers: list[str] = []
    if detail.get("evidence_vetoes"):
        blockers.append("evidence_veto")
    raw = detail.get("ai_results")
    results = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    if any(str(item.get("category") or "") in ("porn", "violence") for item in results):
        blockers.append("attachment_category")
    if any(item.get("needs_review") or item.get("degraded_reason") for item in results):
        blockers.append("unresolved")
    if not results:
        return blockers
    review_evidence = any(
        item.get("review_role") == "secondary" or item.get("review_reason") for item in results
    )
    if any("review_role" not in item or "review_group" not in item for item in results):
        if review_evidence:
            blockers.append("unresolved_unknown")
        return blockers
    try:
        from app.moderation.ai import AIModerationResult, attachment_reviews_unresolved

        parsed = [AIModerationResult.model_validate(item) for item in results]
    except Exception:  # noqa: BLE001 - 解析不了就不猜（有复核痕迹则保守计入例外）
        if review_evidence:
            blockers.append("unresolved_unknown")
        return blockers
    if attachment_reviews_unresolved(parsed):
        # local=None：只用结果里已持久化的 review_reason，不重算、不按默认阈值猜
        blockers.append("unresolved_secondary")
    return blockers


def import_seeds(
    *,
    db: Path,
    seeds: list[tuple[Path, str, str]],
    dry_run: bool,
    operator: str,
    excluded: set[str] | None = None,
) -> tuple[int, int, int, int]:
    """写入白名单；返回 (新增, 已存在(重复), 计算失败, 被排除清单跳过)。"""
    excluded = excluded or set()
    added = duplicate = failed = skipped = 0
    con = None if dry_run else sqlite3.connect(db)
    try:
        for path, source, note in seeds:
            phash = dhash64_file(path)
            if phash is None:
                failed += 1
                print(f"  [跳过] 无法计算哈希：{path.name}")
                continue
            value = to_hex(phash)
            if value in excluded:
                skipped += 1
                print(f"  [排除] 负责人在审核清单中判为撤回：{value} {path.name}")
                if con is not None:
                    # A05：排除必须**真正停用**既有启用条目——只跳过 INSERT 等于没撤权
                    con.execute(
                        "update image_allowlist set enabled=0, "
                        "note = note || ';excluded:2026-09-19' "
                        "where phash = ? and enabled = 1",
                        (value,),
                    )
                continue
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
            # A05：即使该哈希不是本次种子（例如上一批已导入后才被判撤回），
            # 也必须按排除清单停用——保证"生效名单"与负责人结论一致。
            for value in sorted(excluded):
                con.execute(
                    "update image_allowlist set enabled=0, note = note || ';excluded:2026-09-19' "
                    "where phash = ? and enabled = 1",
                    (value,),
                )
            con.commit()
    finally:
        if con is not None:
            con.close()
    return added, duplicate, failed, skipped


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
    parser.add_argument(
        "--exclude-file",
        default=str(ROOT / "docs" / "evidence" / "image-review" / "exclude_hashes.txt"),
        help="负责人审核后排除的哈希清单（每行一个十六进制值，# 开头为注释）",
    )
    args = parser.parse_args(argv)

    excluded = load_excluded(Path(args.exclude_file))
    print(f"排除清单：{len(excluded)} 条（{args.exclude_file}）")
    seeds = scan_samples(Path(args.samples_dir))
    print(f"负责人样本：{len(seeds)} 张")
    if args.from_history:
        history = scan_history(Path(args.db), Path(args.media_dir))
        print(f"历史放行图（原图仍在盘）：{len(history)} 张")
        seeds += history
    added, duplicate, failed, skipped = import_seeds(
        db=Path(args.db),
        seeds=seeds,
        dry_run=args.dry_run,
        operator=args.operator,
        excluded=excluded,
    )
    print(
        f"SEED_DONE added={added} duplicate={duplicate} failed={failed} "
        f"excluded={skipped} dry_run={args.dry_run}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
