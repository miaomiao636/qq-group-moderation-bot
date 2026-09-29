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

主审第十一轮（C04-R）追加：

- **dHash 只能找候选，不能首匹配替代身份**：provenance 的批次就是被核对的批次时，
  `(batch, no)` 是精确身份 → 必须核对那一行，缺失或 dHash 不符即失败，**不得改绑**；
  来源批次与 provenance 不同（合法重编号/重新批准链）时，先用候选集合，
  候选里只要存在**相反决定**就报歧义（不按行顺序取第一个）；全部候选一致才算明确，
  多个候选全放行记 `multiple_approved_identities`（多个批准身份，而非"唯一原图"）；
- **状态是枚举**：`state` 只认 `approved` / `rejected`（缺省键沿用历史默认 `rejected`），
  `null`/`true`/`7`/`""` 一律拒绝——否则同一条目能同时给放行与撤回开出 mismatch=0 的证明；
- **来源必须唯一可解释**：`review:甲;review:乙` 这类多批次来源报歧义，不取第一段。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.moderation.image_hash import dhash64, to_hex  # noqa: E402
from scripts.image_allowlist_seed import rejection_snapshot_path  # noqa: E402

DEFAULT_DB = ROOT / "data" / "moderation.db"
REVIEW_ROOT = ROOT / "docs" / "evidence" / "image-review"
OUT_DIR = ROOT / "docs" / "evidence" / "image-review"
NOTE = re.compile(r"(batch-\d{8}T\d{6}Z):no=(\d+)")
ROW = re.compile(r"^\| (\d+) \| (.*?) \| `(.*?)` \|")
HEX = re.compile(r"^[0-9a-f]+$")
BATCH_TOKEN = re.compile(r"(batch-\d{8}T\d{6}Z)")
# C04-R（主审第十一轮）：快照状态是**枚举**，不是"非 approved 即 rejected"。
STATE_ENUM = ("approved", "rejected")


def _state_of(entry: object) -> tuple[str, str]:
    """→ ``(状态, 问题)``——拒绝快照条目的状态**契约判定**。

    - 条目不是对象 → 问题（身份不完整，不能当"没有条目"）；
    - 缺少 `state` 键 → 沿用历史兼容默认 `rejected`（既有快照写的就是这个语义）；
    - 显式给出 `state` → 只认 `approved` / `rejected`；`null`/`true`/`7`/`""`/任意字符串
      一律**拒绝**，不能"在放行分支不报错、在撤回分支当撤回"（那样同一条目可以同时给
      放行与撤回开出 mismatch=0 的证明）。
    """
    if not isinstance(entry, dict):
        return "", "拒绝快照条目非法（不是对象）→ 身份不完整"
    if "state" not in entry:
        return "rejected", ""
    raw = entry.get("state")
    if raw not in STATE_ENUM:
        return "", f"拒绝快照状态非法（{raw!r}）→ 既不证明放行也不证明撤回"
    return str(raw), ""


def _source_batches(source: str) -> tuple[list[str], str]:
    """→ (来源里的**全部**批次名, 问题)。

    C04-R：来源必须**唯一可解释**——`re.search` 取第一段会把
    `review:批次甲;review:批次乙` 当成"完整来源通过"，从而忽略第二段的相反结论。
    """
    found = BATCH_TOKEN.findall(source or "")
    if not found:
        return [], "来源缺失/不可解析"
    unique = sorted(set(found))
    if len(unique) > 1:
        return unique, f"来源含多个批次（{'、'.join(unique)}）→ 无法唯一确定批次"
    return unique, ""


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

    from scripts.image_decision_authority import AuthorityError, observe_authority, snapshot_payload

    authority_error = ""
    try:
        authority_rows, rejection_state = observe_authority(db)
        allowed = [
            (key, row["note"], row["created_at"])
            for key, row in authority_rows.items()
            if row["enabled"]
        ]
        snapshot_raw = snapshot_payload(authority_rows)
    except AuthorityError as exc:
        authority_error = str(exc)
        allowed = []
        snapshot_raw = {}
        rejection_state = "authority_unavailable"

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
        phash: str,
        batch: Path | None,
        expect: str,
        *,
        source_note: str,
        no_hint: str | None,
        honor_hint: bool = False,
    ) -> dict[str, object]:
        """在该批次里定位当前图，再走完整链条（定位不唯一 → 问题）。

        C04-R（主审第十一轮）定位契约：

        - `honor_hint=True`（provenance 的批次**就是**被核对的批次）：`(batch, no)`
          是精确身份 → **必须核对该行**；该行缺失或 dHash 不符 → 失败，
          **不得**因为"同批次里还有一张 dHash 相同的图"就自动改绑到别的行；
        - `honor_hint=False`（来源批次与 provenance 批次不同，属合法重编号/重新批准链）：
          先形成**候选集合**（同批次内 dHash 相同的所有行）；**每一个被计入的候选都必须
          走完整身份链**（存在 → 清单 SHA-256 → 磁盘字节 → 实际 dHash → 人工结论，
          主审 C04-R2：只核验一张就不能声称"多个批准身份已核验"）；候选里存在**相反决定**
          或任一行缺证据 → 报**具体编号与原因**并非零；全部候选都完整且结论一致才算明确，
          候选多于一个时记 `multiple_approved_identities`（多个批准身份，而非"唯一原图"），
          逐候选结果放在 `candidates` 里并穿透到报告。
        """
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
        batch_dir: Path = batch

        def verify_entry(no: str) -> dict[str, object]:
            """**单行完整身份链**（主审 C04-R2）：清单行 → 文件名 → SHA-256 等级 →
            磁盘字节 → 实际 dHash → `DECISIONS.json` 人工结论。

            每个**被计入**的候选都必须走完这条链；缺证据（缺图 / 清单 SHA 不可用或
            与字节不符 / 不可解码 / 缺结论）一律算**不完整**，并在报告里给出编号与原因。
            """
            row: dict[str, object] = {"no": no, "problems": []}
            row_problems: list[str] = row["problems"]  # type: ignore[assignment]
            entry = entries[no]
            row["file"] = entry["file"]
            grade = _sha_grade(entry["sha"])
            if grade is None:
                row_problems.append(f"清单 SHA-256 不可用（{entry['sha'] or '空'}）")
            else:
                row["sha_grade"] = grade[1]
            expected_dhash = _clean(entry["dhash"])
            if len(expected_dhash) != 16 or not HEX.match(expected_dhash):
                row_problems.append(f"清单 dHash 不可用（{entry['dhash'] or '空'}）")
            picture = batch_dir / entry["file"]
            if not picture.is_file():
                row_problems.append("批次目录里缺这张原图（原件不在，身份无法证明）")
                return row
            raw = picture.read_bytes()
            full = hashlib.sha256(raw).hexdigest()
            row["file_sha256"] = full
            if grade is not None and not full.startswith(grade[0]):
                row_problems.append(
                    f"字节 SHA-256 与清单不符（清单 {grade[0][:12]}… 实际 {full[:12]}…）"
                )
            phash_now = dhash64(raw)
            if phash_now is None:
                row_problems.append("原图无法解码算哈希")
            else:
                actual = to_hex(phash_now)
                row["file_dhash"] = actual
                if expected_dhash and actual != expected_dhash:
                    row_problems.append(
                        f"清单 dHash 与实际不符（清单 {expected_dhash} vs 文件 {actual}）"
                    )
            decision = decisions.get(no)
            row["decision"] = decision or "（缺）"
            if decision != expect:
                row_problems.append(
                    f"人工结论是 {decision or '（缺）'}，与目标状态（{expect}）不一致"
                )
            return row

        if honor_hint and no_hint:
            target_no = str(int(no_hint))
            hinted = entries.get(target_no)
            if hinted is None:
                problems.append(
                    f"provenance 指向的编号 {target_no} 不在该批次清单里"
                    "（不得按 dHash 自动改绑到别的行）"
                )
                return item
            if _clean(hinted["dhash"]) != phash.lower():
                problems.append(
                    f"provenance 指向的编号 {target_no} 的 dHash 与生效行不一致"
                    "（不得按 dHash 自动改绑）"
                )
            rows = [verify_entry(target_no)]
        else:
            candidates = sorted(
                (no for no, row in entries.items() if _clean(row["dhash"]) == phash.lower()),
                key=int,
            )
            if not candidates:
                problems.append("该批次清单里找不到这张图（按 dHash 匹配）")
                return item
            item["candidate_identities"] = len(candidates)
            if len(candidates) > 1:
                item["multiple_approved_identities"] = candidates
            if no_hint:
                item["no_hint_not_bound"] = str(int(no_hint))
            # 候选集合路径：**每一个候选都走完整链**（只核验一张 = 不能声称"多身份已核验"）。
            rows = [verify_entry(no) for no in candidates]
            reversed_rows = [
                str(row["no"])
                for row in rows
                if decisions.get(str(row["no"])) not in (None, expect)
            ]
            if reversed_rows:
                problems.append(
                    "同哈希候选含相反决定（"
                    + "、".join(f"{no}={decisions.get(no)}" for no in reversed_rows)
                    + "）→ 不能用行顺序取第一个作为批准身份"
                )
        item["candidates"] = rows
        multi = len(rows) > 1
        for row in rows:
            for reason in row["problems"]:  # type: ignore[union-attr]
                problems.append(f"候选 {row['no']}：{reason}" if multi else str(reason))
        representative = next((row for row in rows if not row["problems"]), rows[0])
        item["no"] = representative["no"]
        for key in ("file", "sha_grade", "file_sha256", "file_dhash", "decision"):
            if key in representative:
                item[key] = representative[key]
        return item

    findings: list[dict[str, object]] = []
    for phash, note, created_at in allowed:
        key = str(phash).lower()
        match = NOTE.search(note or "")
        batch = REVIEW_ROOT / match.group(1) if match else None
        no_hint = str(int(match.group(2))) if match else None
        item = verify_by_hash(
            key, batch, "放行", source_note=note or "", no_hint=no_hint, honor_hint=True
        )
        item["created_at"] = created_at
        entry = snapshot_raw.get(key)
        # C04-R：**键存在**与"条目为 null"必须区分——`get()` 都返回 None，
        # 但显式 null 是非法条目（不得当成"没有拒绝记录"）。
        if key in snapshot_raw:
            state, state_problem = _state_of(entry)
            if state_problem:
                item["problems"].append(state_problem)  # type: ignore[union-attr]
            else:
                assert isinstance(entry, dict)
                source = str(entry.get("source") or "")
                item["snapshot_state"] = state
                if state == "approved":
                    batches, source_problem = _source_batches(source)
                    if source_problem:
                        item["problems"].append(  # type: ignore[union-attr]
                            f"快照记『已重新批准』但 {source_problem} → 身份不完整"
                        )
                    else:
                        chip = verify_by_hash(
                            key,
                            REVIEW_ROOT / batches[0],
                            "放行",
                            source_note=source,
                            no_hint=None,
                            honor_hint=False,
                        )
                        # C04-R2 #4：逐候选结果与多身份诊断必须**穿透**到报告，
                        # 不能只留 batch/no/decision（那会把候选核验证据丢掉）。
                        item["reapproval_chain"] = {
                            "batch": chip.get("batch"),
                            "no": chip.get("no"),
                            "decision": chip.get("decision"),
                            "candidate_identities": chip.get("candidate_identities"),
                            "multiple_approved_identities": chip.get(
                                "multiple_approved_identities"
                            ),
                            "candidates": chip.get("candidates"),
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
        state, state_problem = _state_of(entry)
        if state_problem:
            reject_findings.append({"phash": phash, "problems": [state_problem]})
            continue
        if state == "approved":
            continue
        assert isinstance(entry, dict)
        source = str(entry.get("source") or "")
        batches, source_problem = _source_batches(source)
        if source_problem:
            reject_findings.append(
                {"phash": phash, "problems": [f"撤回条目{source_problem} → 身份不完整"]}
            )
            continue
        reject_findings.append(
            verify_by_hash(
                phash.lower(),
                REVIEW_ROOT / batches[0],
                "撤回",
                source_note=source,
                no_hint=None,
                honor_hint=False,
            )
        )

    problems_overall = [f for f in findings + reject_findings if f["problems"]]
    if rejection_state in {"missing", "corrupt", "stale", "authority_unavailable"}:
        problems_overall.append(
            {
                "phash": "-",
                "problems": [
                    authority_error or f"派生导出 {rejection_state}；当前决定来自 DB，导出需重试"
                ],
            }
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
        "- 定位方式：provenance 给出编号时**核对那一行**（不得改绑）；来源无编号时用**候选集合**"
        "并要求**无相反决定**（全放行 = 多个批准身份），再核 清单 SHA-256 / dHash → "
        "磁盘字节 → `DECISIONS.json`（声明批次 + 结论）",
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
        candidates = item.get("candidates")
        if isinstance(candidates, list) and len(candidates) > 1:
            # C04-R2 #4：多候选时把**逐候选编号**一并写进报告（每个候选都已走完整链）。
            nos = "、".join(str(row.get("no")) for row in candidates if isinstance(row, dict))
            state_text += f"（候选 {len(candidates)} 行：{nos}）"
        lines.append(
            f"| `{item.get('phash', '-')}` | {item.get('batch', '-')} | {item.get('no', '-')} | "
            f"{item.get('file', '-')} | {item.get('decision', '-')} | {state_text} |"
        )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}"
    with (OUT_DIR / f"identity-{stamp}.md").open("x", encoding="utf-8") as output:
        output.write("\n".join(lines) + "\n")
    report_json = json.dumps(
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
    )
    with (OUT_DIR / f"identity-{stamp}.json").open("x", encoding="utf-8") as output:
        output.write(report_json)
    status = "IDENTITY_FAILED" if problems_overall else "IDENTITY_OK"
    print(
        f"{status} allowed={len(allowed)} (mismatch {len([f for f in findings if f['problems']])}) "
        f"rejected={len(reject_findings)} "
        f"(mismatch {len([f for f in reject_findings if f['problems']])}) "
        f"snapshot_state={rejection_state} → docs/evidence/image-review/identity-{stamp}.md"
    )
    return 1 if problems_overall else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
