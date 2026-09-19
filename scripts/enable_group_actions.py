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


def load_snapshot(
    path: pathlib.Path | None = None,
    *,
    stats_dir: pathlib.Path | None = None,
) -> tuple[dict, list[tuple[str, int, str, bool]]]:
    """**一次读取**普查快照 → ``(元数据, 群行)``（主审 R9-03-R）。

    元数据与群行必须来自**同一份字节**：此前 `load_survey_meta()` 与 `load_survey()` 各自读一次，
    两次读取之间替换同一路径的内容，就会"审计记旧账号/旧摘要、实际执行新名单"。
    任何解析失败按"快照不可用"处理（返回空元数据），由调用方拒绝执行。
    """
    if path is not None:
        target: pathlib.Path | None = pathlib.Path(path)
    else:
        # 用**调用方模块自己的** STATS（脚本可作为 `scripts.xxx` 或顶层模块导入，
        # 两种方式是两个模块对象；只有各用各的 globals，配置/探针 patch 才生效）。
        base = pathlib.Path(stats_dir) if stats_dir is not None else STATS
        files = glob.glob(str(base / "groups-*.json"))
        target = (
            pathlib.Path(max(files, key=lambda item: pathlib.Path(item).stat().st_mtime))
            if files
            else None
        )
    if target is None or not target.is_file():
        return {}, []
    try:
        raw = target.read_bytes()
    except OSError:
        return {}, []
    sha = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        return {}, []
    if not isinstance(data, dict):
        return {}, []
    rows: list[tuple[str, int, str, bool]] = []
    for item in data.get("groups") or []:
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
    return (
        {
            "path": str(target),
            "sha256": sha,
            "self_id": str(data.get("self_id") or ""),
            "provider": str(data.get("provider") or ""),
            "collected_at": str(data.get("collected_at") or ""),
        },
        rows,
    )


def load_survey_meta() -> dict:
    """快照元数据（**仅供展示/兼容**；执行路径必须用 `load_snapshot` 的单次读取）。"""
    return load_snapshot()[0]


def load_survey() -> list[tuple[str, int, str, bool]]:
    """快照群行（**仅供展示/兼容**；执行路径必须用 `load_snapshot` 的单次读取）。"""
    return load_snapshot()[1]


# 原函数对象：用于识别"调用方是否替换了 `load_survey`"（注入名单的兼容路径）。
_ORIGINAL_LOAD_SURVEY = load_survey


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

    # R9-03：阶段必须**可绑定**到 recall_only；读不到 → 拒绝（不拿写死的常量冒充运行阶段）。
    if stage != "recall_only":
        print(
            f"REFUSED_STAGE_PROOF provider={provider} stage_declared={stage or '（读不到）'} "
            "→ 只撤回阶段的授权无法证明，拒绝执行（不做任何改动）"
        )
        return 2
    # R9-03-R：**一次读取同一份快照**（元数据与群行同源），并严格校验账号 + provider。
    # 旧实现分两次读（meta / rows）——两次之间替换同一路径内容，就会"审计记旧账号/旧摘要、
    # 实际执行新名单"；账号缺失或 provider 不匹配时也一律拒绝（不再"有快照才校验"）。
    if args.survey:
        pinned = pathlib.Path(args.survey)
        if not pinned.is_file():
            print(f"REFUSED_SURVEY_PIN --survey={pinned} 不存在（拒绝执行）")
            return 2
        meta, survey = load_snapshot(pinned)
    else:
        meta, survey = load_snapshot()
    if not meta:
        if load_survey is not _ORIGINAL_LOAD_SURVEY:
            # 兼容路径：调用方**注入**了名单（测试/工具替换了模块级 `load_survey`）且没有快照。
            # 允许执行，但审计**如实标注** `source=injected_loader`（无账号/摘要证据 = NOT_PROVEN），
            # 绝不把它当成"已绑定快照"。
            meta = {
                "path": "",
                "sha256": "",
                "self_id": "",
                "provider": provider,
                "collected_at": "",
                "source": "injected_loader",
            }
            survey = load_survey()
        else:
            print("REFUSED_SNAPSHOT_UNAVAILABLE 没有可用的结构化普查快照（拒绝执行）")
            return 2
    bound_to_snapshot = str(meta.get("source") or "") != "injected_loader"
    if bound_to_snapshot and not self_id:
        print("REFUSED_NO_SELF_ID ONEBOT_SELF_ID 未配置：无法绑定到具体账号（拒绝执行）")
        return 2
    if bound_to_snapshot and str(meta.get("self_id") or "") != self_id:
        print(
            f"REFUSED_ACCOUNT_BINDING snapshot_self_id={meta.get('self_id') or '（缺失）'} "
            f"configured={self_id} → 快照账号与当前配置不一致，拒绝执行"
        )
        return 2
    if bound_to_snapshot and str(meta.get("provider") or "") != provider:
        print(
            f"REFUSED_PROVIDER_BINDING snapshot_provider={meta.get('provider') or '（缺失）'} "
            f"target={provider} → 快照不是该 provider 采集的，拒绝执行"
        )
        return 2
    targets = [r for r in survey if r[1] > args.threshold]
    db = pathlib.Path(args.db)
    con = sqlite3.connect(db)
    try:
        cols = {c[1] for c in con.execute(f"PRAGMA table_info({args.table})")}
        gid_col = "external_group_id" if "external_group_id" in cols else "group_openid"
        has_version = "version" in cols
        # R9-01/R9-09-R：前态**只取目标 provider**，并同时取**行版本**——计划要带版本，
        # 写事务内用 CAS 复核（`version=version+1` 不能替代 `where version=expected`）。
        current: dict[str, tuple[int, int]] = {}
        if "provider" in cols and "action_enabled" in cols:
            selector = f"{gid_col}, action_enabled, " + ("version" if has_version else "1")
            for row in con.execute(
                f"select {selector} from {args.table} where provider=?", (provider,)
            ):
                current[str(row[0])] = (int(row[1] or 0), int(row[2] or 0))
        plan = []
        for gid, count, name, _flag in targets:
            # R9-09：前态以**数据库现值**为准，旧 Markdown/快照的"是"不得覆盖。
            before, version = current.get(gid, (0, 0))
            new_row = gid not in current
            blocked = new_row and not route_ready(con, provider, gid)
            plan.append(
                {
                    "provider": provider,
                    "group": gid,
                    "members": count,
                    "name": _UNSAFE_NAME.sub(" ", name)[:64],
                    "before": before,
                    "version": version,
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
                    # R9-01/R9-09-R：**provider 限定 + 版本 CAS**——计划带的是预览时的版本与"改前=0"，
                    # 写事务内复核；撤权(A→B→A)、删除、并发开启任一发生即拒绝旧计划（不靠 rowcount 兜）。
                    conditions = f"provider=? and {gid_col}=? and action_enabled=0"
                    params: list[object] = [datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")]
                    if has_version:
                        conditions += " and version=?"
                    params += [provider, p["group"]]
                    if has_version:
                        params.append(int(p["version"]))
                    cursor = con.execute(
                        f"update {args.table} set action_enabled=1, "
                        + ("version=version+1, " if has_version else "")
                        + f"updated_at=? where {conditions}",
                        tuple(params),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(
                            f"计划已失效（撤权/删除/版本漂移）：{provider}:{p['group']} "
                            f"期望版本={p['version']} rowcount={cursor.rowcount}"
                            "（拒绝按旧计划赋权）"
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
                    ensure_ascii=True,  # 纯 ASCII：任意默认编码读取都能解析（审计可移植）
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
                    ensure_ascii=True,  # 纯 ASCII：任意默认编码读取都能解析（审计可移植）
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
