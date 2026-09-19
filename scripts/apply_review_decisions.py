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
    import_seeds,
    record_approval,
    record_rejection,
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

    def _prior_state() -> dict[str, int]:
        """本次涉及哈希的**导入前状态**（-1 = 库里没有该行），供失败补偿复位。"""
        state: dict[str, int] = {}
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            for value in [seed[3] for seed in approved] + sorted(rejected):
                row = con.execute(
                    "select enabled from image_allowlist where phash=?", (value,)
                ).fetchone()
                state[value] = -1 if row is None else int(row[0] or 0)
        finally:
            con.close()
        return state

    def _compensate(prior: dict[str, int]) -> None:
        """R9-05-R/R9-06-R：快照落盘失败 → **补偿回滚**已提交的 DB 变更。

        采用"先提交 DB（保住并发语义）+ 失败补偿"，等价于可恢复提交；
        不把 DB 事务悬在快照写盘期间——那会让并发操作撞上 SQLite 写锁。
        """
        con = sqlite3.connect(db)
        try:
            for value, before in prior.items():
                if before == -1:
                    con.execute("delete from image_allowlist where phash=?", (value,))
                else:
                    con.execute(
                        "update image_allowlist set enabled=?, "
                        "note = replace(replace(note, ';reapproved:2026-09-19', ''), "
                        "';excluded:2026-09-19', '') where phash=?",
                        (before, value),
                    )
            con.commit()
        finally:
            con.close()
        print(
            f"COMPENSATED_APPROVAL_ROLLBACK hashes={len(prior)}（决定快照写盘失败，已复位 DB 状态）"
        )

    prior = _prior_state()
    result = import_seeds(
        db=db,
        seeds=approved,
        dry_run=False,
        operator=OPERATOR,
        excluded=rejected,
        # 显式重新批准：只有这条路可以覆盖既有拒绝并重新启用
        reapproved={seed[3] for seed in approved},
    )
    try:
        _persist_decisions()
    except (OSError, ValueError, RuntimeError):
        _compensate(prior)
        raise
    added, duplicate, failed, skipped = result
    print(f"IMPORT_OK 新增={added} 已存在={duplicate} 失败={failed} 排除={skipped}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
