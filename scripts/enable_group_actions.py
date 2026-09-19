"""按人数阈值**开启群真实动作**（负责人 2026-09-19 授权：>200 人、只撤回、不排除任何群）。

安全设计（顺序固定，任何一步失败即中止）：
1. **一致性备份**数据库（`app.reports.backup.backup_sqlite`，WAL 安全 + quick_check）；
2. **dry-run 表**：逐群打印 `群号 | 人数 | 群名 | 改前 | 改后 | 是否新增行`；
3. 执行：对目标群 `INSERT OR UPDATE action_enabled=1`（按表结构自适应补 NOT NULL 列）；
4. **逐群审计记录**：写入 `docs/evidence/stats/enable-groups-<ts>.json`（谁/何时/改前改后）；
5. 打印**一键回滚 SQL**（把本次改动的行恢复原状 / 删除本次新增的行）。

用法：
    uv run python scripts/enable_group_actions.py --threshold 200 --dry-run   # 只看
    uv run python scripts/enable_group_actions.py --threshold 200 --execute    # 真执行
"""

from __future__ import annotations

import argparse
import glob
import json
import pathlib
import re
import sqlite3
import sys
from datetime import UTC, datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.reports.backup import backup_sqlite  # noqa: E402

STATS = ROOT / "docs" / "evidence" / "stats"
DEFAULT_DB = ROOT / "data" / "moderation.db"
ROW = re.compile(r"^\| (\d+) \| (\d+) \| (.*?) \| (是|否) \|")


def load_survey() -> list[tuple[str, int, str, bool]]:
    latest = max(
        glob.glob(str(STATS / "groups-*.md")), key=lambda p: pathlib.Path(p).stat().st_mtime
    )
    rows: list[tuple[str, int, str, bool]] = []
    for line in pathlib.Path(latest).read_text(encoding="utf-8").splitlines():
        m = ROW.match(line)
        if m:
            rows.append((m.group(1), int(m.group(2)), m.group(3), m.group(4) == "是"))
    return rows


def required_columns(con: sqlite3.Connection, table: str) -> list[tuple[str, str, object]]:
    """返回**必填且无默认值**的列（用于自适应 INSERT）。"""
    out: list[tuple[str, str, object]] = []
    for _cid, name, ctype, notnull, default, pk in con.execute(f"PRAGMA table_info({table})"):
        if pk or not notnull or default is not None:
            continue
        out.append((name, (ctype or "TEXT").upper(), None))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按人数阈值开启群真实动作")
    parser.add_argument("--threshold", type=int, default=200)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--operator", default="负责人-2026-09-19")
    parser.add_argument("--table", default="provider_group_settings")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not (args.dry_run or args.execute):
        parser.error("必须显式指定 --dry-run 或 --execute")

    survey = load_survey()
    targets = [r for r in survey if r[1] > args.threshold]
    db = pathlib.Path(args.db)
    con = sqlite3.connect(db)
    try:
        cols = {c[1] for c in con.execute(f"PRAGMA table_info({args.table})")}
        gid_col = "external_group_id" if "external_group_id" in cols else "group_openid"
        current: dict[str, int] = {}
        if "provider" in cols and "action_enabled" in cols:
            current = {
                str(row[0]): int(row[1] or 0)
                for row in con.execute(
                    f"select {gid_col}, max(action_enabled) from {args.table} group by {gid_col}"
                )
            }
        plan = []
        for gid, count, name, enabled_flag in targets:
            before = 1 if enabled_flag else int(current.get(gid, 0))
            plan.append(
                {
                    "group": gid,
                    "members": count,
                    "name": name,
                    "before": before,
                    "after": 1,
                    "new_row": gid not in current,
                }
            )
        print(
            f"阈值 > {args.threshold} 人：目标 {len(plan)} 个群"
            f"（其中待开 {sum(1 for p in plan if p['before'] == 0)} 个，"
            f"新增行 {sum(1 for p in plan if p['new_row'])} 个）"
        )
        for p in plan:
            if p["before"] == 0 or p["new_row"]:
                print(
                    f"  {p['group']} | {p['members']} 人 | {p['name'][:18]} | "
                    f"改前={p['before']} 改后=1 新增行={p['new_row']}"
                )
        if args.dry_run:
            print("DRY_RUN_OK（未做任何改动）")
            return 0

        # 1) 一致性备份
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        # `backup_sqlite(database_url)`：一致性备份（SQLite Connection.backup + quick_check），
        # 落在 <库目录>/backups/ 下，文件名自带时间戳。
        target_backup = backup_sqlite(f"sqlite:///{db}")
        print(f"BACKUP_OK {target_backup}")

        # 2) 执行
        need_defaults = required_columns(con, args.table)
        changed = []
        for p in plan:
            if p["before"] == 1 and not p["new_row"]:
                continue
            if p["new_row"]:
                # 主键是 **(provider, external_group_id)**：两者都是"必填无默认"，
                # 却会被"跳过主键列"的通用逻辑漏掉（上一版就是这么报 NOT NULL 的）→ **显式给全**。
                row: dict[str, object] = {
                    "provider": "onebot",
                    gid_col: p["group"],
                    "action_enabled": 1,
                    "updated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f"),
                }
                if "name" in cols:
                    row["name"] = p["name"][:64]
                if "moderation_enabled" in cols:
                    row["moderation_enabled"] = 1
                if "version" in cols:
                    row["version"] = 1
                for name, ctype, _d in need_defaults:  # 兜底：其它"必填且无默认"列
                    if name in row:
                        continue
                    row[name] = 0 if ctype.startswith("INT") else ""
                keys = ", ".join(row)
                marks = ", ".join("?" for _ in row)
                con.execute(
                    f"insert into {args.table} ({keys}) values ({marks})", tuple(row.values())
                )
            else:
                con.execute(
                    f"update {args.table} set action_enabled=1 where {gid_col}=?", (p["group"],)
                )
            changed.append(p)
        con.commit()
        audit = STATS / f"enable-groups-{stamp}.json"
        audit.parent.mkdir(parents=True, exist_ok=True)
        audit.write_text(
            json.dumps(
                {
                    "operator": args.operator,
                    "threshold": args.threshold,
                    "stage": "recall_only",
                    "changed": changed,
                    "backup": str(target_backup),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"APPLIED_OK 改动 {len(changed)} 个群；审计 {audit}")
        rollback = [
            f"update {args.table} set action_enabled=0 where {gid_col}='{p['group']}';"
            if not p["new_row"]
            else f"delete from {args.table} where {gid_col}='{p['group']}';"
            for p in changed
        ]
        print("ROLLBACK_SQL:")
        for line in rollback:
            print("  " + line)
        return 0
    finally:
        con.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
