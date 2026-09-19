"""核对生效名单/拒绝快照与**负责人所审不可变文件**的身份关联（只读证据）。

主审第十轮（C04）后重写要点：

- **按哈希定位当前图**：不再用旧编号去猜新批次的事项——`note` 的批次+编号只作为 provenance，
  当前决定（快照 `approved` 来源）必须**在该批次里按 dHash 找到那张图**，再走完整链条：
  清单行 → 文件名 / SHA-256（12 或 64 位）/ dHash → **磁盘实际字节** → `DECISIONS.json`
  （声明批次 == 目录名、结论 == 期望值）；
- **不完整就是失败**：快照条目不是对象、`approved` 但来源为空、来源批次里找不到该图、
  原图缺失 → 一律计入 `mismatches` 并非零退出；
- **编号冲突拒绝**：清单与结论里的编号先规范化（`01` == `1`），归一化后冲突即报错，
  不 last-wins（与写入工具同一契约）；
- 拒绝快照 `corrupt` → 非零退出；**missing** 如实报告为"没有拒绝记录"，
  并声明本报告不覆盖拒绝链（不得写成"所有决定都已证明"）。
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
    """→ ``(值, 证据等级)``；长度不是 12/64 或非十六进制 → ``None``。"""
    value = _clean(cell)
    if len(value) not in (12, 64) or not HEX.match(value):
        return None
    return value, ("full_sha256" if len(value) == 64 else "prefix12_legacy")


def load_manifest(batch: Path) -> dict[str, dict[str, str]]:
    """→ ``{规范编号: {file, sha, dhash}}``；**归一化编号冲突**直接报错（不 last-wins）。"""
    entries: dict[str, dict[str, str]] = {}
    manifest = batch / "IMAGE_REVIEW.md"
    if not manifest.is_file():
        return entries
    for line in manifest.read_text(encoding="utf-8").splitlines():
        match = ROW.match(line)
        if not match:
            continue
        cells = [cell.strip() for cell in line.split("|")]
        key = str(int(match.group(1)))
        entry = {
            "file": match.group(3),
            "sha": cells[5] if len(cells) > 5 else "",
            "dhash": cells[6] if len(cells) > 6 else "",
        }
        previous = entries.get(key)
        if previous is not None and previous != entry:
            raise ValueError(f"清单里编号 {key} 归一化后冲突：{previous['file']} / {entry['file']}")
        entries[key] = entry
    return entries


def load_decisions(batch: Path) -> tuple[dict[str, str], str]:
    """→ (``{规范编号: 结论}``, `DECISIONS.json` 声明的批次)；编号冲突即报错。"""
    path = batch / "DECISIONS.json"
    if not path.is_file():
        return {}, ""
    raw = json.loads(path.read_text(encoding="utf-8"))
    decisions = raw.get("decisions") if isinstance(raw, dict) else None
    if not isinstance(decisions, dict):
        return {}, ""
    out: dict[str, str] = {}
    for no, verdict in decisions.items():
        key = str(int(no))
        previous = out.get(key)
        if previous is not None and previous != str(verdict):
            raise ValueError(f"结论里编号 {key} 归一化后冲突：{previous} / {verdict}")
        out[key] = str(verdict)
    return out, str(raw.get("batch") or "")


def load_snapshot_entries(db: Path) -> dict[str, object]:
    """**原始**拒绝快照条目（不做"enabled 掩码"）；损坏 → 空。"""
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

    cache: dict[Path, tuple[dict[str, dict[str, str]], dict[str, str], str, str]] = {}

    def batch_data(batch: Path) -> tuple[dict[str, dict[str, str]], dict[str, str], str, str]:
        """→ (清单行, 结论, 声明批次, 解析错误)。解析错误不抛给调用方，转为问题条目。"""
        if batch not in cache:
            error = ""
            try:
                entries = load_manifest(batch)
            except (OSError, ValueError) as exc:
                entries, error = {}, f"清单解析失败：{type(exc).__name__}: {exc}"
            decisions: dict[str, str] = {}
            declared = ""
            if not error:
                try:
                    decisions, declared = load_decisions(batch)
                except (OSError, ValueError, KeyError) as exc:
                    error = f"结论解析失败：{type(exc).__name__}: {exc}"
            cache[batch] = (entries, decisions, declared, error)
        return cache[batch]

    def verify_by_hash(
        phash: str, batch: Path | None, expect: str, *, source_note: str, no_hint: str | None
    ) -> dict[str, object]:
        """在该批次里**按哈希**定位当前图，再走完整链条（找不到 → 问题）。"""
        item: dict[str, object] = {
            "phash": phash,
            "expect": expect,
            "provenance": source_note,
            "no_hint": no_hint or "",
            "problems": [],
        }
        problems: list[str] = item["problems"]  # type: ignore[assignment]
        if batch is None:
            problems.append("来源里没有可识别的批次")
            return item
        item["batch"] = batch.name
        if args.batch and batch.name != Path(args.batch).name:
            problems.append(
                f"来源批次 {batch.name} 与 --batch 指定的 {Path(args.batch).name} 不一致"
                "（--batch 只作范围断言，不能替换 provenance）"
            )
            return item
        if not batch.is_dir():
            problems.append(f"批次目录不存在：{batch}")
            return item
        entries, decisions, declared, error = batch_data(batch)
        if error:
            problems.append(error)
            return item
        if declared != batch.name:
            problems.append(
                f"DECISIONS.json 声明的批次（{declared or '缺失'}）与目录名（{batch.name}）不一致"
            )
        target_no: str | None = None
        for candidate, entry in entries.items():
            if _clean(entry["dhash"]) == phash.lower():
                target_no = candidate
                break
        if target_no is None:
            problems.append("该批次清单里找不到这张图（按 dHash 匹配）")
            return item
        if no_hint and target_no != str(int(no_hint)):
            item["renumbered_from"] = str(int(no_hint))
        item["no"] = target_no
        entry = entries[target_no]
        item["file"] = entry["file"]
        grade = _sha_grade(entry["sha"])
        if grade is None:
            problems.append(f"清单 SHA-256 不可用（{entry['sha'] or '空'}）")
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
            actual = to_hex(phash_now)
            item["file_dhash"] = actual
            if expected_dhash and actual != expected_dhash:
                problems.append(f"清单 dHash 与实际不符（清单 {expected_dhash} vs 文件 {actual}）")
        decision = decisions.get(target_no)
        item["decision"] = decision or "（缺）"
        if decision != expect:
            problems.append(f"人工结论是 {decision or '（缺）'}，与目标状态（{expect}）不一致")
        return item

    findings: list[dict[str, object]] = []
    for phash, note, created_at in allowed:
        key = str(phash).lower()
        match = NOTE.search(note or "")
        batch = REVIEW_ROOT / match.group(1) if match else None
        no_hint = str(int(match.group(2))) if match else None
        item = verify_by_hash(key, batch, "放行", source_note=note or "", no_hint=no_hint)
        item["created_at"] = created_at
        entry = snapshot_raw.get(key)
        if entry is not None:
            if not isinstance(entry, dict):
                item["problems"].append("拒绝快照条目非法（不是对象）→ 身份不完整")  # type: ignore[union-attr]
            else:
                state = str(entry.get("state", "rejected"))
                source = str(entry.get("source") or "")
                item["snapshot_state"] = state
                if state == "approved":
                    match_source = re.search(r"(batch-\d{8}T\d{6}Z)", source)
                    if not source or match_source is None:
                        item["problems"].append(  # type: ignore[union-attr]
                            "快照记『已重新批准』但**来源缺失/不可解析** → 身份不完整"
                        )
                    else:
                        chip = verify_by_hash(
                            key,
                            REVIEW_ROOT / match_source.group(1),
                            "放行",
                            source_note=source,
                            no_hint=no_hint,
                        )
                        item["reapproval_chain"] = {
                            "batch": chip.get("batch"),
                            "no": chip.get("no"),
                            "decision": chip.get("decision"),
                        }
                        for problem in chip["problems"]:  # type: ignore[union-attr]
                            item["problems"].append(  # type: ignore[union-attr]
                                f"重新批准来源链：{problem}"
                            )
                elif state == "rejected":
                    item["problems"].append(  # type: ignore[union-attr]
                        "生效名单里 enabled=1，但拒绝快照仍记着『撤回』（双存储冲突）"
                    )
        findings.append(item)

    reject_findings: list[dict[str, object]] = []
    for phash, entry in sorted(snapshot_raw.items()):
        if not isinstance(entry, dict):
            reject_findings.append({"phash": phash, "problems": ["快照条目非法（不是对象）"]})
            continue
        if str(entry.get("state", "rejected")) == "approved":
            continue
        source = str(entry.get("source") or "")
        match = re.search(r"(batch-\d{8}T\d{6}Z)", source)
        if not source or match is None:
            reject_findings.append(
                {"phash": phash, "problems": ["撤回条目缺少可解析的来源 → 身份不完整"]}
            )
            continue
        reject_findings.append(
            verify_by_hash(
                phash.lower(),
                REVIEW_ROOT / match.group(1),
                "撤回",
                source_note=source,
                no_hint=None,
            )
        )

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
        "- 定位方式：**按 dHash 在该批次里找当前图**（编号可合法变更），再核 清单 SHA-256 / "
        "dHash → 磁盘字节 → `DECISIONS.json`（声明批次 + 结论）",
    ]
    if rejection_state == "missing":
        lines.append(
            "- 口径：拒绝快照**不存在**（missing）→ 本报告只覆盖生效名单，**不代表拒绝链已证明**"
        )
    lines.append("")
    if problems_overall:
        lines += ["## ⚠️ 未通过条目", ""]
        for item in problems_overall:
            lines.append(
                f"- `{item.get('phash', '-')}`：" + "；".join(str(x) for x in item["problems"])  # type: ignore[union-attr]
            )
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
