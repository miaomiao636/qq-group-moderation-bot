"""核对生效名单/拒绝快照与**负责人所审不可变文件**的身份关联（只读证据）。

主审 R9 复核（N-IDENTITY）后重写，逐条连接并**强校验**：

- 生效名单行 → `note` 里的**来源批次 + 编号**（provenance，不因 `--batch` 而改变）；
- 该批次的 `IMAGE_REVIEW.md` 行 → 文件名 / **清单 SHA-256（必须 12 或 64 位十六进制）** /
  **清单 dHash（必须 16 位十六进制）**；
- 磁盘上那张图的**实际字节** → SHA-256（按清单长度匹配）与 dHash **都必须一致**；
- `DECISIONS.json` 的 `batch` 必须等于批次目录名、该编号的结论必须与目标状态一致；
- 拒绝快照条目**按它自己的 `source` 追溯**（不得"扫到任意旧批次的撤回来充当证据"）；
  `enabled=1` 的行若快照写着 `rejected`、或 `approved` 的来源批次结论不是放行，一律报冲突；
- 拒绝快照 `corrupt` → **非零退出**，不得宣称"整体成功"；
- `--batch` 只作**范围断言**（凡来源批次不等于它的行即报冲突），绝不覆盖 provenance。

任何一条对不上都会列进 `mismatches` 并以非零退出。
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
HEX = re.compile(r"^[0-9a-f]+$")


def _clean(cell: str) -> str:
    return (cell or "").strip().strip("`").strip().lower()


def _sha_grade(cell: str) -> tuple[str, str] | None:
    """→ ``(值, 证据等级)``；长度不是 12/64 或非十六进制 → ``None``（不可作为身份证据）。"""
    value = _clean(cell)
    if len(value) not in (12, 64) or not HEX.match(value):
        return None
    return value, ("full_sha256" if len(value) == 64 else "prefix12_legacy")


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
            "sha": cells[5] if len(cells) > 5 else "",
            "dhash": cells[6] if len(cells) > 6 else "",
        }
    return entries


def load_decisions(batch: Path) -> tuple[dict[str, str], str]:
    """→ (``{编号: 结论}``, `DECISIONS.json` 里声明的批次名)。"""
    path = batch / "DECISIONS.json"
    if not path.is_file():
        return {}, ""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, ""
    decisions = raw.get("decisions") if isinstance(raw, dict) else None
    if not isinstance(decisions, dict):
        return {}, ""
    return {str(int(no)): str(verdict) for no, verdict in decisions.items()}, str(
        raw.get("batch") or ""
    )


def load_snapshot_entries(db: Path) -> dict[str, dict]:
    """**原始**拒绝快照条目（不做"enabled 掩码"，provenance 用）；损坏 → 空。"""
    path = rejection_snapshot_path(db)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): v for k, v in data.items()} if isinstance(data, dict) else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="核对生效名单/拒绝快照的身份关联（只读）")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument(
        "--batch", default=None, help="只作**范围断言**：来源批次不等于它的行报冲突"
    )
    args = parser.parse_args(argv)
    db = Path(args.db)

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        allowed = con.execute(
            "select phash, note, created_at from image_allowlist where enabled=1 order by phash"
        ).fetchall()
    finally:
        con.close()
    _rejected_set, rejection_state = load_rejections_state(db)
    snapshot_raw = load_snapshot_entries(db)

    cache: dict[Path, tuple[dict[str, dict[str, str]], dict[str, str], str]] = {}

    def batch_data(batch: Path) -> tuple[dict[str, dict[str, str]], dict[str, str], str]:
        if batch not in cache:
            decisions, declared = load_decisions(batch)
            cache[batch] = (load_manifest(batch), decisions, declared)
        return cache[batch]

    def verify(
        phash: str, batch: Path | None, no: str | None, expect: str, *, source_note: str
    ) -> dict[str, object]:
        item: dict[str, object] = {
            "phash": phash,
            "expect": expect,
            "provenance": source_note,
            "problems": [],
        }
        problems: list[str] = item["problems"]  # type: ignore[assignment]
        if batch is None or no is None:
            problems.append("无法从来源解析出批次/编号")
            return item
        item["batch"] = batch.name
        item["no"] = no
        if args.batch and batch.name != Path(args.batch).name:
            problems.append(
                f"来源批次 {batch.name} 与 --batch 指定的 {Path(args.batch).name} 不一致"
                "（--batch 只作范围断言，不能替换 provenance）"
            )
            return item
        if not batch.is_dir():
            problems.append(f"批次目录不存在：{batch}")
            return item
        entries, decisions, declared = batch_data(batch)
        if declared != batch.name:
            problems.append(
                f"DECISIONS.json 声明的批次（{declared or '缺失'}）与目录名（{batch.name}）不一致"
            )
        entry = entries.get(no)
        if entry is None:
            problems.append("清单里没有该编号")
            return item
        item["file"] = entry["file"]
        grade = _sha_grade(entry["sha"])
        if grade is None:
            problems.append(f"清单 SHA-256 不可用（{entry['sha'] or '空'}）→ 不能证明批准字节")
        else:
            item["sha_grade"] = grade[1]
        expected_dhash = _clean(entry["dhash"])
        if len(expected_dhash) != 16 or not HEX.match(expected_dhash):
            problems.append(f"清单 dHash 不可用（{entry['dhash'] or '空'}）")
        picture = batch / entry["file"]
        if not picture.is_file():
            problems.append("批次目录里缺这张原图（原件不在，身份无法证明）")
            return item
        raw = picture.read_bytes()
        full = hashlib.sha256(raw).hexdigest()
        item["file_sha256"] = full
        if grade is not None and not full.startswith(grade[0]):
            problems.append(f"字节 SHA-256 与清单不符（清单 {grade[0][:12]}… 实际 {full[:12]}…）")
        phash_now = dhash64(raw)
        if phash_now is None:
            problems.append("原图无法解码算哈希")
        else:
            actual_dhash = to_hex(phash_now)
            item["file_dhash"] = actual_dhash
            if actual_dhash != phash.lower():
                problems.append(f"dHash 与目标条目不符（文件 {actual_dhash} vs 目标 {phash}）")
            if expected_dhash and actual_dhash != expected_dhash:
                problems.append(
                    f"清单 dHash 与实际不符（清单 {expected_dhash} vs 文件 {actual_dhash}）"
                )
        decision = decisions.get(no)
        item["decision"] = decision or "（缺）"
        if decision != expect:
            problems.append(f"人工结论是 {decision or '（缺）'}，与目标状态（{expect}）不一致")
        return item

    findings: list[dict[str, object]] = []
    for phash, note, created_at in allowed:
        match = NOTE.search(note or "")
        batch = REVIEW_ROOT / match.group(1) if match else None
        no = str(int(match.group(2))) if match else None
        item = verify(str(phash).lower(), batch, no, "放行", source_note=note or "")
        item["created_at"] = created_at
        # 快照里对**已启用**行的最新声明必须与来源批次结论一致（R9-05-R）
        entry = snapshot_raw.get(str(phash).lower())
        if isinstance(entry, dict):
            state = str(entry.get("state", "rejected"))
            source = str(entry.get("source") or "")
            item["snapshot_state"] = state
            match_source = re.search(r"(batch-\d{8}T\d{6}Z)", source)
            if state == "rejected":
                findings_problem = "生效名单里 enabled=1，但拒绝快照仍记着『撤回』（双存储冲突）"
                item["problems"].append(findings_problem)  # type: ignore[union-attr]
            elif state == "approved" and match_source:
                src_batch = REVIEW_ROOT / match_source.group(1)
                src_decisions, _declared = load_decisions(src_batch)
                src_verdict = src_decisions.get(no or "")
                if src_verdict != "放行":
                    item["problems"].append(  # type: ignore[union-attr]
                        f"快照记『已重新批准』，但其来源批次 {src_batch.name} 对该编号的结论是 "
                        f"{src_verdict or '（缺）'}"
                    )
        findings.append(item)

    reject_findings: list[dict[str, object]] = []
    for phash, entry in sorted(snapshot_raw.items()):
        if not isinstance(entry, dict) or str(entry.get("state", "rejected")) == "approved":
            continue
        source = str(entry.get("source") or "")
        match = re.search(r"(batch-\d{8}T\d{6}Z)", source)
        no_match = NOTE.search(f"review:{source}:no=1")  # 快照 source 不含编号 → 由哈希反查
        batch = REVIEW_ROOT / match.group(1) if match else None
        no: str | None = None
        if batch is not None and batch.is_dir():
            for candidate, row in load_manifest(batch).items():
                if _clean(row["dhash"]) == phash.lower():
                    no = candidate
                    break
        _ = no_match
        item = verify(phash.lower(), batch, no, "撤回", source_note=source)
        reject_findings.append(item)

    problems_overall = [f for f in findings + reject_findings if f["problems"]]
    if rejection_state == "corrupt":
        problems_overall.append(
            {"phash": "-", "problems": ["拒绝快照损坏/不可读 → 不能宣称整体成功"]}
        )
    lines = [
        "# 生效名单 / 拒绝快照 ↔ 负责人所审文件的身份关联（只读）",
        "",
        f"- 生成时刻（UTC）：{datetime.now(UTC).isoformat(timespec='seconds')}",
        f"- 生效名单：**{len(allowed)} 条**（`enabled=1`）；逐条核验："
        f"**{len(allowed) - len([f for f in findings if f['problems']])}/{len(allowed)} 一致**",
        f"- 拒绝快照：**{len(reject_findings)} 条**（读取状态 `{rejection_state}`，文件 "
        f"`{rejection_snapshot_path(db).name}`）；逐条核验："
        f"**{len(reject_findings) - len([f for f in reject_findings if f['problems']])}/"
        f"{len(reject_findings)} 一致**",
        "- 核验内容：来源批次清单行 → 文件名 / **清单 SHA-256（12 或 64 位）** / **清单 dHash**"
        " → **磁盘实际字节** → `DECISIONS.json`（结论 + 声明批次）→ 快照最新决定来源",
        "",
    ]
    if problems_overall:
        lines += ["## ⚠️ 未通过条目", ""]
        for item in problems_overall:
            lines.append(
                f"- `{item.get('phash', '-')}`：" + "；".join(str(x) for x in item["problems"])
            )  # type: ignore[union-attr]
        lines.append("")
    lines += [
        "## 逐条结果",
        "",
        "| 哈希 | 批次 | 编号 | 文件 | 结论 | 状态 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in findings + reject_findings:
        state_text = "OK" if not item["problems"] else "；".join(str(x) for x in item["problems"])  # type: ignore[union-attr]
        lines.append(
            f"| `{item.get('phash', '-')}` | {item.get('batch', '-')} | {item.get('no', '-')} | "
            f"{item.get('file', '-')} | {item.get('decision', '-')} | {state_text} |"
        )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    (OUT_DIR / f"identity-{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT_DIR / f"identity-{stamp}.json").write_text(
        json.dumps(
            {
                "allowed_checked": len(allowed),
                "rejected_checked": len(reject_findings),
                "rejection_state": rejection_state,
                "mismatches": problems_overall,
                "allowed": findings,
                "rejected": reject_findings,
            },
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"IDENTITY_OK allowed={len(allowed)} (mismatch {len([f for f in findings if f['problems']])}) "
        f"rejected={len(reject_findings)} "
        f"(mismatch {len([f for f in reject_findings if f['problems']])}) "
        f"snapshot_state={rejection_state} → docs/evidence/image-review/identity-{stamp}.md"
    )
    return 1 if problems_overall else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
