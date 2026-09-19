"""按人数阈值开启群真实动作（**provider 限定**、审计先落盘、账号/阶段绑定）。

主审 R9-01~03 / R9-09 整改要点：

- **R9-01**：采集、计划、前态、更新、审计、回滚**全部显式绑定 `(provider, external_group_id)`**——
  同号 `onebot` / `qq_official` 互不影响，回滚脚本绝不删另一个 provider 的同号行；
- **R9-02**：**审计先落盘、后提交**。审计写失败 → 事务回滚，绝不留下"已开启但没有证据"的群；
  每次执行**唯一 op id**，审计文件只增不改（同一秒重跑不会覆盖上一次非空审计）；
  输出区分"未提交 / 已提交但导出失败 / 完整成功"；
- **R9-09**：`UPDATE` 校验 `rowcount`（并发删除不得记成成功开启）；**新建行**必须"路由 + 归属"已就绪
  （与运行时 `resolve_action_provider` 同语义），未就绪 → **阻塞、不开、不自动建立跨通道映射**；
  "配置 flag 已开"与"执行就绪"分列输出，不再虚报；
- **R9-03**：真实动作**阶段**必须能绑定到 `recall_only`（读 环境变量 → `.env`），否则拒绝执行；
  普查快照的 self_id 必须与当前配置一致；审计写 `stage_declared` 并显式标
  `stage_runtime_proven=false` —— **不拿常量冒充运行阶段**。

用法：`--dry-run` 只读预览（不写库/不备份/不写审计）；`--execute` 才真正执行。
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import sys
import uuid
from datetime import UTC, datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.reports.backup import backup_sqlite  # noqa: E402

STATS = ROOT / "docs" / "evidence" / "stats"
DEFAULT_DB = ROOT / "data" / "moderation.db"
TARGET_PROVIDER = "onebot"
ROW = re.compile(r"^\| (\d+) \| (\d+) \| (.*?) \| (是|否) \|")
_UNSAFE_NAME = re.compile(r"[|\r\n\t]")


def _configured(key: str, default: str = "") -> str:
    """按 **环境变量 → `.env`** 顺序读取单个键（不构造 Settings，不打印其它键的值）。"""
    value = os.environ.get(key)
    if value:
        return value.strip()
    try:
        lines = (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return default
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, raw = stripped.partition("=")
        if name.strip() == key:
            return raw.strip().strip('"').strip("'")
    return default


def declared_stage() -> str:
    """**声明**的真实动作阶段（环境变量 → `.env`）；读不到返回空串（调用方必须拒绝执行）。"""
    return _configured("ONEBOT_ACTION_STAGE")


def configured_self_id() -> str:
    return _configured("ONEBOT_SELF_ID")


def _latest_survey_path() -> pathlib.Path | None:
    files = glob.glob(str(STATS / "groups-*.json"))
    if not files:
        return None
    return pathlib.Path(max(files, key=lambda p: pathlib.Path(p).stat().st_mtime))


def load_survey_meta() -> dict:
    """最新**结构化**普查快照的元数据（含文件 SHA-256）；无快照返回空字典。

    R9-03：执行侧不再解析不可信的显示文本，改为绑定结构化快照 + 账号 + 采集时刻。
    """
    path = _latest_survey_path()
    if path is None:
        return {}
    try:
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "self_id": str(data.get("self_id") or ""),
        "provider": str(data.get("provider") or ""),
        "collected_at": str(data.get("collected_at") or ""),
    }


def load_survey() -> list[tuple[str, int, str, bool]]:
    """读取最新普查快照的群列表 ``[(external_group_id, member_count, name, action_enabled)]``。

    只读**结构化 JSON**——群名里的换行/竖线不再可能注入一条新的执行目标。
    """
    path = _latest_survey_path()
    if path is None:
        return []
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return []
    rows: list[tuple[str, int, str, bool]] = []
    for item in (data.get("groups") if isinstance(data, dict) else None) or []:
        if not isinstance(item, dict):
            continue
        gid = str(item.get("external_group_id") or "")
        if not gid:
            continue
        rows.append(
            (
                gid,
                int(item.get("member_count") or 0),
                _UNSAFE_NAME.sub(" ", str(item.get("name") or "")),
                bool(item.get("action_enabled")),
            )
        )
    return rows


def required_columns(con: sqlite3.Connection, table: str) -> list[tuple[str, str, object]]:
    """返回**必填且无默认值**的列（用于自适应 INSERT）。"""
    out: list[tuple[str, str, object]] = []
    for _cid, name, ctype, notnull, default, pk in con.execute(f"PRAGMA table_info({table})"):
        if pk or not notnull or default is not None:
            continue
        out.append((name, (ctype or "TEXT").upper(), None))
    return out


def route_ready(con: sqlite3.Connection, provider: str, gid: str) -> bool:
    """与运行时 `app.core.routing.resolve_action_provider` **同语义**的只读判定。

    `onebot` 必须有 `group_provider_routes(provider, external_group_id)` 行且 `action_provider` 一致；
    归属行存在但与目标 provider 不一致 → 不可路由。表缺失按"未就绪"处理（fail-closed）。
    """
    try:
        owner = con.execute(
            "select provider from group_action_owners where external_group_id=?", (gid,)
        ).fetchone()
        if owner is not None and str(owner[0]) != provider:
            return False
        if owner is None:
            routes = {
                str(row[0])
                for row in con.execute(
                    "select message_provider from group_provider_routes where external_group_id=?",
                    (gid,),
                )
            }
            if len(routes) > 1 or (routes and provider not in routes):
                return False
        row = con.execute(
            "select action_provider from group_provider_routes "
            "where message_provider=? and external_group_id=?",
            (provider, gid),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None and str(row[0]) == provider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按人数阈值开启群真实动作（provider 限定）")
    parser.add_argument("--threshold", type=int, default=200)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--operator", default="负责人-2026-09-19")
    parser.add_argument("--provider", default=TARGET_PROVIDER)
    parser.add_argument("--table", default="provider_group_settings")
    parser.add_argument("--survey", default=None, help="显式绑定某份普查快照（默认取最新）")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not (args.dry_run or args.execute):
        parser.error("必须显式指定 --dry-run 或 --execute")

    provider = str(args.provider)
    stage = declared_stage()
    self_id = configured_self_id()
    meta = load_survey_meta()

    # R9-03：阶段必须**可绑定**到 recall_only；读不到 → 拒绝（不拿写死的常量冒充运行阶段）。
    if stage != "recall_only":
        print(
            f"REFUSED_STAGE_PROOF provider={provider} stage_declared={stage or '（读不到）'} "
            "→ 只撤回阶段的授权无法证明，拒绝执行（不做任何改动）"
        )
        return 2
    # R9-03：快照必须绑定到当前配置的账号（有快照才校验；无快照时按 NOT_PROVEN 记录）。
    if meta and meta.get("self_id") and self_id and meta["self_id"] != self_id:
        print(
            f"REFUSED_ACCOUNT_BINDING snapshot_self_id={meta['self_id']} configured={self_id} "
            "→ 普查快照不是当前账号采集的，拒绝执行"
        )
        return 2
    if args.survey:
        pinned = pathlib.Path(args.survey)
        if meta.get("path") and pathlib.Path(str(meta["path"])) != pinned:
            print(f"REFUSED_SURVEY_PIN --survey={pinned} 与最新快照 {meta.get('path')} 不一致")
            return 2

    survey = load_survey()
    targets = [r for r in survey if r[1] > args.threshold]
    db = pathlib.Path(args.db)
    con = sqlite3.connect(db)
    try:
        cols = {c[1] for c in con.execute(f"PRAGMA table_info({args.table})")}
        gid_col = "external_group_id" if "external_group_id" in cols else "group_openid"
        current: dict[str, int] = {}
        if "provider" in cols and "action_enabled" in cols:
            # R9-01：前态**只取目标 provider**——别的 provider 的 action_enabled 不能当作本行前态。
            current = {
                str(row[0]): int(row[1] or 0)
                for row in con.execute(
                    f"select {gid_col}, max(action_enabled) from {args.table} "
                    "where provider=? group by " + gid_col,
                    (provider,),
                )
            }
        plan = []
        for gid, count, name, _flag in targets:
            # R9-09：前态以**数据库现值**为准，旧 Markdown/快照的"是"不得覆盖。
            before = int(current.get(gid, 0))
            new_row = gid not in current
            blocked = new_row and not route_ready(con, provider, gid)
            plan.append(
                {
                    "provider": provider,
                    "group": gid,
                    "members": count,
                    "name": _UNSAFE_NAME.sub(" ", name)[:64],
                    "before": before,
                    "after": 1,
                    "new_row": new_row,
                    "blocked": blocked,
                    "blocked_reason": "new_row_without_route" if blocked else "",
                }
            )
        todo = [p for p in plan if p["blocked"] or p["before"] == 0 or p["new_row"]]
        blocked_items = [p for p in plan if p["blocked"]]
        print(
            f"[{provider}] 阈值 > {args.threshold} 人：目标 {len(plan)} 个群"
            f"（待开 {sum(1 for p in plan if p['before'] == 0)} 个，"
            f"新增行 {sum(1 for p in plan if p['new_row'])} 个，"
            f"其中**路由未就绪而阻塞** {len(blocked_items)} 个）"
        )
        for p in todo:
            mark = "阻塞(路由未就绪)" if p["blocked"] else "待开"
            print(
                f"  {p['group']} | {p['members']} 人 | {p['name'][:18]} | "
                f"改前={p['before']} 改后={p['after']} 新增行={p['new_row']} {mark}"
            )
        if args.dry_run:
            print("DRY_RUN_OK（未做任何改动）")
            return 0

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        op_id = uuid.uuid4().hex[:12]

        # 1) 一致性备份（在任何写库之前）
        target_backup = backup_sqlite(f"sqlite:///{db}")
        print(f"BACKUP_OK {target_backup}")

        # 2) 写库（先不提交）
        need_defaults = required_columns(con, args.table)
        changed: list[dict] = []
        try:
            for p in plan:
                if p["blocked"] or (p["before"] == 1 and not p["new_row"]):
                    continue
                if p["new_row"]:
                    row: dict[str, object] = {
                        "provider": provider,
                        gid_col: p["group"],
                        "action_enabled": 1,
                        "updated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f"),
                    }
                    if "name" in cols:
                        row["name"] = str(p["name"])[:64]
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
                    p["rowcount"] = 1
                else:
                    # R9-01/R9-09：**provider 限定** + 校验 rowcount（并发删除不得记成成功）。
                    cursor = con.execute(
                        f"update {args.table} set action_enabled=1, "
                        + ("version=version+1, " if "version" in cols else "")
                        + f"updated_at=? where provider=? and {gid_col}=?",
                        (
                            datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f"),
                            provider,
                            p["group"],
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(
                            f"计划行已不存在或状态漂移：{provider}:{p['group']} "
                            f"rowcount={cursor.rowcount}（不得记成成功开启）"
                        )
                    p["rowcount"] = cursor.rowcount
                changed.append(p)

            # 3) **审计先落盘**（R9-02）：写失败 → 回滚，绝不留下"已开启但无证据"的群。
            audit = STATS / f"enable-groups-{stamp}-{op_id}.plan.json"
            audit.parent.mkdir(parents=True, exist_ok=True)
            audit.write_text(
                json.dumps(
                    {
                        "op_id": op_id,
                        "status": "pending_commit",
                        "operator": args.operator,
                        "threshold": args.threshold,
                        "provider": provider,
                        "stage_declared": stage,
                        "stage_runtime_proven": False,
                        "self_id": self_id,
                        "survey": meta,
                        "changed": changed,
                        "blocked": blocked_items,
                        "backup": str(target_backup),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except BaseException:
            con.rollback()
            raise
        # 4) 提交
        con.commit()

        # 5) 结果审计（导出失败必须显式告知，不能当作完整成功）
        applied_path = STATS / f"enable-groups-{stamp}-{op_id}.applied.json"
        try:
            applied_path.write_text(
                json.dumps(
                    {
                        "op_id": op_id,
                        "status": "committed",
                        "operator": args.operator,
                        "threshold": args.threshold,
                        "provider": provider,
                        "stage_declared": stage,
                        "stage_runtime_proven": False,
                        "self_id": self_id,
                        "survey": meta,
                        "changed": changed,
                        "blocked": blocked_items,
                        "backup": str(target_backup),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            print(
                f"COMMITTED_BUT_AUDIT_EXPORT_FAILED op_id={op_id} plan={audit} "
                f"err={type(exc).__name__}（设置已提交，结果文件导出失败——按 op_id 找回真实结果）"
            )
            return 3

        print(
            f"APPLIED_OK op_id={op_id} 改动 {len(changed)} 个群；"
            f"阻塞 {len(blocked_items)} 个（新建行但路由未就绪，**未开启**）；审计 {audit}"
        )
        if blocked_items:
            print(
                "BLOCKED_NEW_ROWS provider="
                + provider
                + " groups="
                + ",".join(str(p["group"]) for p in blocked_items)
                + "（先经负责人授权建立群归属/路由，再重跑；脚本不会自动建立跨通道映射）"
            )
        rollback = [
            f"update {args.table} set action_enabled=0 where provider='{provider}' "
            f"and {gid_col}='{p['group']}';"
            if not p["new_row"]
            else f"delete from {args.table} where provider='{provider}' "
            f"and {gid_col}='{p['group']}';"
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
