"""落库负责人对图片审核清单的结论（**可验证、失败即停**）。

主审 R9-04 / R9-05 / R9-07 整改：

- **绑定字节身份**（R9-04）：按清单里的完整 SHA-256（兼容前 12 位写法）与 dHash 校验磁盘上的图；
  同名的图被换掉 → 明确失败，**绝不**把新字节批准进白名单；校验用**同一份字节**算出哈希，
  不再"先校验再二次读取"；
- **失败即停、不部分成功**（R9-04/R9-07）：原图缺失、编号不在清单、结论未定、
  清单里有图没给结论 → 打印 `APPLY_FAILED` 并以非零退出，**不做任何写库**；
- **编号是正整数**（R9-07）：第 100 张及以后（三位编号）同样能渲染、能落库；
- **区分普通重复导入与显式重新批准**（R9-05）：本工具是"显式重新批准"的入口——
  放行会写 `record_approval` 并重新启用既有行（保留撤回历史）；普通 seed 导入仍必须尊重拒绝。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.moderation.image_hash import dhash64, to_hex  # noqa: E402
from image_allowlist_seed import (  # noqa: E402
    decision_lock,
    import_seeds,
    record_approval,
    record_rejection,
    rejection_snapshot_path,
)

REVIEW_ROOT = ROOT / "docs" / "evidence" / "image-review"
DEFAULT_DB = ROOT / "data" / "moderation.db"
# 列序与 `image_review_export` 写的清单一致：
# 编号 | 状态 | 图片文件 | 来源 | 文件 SHA-256 | 文件 dHash | 命中种子 dHash | 距离 | 命中次数 | ...
ROW = re.compile(r"^\| (\d+) \| (.*?) \| `(.*?)` \|")
OPERATOR = "负责人-2026-09-19"


def load_manifest(batch: Path) -> dict[str, dict[str, str]]:
    """→ ``{编号(规范化为整数字符串): {file, status, source, sha256, dhash}}``。

    编号一律规范成整数写法（`01` 与 `1` 视为同一张），避免"清单写 01、结论写 1"互相找不到。
    """
    entries: dict[str, dict[str, str]] = {}
    for line in (batch / "IMAGE_REVIEW.md").read_text(encoding="utf-8").splitlines():
        match = ROW.match(line)
        if not match:
            continue
        cells = [cell.strip() for cell in line.split("|")]
        no = str(int(match.group(1)))
        entry = {
            "file": match.group(3),
            "status": match.group(2),
            "source": cells[4] if len(cells) > 4 else "",
            "sha256": (cells[5] if len(cells) > 5 else "").strip("`"),
            "dhash": (cells[6] if len(cells) > 6 else "").strip("`"),
        }
        previous = entries.get(no)
        if previous is not None and previous["sha256"] != entry["sha256"]:
            raise ValueError(
                f"清单里编号 {no} 重复且内容不一致（{previous['file']} / {entry['file']}）"
            )
        entries[no] = entry
    return entries


def load_decisions(batch: Path) -> dict[str, str]:
    """→ ``{规范编号: 结论}``。

    R9-07-R：编号先规范化（`01` 与 `1` 是同一张），**规范化后冲突**（如 `01=撤回` 与
    `1=放行` 同时出现）必须报错，不能静默 last-wins。
    """
    raw = json.loads((batch / "DECISIONS.json").read_text(encoding="utf-8"))["decisions"]
    out: dict[str, str] = {}
    for no, verdict in raw.items():
        key = str(int(no))
        previous = out.get(key)
        if previous is not None and previous != str(verdict):
            raise ValueError(f"结论里编号 {key} 出现冲突写法（{no}）：{previous} / {verdict}")
        out[key] = str(verdict)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="落库负责人审核结论（可验证、失败即停）")
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
    try:
        manifest = load_manifest(batch)
        decisions = load_decisions(batch)
    except (OSError, ValueError, KeyError) as exc:
        print(f"APPLY_FAILED 无法读取批次：{type(exc).__name__}: {exc}")
        return 4

    problems: list[str] = []
    approved: list[tuple[Path, str, str, str]] = []
    rejected: set[str] = set()
    for no in sorted(decisions, key=int):
        verdict = decisions[no]
        entry = manifest.get(no)
        if entry is None:
            problems.append(f"编号 {no} 不在清单里（未知编号）")
            continue
        picture = batch / entry["file"]
        if not picture.is_file():
            problems.append(f"编号 {no} 的原图缺失：{entry['file']}（**不得**算作成功）")
            continue
        try:
            raw = picture.read_bytes()  # 只读一次：校验与哈希用**同一份字节**
        except OSError as exc:
            problems.append(f"编号 {no} 读取失败：{type(exc).__name__}")
            continue
        full_sha = hashlib.sha256(raw).hexdigest()
        expected_sha = entry["sha256"].strip().lower()
        # R9-04-R：**身份必须完整可验**——只接受 12 位或 64 位十六进制；
        # 缺失、`-`、或任意长度（例如 1 位）一律拒绝：不得"跳过校验就当通过"。
        if len(expected_sha) not in (12, 64) or any(
            ch not in "0123456789abcdef" for ch in expected_sha
        ):
            problems.append(
                f"编号 {no} 的清单 SHA-256 不可用（{entry['sha256'] or '空'}）→ 拒绝批准"
            )
            continue
        if not full_sha.startswith(expected_sha):
            problems.append(
                f"编号 {no} 的文件已被替换：清单 SHA-256={expected_sha[:12]}… "
                f"实际={full_sha[:12]}…（拒绝批准）"
            )
            continue
        phash = dhash64(raw)
        if phash is None:
            problems.append(f"编号 {no} 无法解码算哈希：{entry['file']}")
            continue
        value = to_hex(phash)
        expected_dhash = entry["dhash"].strip().lower()
        if len(expected_dhash) != 16 or any(ch not in "0123456789abcdef" for ch in expected_dhash):
            problems.append(f"编号 {no} 的清单 dHash 不可用（{entry['dhash'] or '空'}）→ 拒绝批准")
            continue
        if value != expected_dhash:
            problems.append(f"编号 {no} 的 dHash 与清单不一致：{expected_dhash} vs {value}")
            continue
        if verdict == "放行":
            approved.append((picture, "review-2026-09-19", f"{batch.name}:no={no}", value))
        elif verdict == "撤回":
            rejected.add(value)
        else:
            problems.append(f"编号 {no} 的结论未定：{verdict}（要求处理但未处理）")

    pending = [no for no in manifest if no not in decisions]
    if pending:
        problems.append(
            f"清单里仍有 {len(pending)} 张未给结论：{','.join(sorted(pending, key=int))}"
            "（不得宣称整批成功）"
        )
    if problems:
        print("APPLY_FAILED 未做任何写入（不部分成功）：")
        for item in problems:
            print(f"  - {item}")
        return 4

    print(f"批次 {batch.name}：放行 {len(approved)} 张、撤回 {len(rejected)} 张")
    if args.dry_run:
        print("DRY_RUN_OK（未做任何改动）")
        return 0

    def _persist_decisions() -> None:
        for _picture, _source, _note, value in approved:
            record_approval(db, value, source=f"review:{batch.name}", operator=OPERATOR)
        for value in sorted(rejected):
            record_rejection(db, value, source=f"review:{batch.name}", operator=OPERATOR)

    def _row_state(con: sqlite3.Connection, value: str) -> tuple[int, int, str] | None:
        """``(id, enabled, note)``；行不存在 → ``None``。"""
        row = con.execute(
            "select id, enabled, note from image_allowlist where phash=?", (value,)
        ).fetchone()
        return None if row is None else (int(row[0] or 0), int(row[1] or 0), str(row[2] or ""))

    def _states() -> dict[str, tuple[int, int, str] | None]:
        """本次涉及哈希的**完整行状态**（id / enabled / note）——用于"只撤销自己那一份"。"""
        state: dict[str, tuple[int, int, str] | None] = {}
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            for value in [seed[3] for seed in approved] + sorted(rejected):
                state[value] = _row_state(con, value)
        finally:
            con.close()
        return state

    def _snapshot_state(value: str) -> str:
        """拒绝快照里该哈希**当前的决定状态**（``approved`` / ``rejected`` / 无条目 → 空串）。"""
        try:
            data = json.loads(rejection_snapshot_path(db).read_bytes().decode("utf-8"))
        except (OSError, ValueError):
            return ""
        entry = data.get(value) if isinstance(data, dict) else None
        return str(entry.get("state", "rejected")) if isinstance(entry, dict) else ""

    def _compensate(
        prior: dict[str, tuple[int, int, str] | None],
        credential: dict[str, tuple[int, int, str] | None],
    ) -> None:
        """C03/P2（主审第十/十一轮）：**只撤销本操作确实写入的那一份**。

        仍采用"先提交 DB（保住并发语义）+ 失败补偿"，但补偿必须满足：
        ① **归属凭据**（R1）：该行必须仍等于**本次写事务内**记录的状态（`credential`）才动手。
           `credential` 是写入事务自己看到的最终状态；提交后另起连接读到的三元组**不是**
           归属证明（外部删除/重建会被误认为"自己写的"）；
        ② 拒绝快照里已有矛盾决定 → 有更晚的成功决定，**不得覆盖**；
        ③ **提交前再校验**（R2）：恢复期间快照是否出现"后续成功决定"（DB 已提交、只剩 JSON
           写盘的另一次审核）→ 出现即 `ROLLBACK` 本次恢复并报冲突，不覆盖后续结果；
        ④ 恢复**精确的原 note 字符串**；恢复不完整如实报告，不假称已回滚。
        """
        reverted: list[str] = []
        conflicts: list[str] = []
        con = sqlite3.connect(db, timeout=10)
        try:
            con.execute("BEGIN IMMEDIATE")
            # 决策依据：**恢复开始前**读到的快照决定状态（提交前会再读一次比对漂移）。
            observed = {value: _snapshot_state(value) for value in prior}
            for value, before in prior.items():
                if _row_state(con, value) != credential.get(value):
                    conflicts.append(f"{value}:行状态不等于本次写入凭据（外部/后续写入，不撤销）")
                    continue
                # ② 若快照里的**现存决定**与"回滚后应有的状态"矛盾，说明有更晚的成功决定 → 不覆盖。
                target_enabled = 0 if before is None else int(before[1])
                entry_state = observed[value]
                if (entry_state == "approved" and target_enabled != 1) or (
                    entry_state == "rejected" and target_enabled != 0
                ):
                    conflicts.append(f"{value}:快照现存决定 state={entry_state} 与回滚结果矛盾")
                    continue
                if before is None:
                    con.execute("delete from image_allowlist where phash=?", (value,))
                else:
                    con.execute(
                        "update image_allowlist set enabled=?, note=? where phash=?",
                        (before[1], before[2], value),
                    )
                reverted.append(value)
            # ③ 提交前再校验：恢复期间是否被**后续成功决定**改写（例如 DB 已提交、只剩 JSON 写盘的审核）。
            drifted = [value for value in reverted if _snapshot_state(value) != observed[value]]
            if drifted:
                con.rollback()
                reverted = []
                conflicts.extend(
                    f"{value}:恢复期间快照出现后续决定（已放弃本次恢复，保留后续结果）"
                    for value in drifted
                )
            else:
                con.commit()
        finally:
            con.close()
        print(
            f"COMPENSATED_APPROVAL_ROLLBACK reverted={len(reverted)} conflicts={len(conflicts)}"
            "（决定快照写盘失败 → 只复位本操作拥有的行）"
        )
        if conflicts:
            print("COMPENSATION_CONFLICT " + "；".join(conflicts) + "（未完全回滚，需人工确认）")

    # C03-R2-2（主审第十二轮）：**前态读取 → DB 变更 → 决定快照发布 → 失败补偿**
    # 全部在同一把跨进程锁内（`decision_lock` 可重入，锁内的 `import_seeds` 不会自锁）。
    # 只锁补偿的最后一段挡不住"DB 已提交、只剩 JSON 写盘"的并发审核。
    with decision_lock(db):
        prior = _states()
        credential: dict[str, tuple[int, int, str] | None] = {}
        result = import_seeds(
            db=db,
            seeds=approved,
            dry_run=False,
            operator=OPERATOR,
            excluded=rejected,
            # 显式重新批准：只有这条路可以覆盖既有拒绝并重新启用
            reapproved={seed[3] for seed in approved},
            # C03-R1：写事务内产出归属凭据（不是提交后另读的近似状态）
            write_credential=credential,
        )
        try:
            _persist_decisions()
        except (OSError, ValueError, RuntimeError):
            _compensate(prior, credential)
            raise
    added, duplicate, failed, skipped = result
    print(f"IMPORT_OK 新增={added} 已存在={duplicate} 失败={failed} 排除={skipped}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
