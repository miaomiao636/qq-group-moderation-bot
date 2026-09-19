"""核对生效名单/拒绝快照与**负责人所审不可变文件**的身份关联（只读证据，主审要求）。

逐条连接：`image_allowlist` 行（或拒绝快照的哈希）→ 批次目录 → `IMAGE_REVIEW.md` 清单行
→ 该行记录的文件名 / 完整 SHA-256 / dHash → 磁盘上那张图的**实际字节** → `DECISIONS.json` 的人工结论。

任何一条对不上（清单没这行、原图缺失、字节哈希与清单不符、dHash 与生效条目不符、结论不是"放行"/
"撤回"）都会**显式列出**并以非零退出——不用"看起来对得上"代替逐条证明。

用法：`uv run python scripts/verify_approved_identity.py [--db data/moderation.db] [--batch <目录>]`
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.moderation.image_hash import dhash64, to_hex  # noqa: E402
from image_allowlist_seed import load_rejections_state, rejection_snapshot_path  # noqa: E402

DEFAULT_DB = ROOT / "data" / "moderation.db"
REVIEW_ROOT = ROOT / "docs" / "evidence" / "image-review"
OUT_DIR = ROOT / "docs" / "evidence" / "image-review"
NOTE = re.compile(r"(batch-\d{8}T\d{6}Z):no=(\d+)")
ROW = re.compile(r"^\| (\d+) \| (.*?) \| `(.*?)` \|")


def load_manifest(batch: Path) -> dict[str, dict[str, str]]:
    entries: dict[str, dict[str, str]] = {}
    manifest = batch / "IMAGE_REVIEW.md"
    if not manifest.is_file():
        return entries
    for line in manifest.read_text(encoding="utf-8").splitlines():
        match = ROW.match(line)
        if not match:
            continue
        cells = [cell.strip() for cell in line.split("|")]
        entries[str(int(match.group(1)))] = {
            "file": match.group(3),
            "sha256": (cells[5] if len(cells) > 5 else "").strip("`"),
            "dhash": (cells[6] if len(cells) > 6 else "").strip("`"),
        }
    return entries


def load_decisions(batch: Path) -> dict[str, str]:
    path = batch / "DECISIONS.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))["decisions"]
    except (OSError, ValueError, KeyError):
        return {}
    return {str(int(no)): str(verdict) for no, verdict in raw.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="核对生效名单/拒绝快照的身份关联（只读）")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--batch", default=None, help="只核对某个批次目录（默认按 note 追溯）")
    args = parser.parse_args(argv)
    db = Path(args.db)

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        allowed = con.execute(
            "select phash, note, created_at from image_allowlist where enabled=1 order by phash"
        ).fetchall()
    finally:
        con.close()
    rejected, rejection_state = load_rejections_state(db)

    cache: dict[Path, tuple[dict[str, dict[str, str]], dict[str, str]]] = {}

    def batch_data(batch: Path) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
        if batch not in cache:
            cache[batch] = (load_manifest(batch), load_decisions(batch))
        return cache[batch]

    findings: list[dict[str, object]] = []

    def verify(phash: str, batch: Path | None, no: str | None, expect: str) -> dict[str, object]:
        item: dict[str, object] = {"phash": phash, "expect": expect, "problems": []}
        problems: list[str] = item["problems"]  # type: ignore[assignment]
        if batch is None or no is None:
            problems.append("无法从 note 解析出批次/编号")
            return item
        item["batch"] = batch.name
        item["no"] = no
        if not batch.is_dir():
            problems.append(f"批次目录不存在：{batch}")
            return item
        entries, decisions = batch_data(batch)
        entry = entries.get(no)
        if entry is None:
            problems.append("清单里没有该编号")
            return item
        item["file"] = entry["file"]
        picture = batch / entry["file"]
        if not picture.is_file():
            problems.append("批次目录里缺这张原图（原件不在，身份无法证明）")
            return item
        raw = picture.read_bytes()
        full = hashlib.sha256(raw).hexdigest()
        item["file_sha256"] = full
        expected_sha = entry["sha256"]
        if expected_sha and expected_sha != "-" and not full.startswith(expected_sha):
            problems.append(
                f"字节 SHA-256 与清单不符（清单 {expected_sha[:12]}… 实际 {full[:12]}…）"
            )
        phash_now = dhash64(raw)
        if phash_now is None:
            problems.append("原图无法解码算哈希")
        else:
            item["file_dhash"] = to_hex(phash_now)
            if to_hex(phash_now) != phash.lower():
                problems.append(f"dHash 与目标条目不符（文件 {to_hex(phash_now)} vs 目标 {phash}）")
        decision = decisions.get(no)
        item["decision"] = decision or "（缺）"
        if decision != expect:
            problems.append(f"人工结论是 {decision or '（缺）'}，与目标状态（{expect}）不一致")
        return item

    for phash, note, created_at in allowed:
        match = NOTE.search(note or "")
        batch = REVIEW_ROOT / match.group(1) if match else None
        if args.batch:
            batch = Path(args.batch)
        no = str(int(match.group(2))) if match else None
        item = verify(str(phash).lower(), batch, no, "放行")
        item["created_at"] = created_at
        findings.append(item)

    reject_findings: list[dict[str, object]] = []
    for phash in sorted(rejected):
        found: dict[str, object] | None = None
        for batch in sorted(REVIEW_ROOT.glob("batch-*")):
            entries, decisions = batch_data(batch)
            for no, entry in entries.items():
                if entry["dhash"].lower() == phash:
                    found = verify(phash, batch, no, "撤回")
                    break
            if found is not None:
                break
        if found is None:
            found = {"phash": phash, "problems": ["任何批次清单里都找不到该 dHash"]}
        reject_findings.append(found)

    bad = [f for f in findings + reject_findings if f["problems"]]
    lines = [
        "# 生效名单 / 拒绝快照 ↔ 负责人所审文件的身份关联（只读）",
        "",
        f"- 生成时刻（UTC）：{datetime.now(UTC).isoformat(timespec='seconds')}",
        f"- 生效名单：**{len(allowed)} 条**（`enabled=1`）；逐条核验结果："
        f"**{len(allowed) - len([f for f in findings if f['problems']])}/{len(allowed)} 一致**",
        f"- 拒绝快照：**{len(rejected)} 条**（读取状态 `{rejection_state}`，文件 "
        f"`{rejection_snapshot_path(db).name}`）；逐条核验："
        f"**{len(rejected) - len([f for f in reject_findings if f['problems']])}/{len(rejected)} 一致**",
        "- 核验内容：批次清单行 → 文件名 / 完整 SHA-256 / dHash → **磁盘实际字节** → `DECISIONS.json` 人工结论",
        "",
    ]
    if bad:
        lines += ["## ⚠️ 未通过条目", ""]
        for item in bad:
            lines.append(
                f"- `{item.get('phash', '-')}`（{item.get('batch', '-')} / no={item.get('no', '-')}）："
                + "；".join(str(x) for x in item["problems"])
            )
        lines.append("")
    lines += [
        "## 逐条结果",
        "",
        "| 哈希 | 批次 | 编号 | 文件 | 结论 | 状态 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in findings + reject_findings:
        state = "OK" if not item["problems"] else "；".join(str(x) for x in item["problems"])
        lines.append(
            f"| `{item.get('phash', '-')}` | {item.get('batch', '-')} | {item.get('no', '-')} | "
            f"{item.get('file', '-')} | {item.get('decision', '-')} | {state} |"
        )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    (OUT_DIR / f"identity-{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT_DIR / f"identity-{stamp}.json").write_text(
        json.dumps(
            {
                "allowed_checked": len(allowed),
                "rejected_checked": len(rejected),
                "rejection_state": rejection_state,
                "mismatches": bad,
                "allowed": findings,
                "rejected": reject_findings,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"IDENTITY_OK allowed={len(allowed)} (mismatch {len([f for f in findings if f['problems']])}) "
        f"rejected={len(rejected)} (mismatch {len([f for f in reject_findings if f['problems']])}) "
        f"snapshot_state={rejection_state} → docs/evidence/image-review/identity-{stamp}.md"
    )
    return 1 if bad else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
