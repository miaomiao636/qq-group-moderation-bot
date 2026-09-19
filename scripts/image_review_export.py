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
import hashlib
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

from app.moderation.image_hash import (  # noqa: E402
    best_match,
    dhash64_file,
    frame_scope_of,
    to_hex,
)
from image_allowlist_seed import (  # noqa: E402
    detail_blockers,
    disabled_hashes,
    effective_hashes,
    effective_state,
    load_excluded,
    load_rejections,
    scan_history,
    scan_samples,
)

# A05：回放/导出必须与导入共用**同一套集合**（含负责人排除清单）
EXCLUDE_FILE = ROOT / "docs" / "evidence" / "image-review" / "exclude_hashes.txt"

BLOCKED_CATEGORIES = {"porn", "violence"}
HARD_EVIDENCE = {"R001", "R003", "R006"}


def _referenced_hashes(db: Path, media_dir: Path) -> set[int]:
    """**判定里确实出现过的**图片哈希（不按判定结论过滤：关键是"有没有发生过判定"）。

    用于生效名单为空时的审核批次：负责人要看的是"这些真实出现过的图会不会因白名单改变判定"。
    """
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute("select detail_json from shadow_decisions where kind='image'").fetchall()
    finally:
        con.close()
    values: set[int] = set()
    for (detail_json,) in rows:
        try:
            detail = json.loads(detail_json or "{}")
        except ValueError:
            continue
        for entry in detail.get("media_files") or []:
            name = entry.get("name") if isinstance(entry, dict) else None
            if not isinstance(name, str):
                continue
            path = media_dir / name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            if not path.is_file():
                continue
            phash = dhash64_file(path)
            if phash is not None:
                values.add(phash)
    return values


def build_whitelist(samples_dir: Path, db: Path, media_dir: Path) -> list[tuple[int, str]]:
    """**生效名单优先**（已导入且 enabled=1，与 shadow 读取完全一致，A05-R）。

    - 生效名单**非空** → 直接用；
    - 生效名单**存在但为空**（默认导入什么都没加 / 全被停用排除）→ **审核批次不得塌成空**：
      取"判定里确实出现过的图"里、与样本库同一张的那些（未被停用/排除）——否则导出会静默
      输出 0 张图并以成功退出，等于谎报"没有需要复核的图"；
    - 表**缺失/不可读** → 候选：样本库 + 历史放行图 − 排除清单。
    """
    approved = effective_hashes(db)
    if approved is not None:
        # 生效名单**存在**（非空或为空）→ **只认生效名单**（R6-02）。
        # 生效名单为空时**不得**把"判定引用过、样本库里也有"的图补成候选来冒充生效评估——
        # 候选收集是 `collect` 的职责，且会显式标注"未在生效名单（候选）"；
        # 在构建集合时偷偷补人，会让负责人以为这是生效名单。
        return [(int(value, 16), "db:enabled") for value in sorted(approved)]
    # 表缺失/不可读 → 候选：样本库 + 历史放行图 − 排除清单（来源标 candidate）
    excluded = load_excluded(EXCLUDE_FILE)
    seen: dict[int, str] = {}
    for path, source, note in scan_samples(samples_dir) + scan_history(db, media_dir):
        phash = dhash64_file(path)
        if phash is None or to_hex(phash) in excluded:
            continue
        seen.setdefault(phash, f"candidate:{source}:{note}")
    return list(seen.items())


def collect(
    *, db: Path, media_dir: Path, whitelist: list[tuple[int, str]], max_distance: int, limit: int
) -> dict[int, dict[str, object]]:
    """按"命中的白名单哈希"聚合出需要负责人审核的图片。"""
    candidates = [(index, value) for index, (value, _label) in enumerate(whitelist)]
    # R6-02-R：拒绝快照（导入侧记录）与排除清单、停用行一起构成"负责人已拒绝"的同一份事实。
    rejected = load_rejections(db)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "select message_id, created_at, verdict, category, external_group_id, detail_json "
        "from shadow_decisions where kind='image' order by created_at desc limit ?",
        (limit,),
    ).fetchall()

    # A04：按 **(命中种子, 该文件的 SHA-256)** 分组——旧实现只按种子分组，会把"字节不同、
    # 但 dHash 相近"的多张图折叠成一条并只展示第一张，人工看到一张不能推定审核了整个匹配簇。
    groups: dict[tuple[int, str], dict[str, object]] = {}
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
        detail_block = detail_blockers(detail)  # A06-R：离线与在线同源看全证据
        blocked = (
            category in BLOCKED_CATEGORIES
            or any(rid in HARD_EVIDENCE or str(rid).startswith("DR_") for rid in rule_ids)
            or bool(detail_block)
        )
        for name in names:
            path = media_dir / name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            if not path.is_file():
                continue
            phash = dhash64_file(path)
            if phash is None:
                continue
            found = best_match(phash, candidates, max_distance=max_distance)
            unmatched = found is None
            if unmatched and candidates:
                # 生效名单**非空**：不在名单内的图与本次审核无关（保持原行为）
                continue
            if unmatched:
                # R6-02-R：候选收集必须套**同一份拒绝快照**——负责人已明确拒绝的图
                # （排除清单 / 自定义排除（拒绝快照）/ 库内 enabled=0）**不得**被重新拉出来。
                value = to_hex(phash)
                if value in rejected:
                    continue
                if value in load_excluded(EXCLUDE_FILE):
                    continue
                if value in disabled_hashes(db):
                    continue
            try:
                file_sha = hashlib.sha256(path.read_bytes()).hexdigest()
                file_size = path.stat().st_size
            except OSError:
                continue
            # R6-02：生效名单**为空**（空表/不可读）时，判定里出现过的图**仍纳入批次**，
            # 但显式标成"未在生效名单（候选）"，且**不计入"会改变判定"**——
            # 没有生效条目就不存在"因白名单而改变"的结论。
            entry = groups.setdefault(
                (-1, file_sha) if unmatched else (found[0], file_sha),
                {
                    "seed_hash": 0 if unmatched else whitelist[found[0]][0],
                    "label": "未在生效名单（候选）" if unmatched else whitelist[found[0]][1],
                    "file_dhash": phash,
                    "file_sha256": file_sha,
                    "file_size": file_size,
                    # R7（主审）：动图范围必须在**离线清单**里可见——多帧图只按**首帧**参与哈希，
                    # 命中不代表整段动图等价（在线观察侧已有 `frame_scope`，这里补到导出侧）。
                    "frame_scope": frame_scope_of(path.read_bytes()),
                    # 该行"没有计入会改变判定"的原因（供清单显示，避免静默剔除）
                    "blocked_by": [],
                    "unknown": 0,
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
            if found is not None:
                entry["distances"].append(found[1])  # type: ignore[union-attr]
            # R6-02-R：**只有命中生效条目**才可能"因白名单改变判定"；候选（未匹配）一律为 0。
            if found is not None and verdict != "allow" and not blocked:
                entry["would_change"] = int(entry["would_change"]) + 1  # type: ignore[arg-type]
            elif found is not None and detail_block:
                # R6-01-R 的后果修正：历史记录缺政策上下文（`unresolved_unknown`）时**不再静默剔除**——
                # 如实计入 `unknown` 并在清单标注，否则负责人会**漏审**这些图。
                entry["blocked_by"] = sorted(
                    {*entry["blocked_by"], *detail_block}  # type: ignore[arg-type, index]
                )
                entry["unknown"] = int(entry["unknown"]) + 1  # type: ignore[arg-type]
                if len(entry["samples"]) < 5:  # type: ignore[arg-type]
                    entry["samples"].append(f"{message_id} @{created_at} ({group})")  # type: ignore[union-attr]
    # R6-02-R：返回过滤必须与计数同步——**候选行即使"会改变判定"为 0 也必须保留**
    # （否则修完计数后合法候选会被整体滤掉）；只有"非候选且 would_change=0"的行不展示。
    return {
        index: entry
        for index, entry in groups.items()
        # 候选（未命中）与"命中了但结论无法判定（缺证据）"的行**都必须保留**，
        # 否则清单会静默变小、负责人漏审。
        if int(entry["would_change"]) > 0 or index[0] == -1 or int(entry["unknown"]) > 0
    }  # type: ignore[arg-type]


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

    # R6-02：**生效名单评估**与**候选图片搜集**必须分列，且报告要写出模式、来源、读取状态——
    # 不能悄悄把"读不到白名单"或"生效名单为空"补成候选后仍冒充同一次生效评估。
    _state, state_reason = effective_state(Path(args.db))
    labels = [source for _value, source in whitelist]
    # R6-02-R：模式必须由**实际产出的行**决定（不能只看 builder 的 label），
    # 否则会出现"头部说无候选、表格却有 1 张"的自相矛盾。
    has_candidate_rows = any(key[0] == -1 for key in groups)
    if state_reason != "ok":
        set_mode = "candidate"
        mode_note = f"生效名单不可读（`{state_reason}`）→ 本清单是**候选**，不是生效评估"
    elif has_candidate_rows:
        set_mode = "candidate_referenced"
        mode_note = (
            "生效名单**为空** → 本清单是**候选收集**（每行标注『未在生效名单（候选）』），"
            "**不是**生效名单评估"
        )
    elif _state:
        set_mode = "active"
        mode_note = "按**生效名单**（`image_allowlist` 中 `enabled=1`）评估"
    else:
        set_mode = "empty"
        mode_note = "生效名单为空且无候选"
    source_mix = ", ".join(sorted({label.split(":")[0] for label in labels})) or "无"

    lines = [
        "# 需负责人审核的图片清单（哈希白名单会改变判定）",
        "",
        f"- 生成时刻（UTC）：{datetime.now(UTC).isoformat(timespec='seconds')}",
        f"- **集合模式**：`{set_mode}` —— {mode_note}",
        f"- **集合来源构成**：{source_mix}",
        f"- 命中阈值：汉明距离 <= {args.max_distance}；扫描范围：最近 {args.limit} 条图片判定",
        f"- 待审核图片：**{len(groups)} 张**（唯一图片；同图的多条消息已折叠）",
        f"- **动图范围**：{sum(1 for e in groups.values() if e.get('frame_scope') == 'first_frame')} 张为"
        "多帧图（**仅按首帧参与哈希**——命中不代表整段动图等价，请据此判断是否可放行）",
        f"- **无法判定（历史记录缺政策上下文）**："
        f"{sum(1 for e in groups.values() if int(e.get('unknown', 0)) > 0)} 张"
        "——这些图**已列出但未计入「会改变判定」**（不是「不会变」，而是**证据不足以判定**），请人工判断",
        "",
    ]
    if set_mode != "active":
        lines += [
            "> ⚠️ **本清单不是生效名单评估结果**：它只是**候选**图片搜集，供负责人判断哪些图"
            "**值得考虑**；不得据此声称与在线使用同一生效集合。",
            "",
        ]
    lines += [
        "> 请对**每张图**给一个结论（**放行 / 撤回**），我按你的结论增删白名单条目。",
        "> 图片就在本目录下（`img-XX_<hash>.jpg/png`），点开对照即可。",
        "",
        "| 编号 | 状态 | 图片文件 | 来源 | 文件 SHA-256（前 12） | 文件 dHash | 命中种子 dHash | 距离 | 命中次数 | 其中会改变判定 | 原判定分布 | 类别分布 | 样例消息 | 负责人结论（放行/撤回） |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    with_images = 0
    copy_failures = 0

    def _priority(item: tuple[tuple[int, str], dict[str, object]]) -> tuple[int, int]:
        """审核优先级：①会改变判定 → ②无法判定（缺证据）→ ③候选（未在生效名单）→ ④命中不改变。"""
        key, entry = item
        if int(entry["would_change"]) > 0:
            return (0, -int(entry["would_change"]))
        if int(entry["unknown"]) > 0:
            return (1, -int(entry["count"]))
        if key[0] == -1:
            return (2, -int(entry["count"]))
        return (3, -int(entry["count"]))

    for order, (_index, entry) in enumerate(sorted(groups.items(), key=_priority), start=1):  # type: ignore[arg-type]
        source = Path(str(entry["image"]))
        suffix = source.suffix.lower() or ".jpg"
        # 文件名写**该文件自己的** dHash + SHA-256 前缀（A04：不能再拿种子 dHash 冒充文件身份）
        target = batch / (
            f"img-{order:02d}_{to_hex(int(entry['file_dhash'] or 0))}_"
            f"{str(entry['file_sha256'])[:8]}{suffix}"
        )
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
        dist_text = f"{min(distances)}~{max(distances)}" if distances else "-"
        # R6-02-R：未命中生效条目时**不能**用零哈希冒充"命中种子"，如实显示不适用；
        # 同时把**每行的来源**（生效条目 / 候选）渲染出来，不能只留在内部字段里。
        seed_text = (
            "-" if int(entry["seed_hash"] or 0) == 0 else to_hex(int(entry["seed_hash"] or 0))
        )
        # 逐行披露动图范围（主审：不能只留在在线 JSON / 只在头部计数）
        label_text = str(entry["label"]) + (
            "（动图:仅首帧）" if entry.get("frame_scope") == "first_frame" else ""
        )
        if int(entry["unknown"]) > 0:
            label_text += "（无法判定:证据不足）"
        if int(entry["would_change"]) > 0:
            status = "**会改变判定**"
        elif int(entry["unknown"]) > 0:
            status = "无法判定"
        elif _index[0] == -1:
            status = "候选（未在生效名单）"
        else:
            status = "命中（不改变）"
        lines.append(
            f"| {order:02d} | {status} | `{target.name}` | {label_text} | {str(entry['file_sha256'])[:12]} | "
            f"{to_hex(int(entry['file_dhash'] or 0))} | {seed_text} | "
            f"{dist_text} | {entry['count']} | **{entry['would_change']}** | "
            f"{verdicts} | {categories or '-'} | {samples or '-'} |  |"
        )
    lines += [
        "",
        f"（已复制 {with_images} 张原图到本目录；原始文件仍在 `data/media`，本操作不修改数据。）",
        "",
        "## 填完之后",
        "",
        "- 标「撤回」的图：我从白名单里**删除**对应哈希（或按你的要求收紧阈值）；",
        "- 标「放行」的图：保留；",
        "- **enforce（命中即放行）尚未实现、未经复验，也未获负责人授权**——本清单"
        "**不构成**启用依据；请勿据本文件切 enforce。",
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
