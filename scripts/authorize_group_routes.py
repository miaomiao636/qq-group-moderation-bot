"""按负责人授权，为**已开启真实动作的群**补齐 `(provider, 群)` 归属与路由（B 方案）。

背景（主审 R9-09 实测）：`provider_group_settings.action_enabled=1` 只是"配置位"，
运行时能否真正外发动作由 `app.core.routing.resolve_action_provider` 决定——
`onebot` 群**必须**有 `group_provider_routes(message_provider='onebot', action_provider='onebot')` 行，
否则返回 `None`（fail-closed）。2026-09-19 的 67 个已开群里只有 13 个可路由，
另外 54 个是"配置位开了但实际不会执行"。

本工具**只做一件事**：在负责人明确授权下，为这些群插入**缺失的**归属/路由行。
安全约束（与主审 R9-01/02/03/09 同标准）：

- 全程绑定 `(provider, external_group_id)`，**绝不**触碰其它 provider 的行；
- 发现冲突（同群已有别的 provider 归属/路由，或 onebot 路由指向别的出口）→ **跳过并报告**，
  绝不为"跑绿"而覆盖或建立跨通道映射；
- **审计先落盘再提交**；唯一 op id；审计文件只增不改；
- 提交前用与运行时同语义的 `route_ready` 逐群复核，任一未就绪 → **整体回滚**；
- 阶段必须能绑定到 `recall_only`；输出 REPLACED/INSERTED 与**逐行可执行的回滚 SQL**。

用法：`--dry-run`（只读预览）或 `--execute`，且必须 `--authorized-by "<负责人授权说明>"`。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
import uuid
from datetime import UTC, datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.reports.backup import backup_sqlite  # noqa: E402
from enable_group_actions import (  # noqa: E402
    STATS,
    configured_self_id,
    declared_stage,
    load_snapshot,
    load_survey,
    load_survey_meta,
    route_ready,
)

# 兼容锚点：外部/探针仍可能 monkeypatch 这两个名字；执行路径已统一改为 `load_snapshot()` 单次读取。
_COMPAT_SURVEY_LOADERS = (load_survey, load_survey_meta)

DEFAULT_DB = ROOT / "data" / "moderation.db"
TARGET_PROVIDER = "onebot"
OWNERS = "group_action_owners"
ROUTES = "group_provider_routes"


def _conflicts(con: sqlite3.Connection, provider: str, gid: str) -> list[str]:
    """该群上**不能自动处理**的冲突（必须人工决定，不得脚本硬来）。"""
    problems: list[str] = []
    owner = con.execute(
        f"select provider from {OWNERS} where external_group_id=?", (gid,)
    ).fetchone()
    if owner is not None and str(owner[0]) != provider:
        problems.append(f"已有归属 provider={owner[0]}（与目标 {provider} 不一致）")
    # B01：官方通道的**隐式默认**也是有效归属——`resolve_action_provider` 在"无 owner、
    # 无 route 行"时对 `qq_official` 仍返回 `qq_official`。因此该群只要存在**其它 provider
    # 的 settings 行**（官方通道在用），插入 OneBot 的共享 owner 就会把它从 `qq_official`
    # 变成 `None` —— 即使没有 UPDATE 任何官方行，也改变了它的有效路由。
    other_settings = con.execute(
        "select count(*) from provider_group_settings "
        "where provider<>? and external_group_id=? and action_enabled=1",
        (provider, gid),
    ).fetchone()[0]
    if other_settings:
        problems.append(
            f"该群有其它 provider 的已启用 settings 行（{other_settings} 条）→"
            " 共享 owner 会撤销其隐式默认归属"
        )
    others = {
        str(row[0])
        for row in con.execute(
            f"select message_provider from {ROUTES} where external_group_id=? and message_provider<>?",
            (gid, provider),
        )
    }
    if others:
        problems.append(f"已有其它通道路由 {sorted(others)}（再插 {provider} 会变成多通道歧义）")
    existing = con.execute(
        f"select action_provider from {ROUTES} where message_provider=? and external_group_id=?",
        (provider, gid),
    ).fetchone()
    if existing is not None and str(existing[0]) != provider:
        problems.append(f"已有 {provider} 路由但出口指向 {existing[0]}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按负责人授权补齐群归属/路由（provider 限定）")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--threshold", type=int, default=200)
    parser.add_argument("--authorized-by", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not (args.dry_run or args.execute):
        parser.error("必须显式指定 --dry-run 或 --execute")

    provider = TARGET_PROVIDER
    stage = declared_stage()
    self_id = configured_self_id()
    if stage != "recall_only":
        print(f"REFUSED_STAGE_PROOF stage_declared={stage or '（读不到）'} → 拒绝执行")
        return 2
    if args.execute and not args.authorized_by.strip():
        print("REFUSED_NO_AUTHORIZATION 缺少 --authorized-by（负责人授权说明必须留档）")
        return 2
    # R9-03-R：与 enable 同一套**单次读取 + 严格身份绑定**（元数据与群行同源；账号/provider 缺失即拒绝）。
    meta, snapshot_rows = load_snapshot(stats_dir=STATS)
    if not meta:
        print("REFUSED_SNAPSHOT_UNAVAILABLE 没有可用的结构化普查快照（拒绝执行）")
        return 2
    if not self_id:
        print("REFUSED_NO_SELF_ID ONEBOT_SELF_ID 未配置：无法绑定到具体账号（拒绝执行）")
        return 2
    if str(meta.get("self_id") or "") != self_id:
        print(
            f"REFUSED_ACCOUNT_BINDING snapshot_self_id={meta.get('self_id') or '（缺失）'} "
            f"configured={self_id} → 快照账号与当前配置不一致，拒绝执行"
        )
        return 2
    if str(meta.get("provider") or "") != provider:
        print(
            f"REFUSED_PROVIDER_BINDING snapshot_provider={meta.get('provider') or '（缺失）'} "
            f"target={provider} → 快照不是该 provider 采集的，拒绝执行"
        )
        return 2
    members = {gid: count for gid, count, _name, _flag in snapshot_rows}

    db = pathlib.Path(args.db)
    con = sqlite3.connect(db)
    try:
        enabled = [
            str(row[0])
            for row in con.execute(
                "select external_group_id from provider_group_settings "
                "where provider=? and action_enabled=1 order by external_group_id",
                (provider,),
            )
        ]
        targets, conflicts, already = [], [], []
        planned_versions: dict[str, int] = {}
        for gid in enabled:
            # R9-09-R：`--threshold` 与快照必须**真正参与选择**——不在快照内、或人数不达阈值
            # 的群一律列阻塞（不能把"未来新增的 action_enabled 行"自动解释成本次旧授权）。
            if gid not in members:
                conflicts.append((gid, ["不在最新普查快照内（无法证明在批准的人数范围内）"]))
                continue
            if members[gid] <= args.threshold:
                conflicts.append((gid, [f"快照人数 {members[gid]} <= 阈值 {args.threshold}"]))
                continue
            found = _conflicts(con, provider, gid)
            if found:
                conflicts.append((gid, found))
                continue
            if route_ready(con, provider, gid):
                already.append(gid)
                continue
            row = con.execute(
                "select action_enabled, version from provider_group_settings "
                "where provider=? and external_group_id=?",
                (provider, gid),
            ).fetchone()
            if row is None or int(row[0] or 0) != 1:
                conflicts.append((gid, ["当前不是已启用状态（不在赋权范围）"]))
                continue
            planned_versions[gid] = int(row[1] or 0)
            targets.append(gid)

        missing_members = [gid for gid in targets if gid not in members]
        print(
            f"[{provider}] 已开真实动作 {len(enabled)} 群：可路由 {len(already)}、"
            f"待补归属/路由 {len(targets)}、冲突跳过 {len(conflicts)}"
        )
        if missing_members:
            print(
                f"  提示：{len(missing_members)} 个待补群不在最新普查快照里"
                "（仍将补路由；快照只是人数参考，不构成授权依据）"
            )
        for gid in targets:
            size = members.get(gid)
            print(f"  + {gid} | 人数 {size if size is not None else '未知'} | 插入 owner+route")
        for gid, found in conflicts:
            print(f"  ! {gid} | 跳过：{'；'.join(found)}")

        if args.dry_run:
            print("DRY_RUN_OK（未做任何改动）")
            return 0

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        op_id = uuid.uuid4().hex[:12]
        target_backup = backup_sqlite(f"sqlite:///{db}")
        print(f"BACKUP_OK {target_backup}")

        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
        inserted_owners, inserted_routes = [], []
        try:
            # C01/P1（主审第十轮）：**先取真正的写锁**再复核——默认连接下此前的 SELECT 并不开启
            # 写事务，复核与首次 INSERT 之间别的连接仍能提交撤权/跨通道冲突。
            # `BEGIN IMMEDIATE` 立刻拿写锁，使"复核 → 赋权 → 提交"整体序列化。
            con.execute("BEGIN IMMEDIATE")
            # R9-09-R / B01：写事务内**逐目标重新复核**资格与完整归属——
            # 预览→备份之间可能已被撤权（版本变化）或并发插入别的 provider 路由。
            # 必须在**任何写入之前**完成，冲突先阻塞、绝不"先写 owner 把冲突消解"。
            for gid in targets:
                row = con.execute(
                    "select action_enabled, version from provider_group_settings "
                    "where provider=? and external_group_id=?",
                    (provider, gid),
                ).fetchone()
                if (
                    row is None
                    or int(row[0] or 0) != 1
                    or int(row[1] or 0) != planned_versions[gid]
                ):
                    raise RuntimeError(f"目标在预览后已被撤权/变更：{gid}（拒绝按旧计划补路由）")
                found = _conflicts(con, provider, gid)
                if found:
                    raise RuntimeError(f"目标在预览后出现归属冲突：{gid}：{'；'.join(found)}")
            for gid in targets:
                owner = con.execute(
                    f"select provider from {OWNERS} where external_group_id=?", (gid,)
                ).fetchone()
                if owner is None:
                    cursor = con.execute(
                        f"insert into {OWNERS} (external_group_id, provider) values (?, ?)",
                        (gid, provider),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(f"归属行插入未生效：{gid}")
                    inserted_owners.append(gid)
                route = con.execute(
                    f"select action_provider from {ROUTES} "
                    "where message_provider=? and external_group_id=?",
                    (provider, gid),
                ).fetchone()
                if route is None:
                    cursor = con.execute(
                        f"insert into {ROUTES} "
                        "(external_group_id, message_provider, action_provider, updated_at) "
                        "values (?, ?, ?, ?)",
                        (gid, provider, provider, now),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(f"路由行插入未生效：{gid}")
                    inserted_routes.append(gid)

            # 提交前逐群复核（与运行时同语义）：未全就绪 → 整体回滚
            not_ready = [gid for gid in targets if not route_ready(con, provider, gid)]
            if not_ready:
                raise RuntimeError(f"提交前复核未就绪：{','.join(not_ready)}")

            audit = STATS / f"authorize-groups-{stamp}-{op_id}.plan.json"
            audit.parent.mkdir(parents=True, exist_ok=True)
            audit.write_text(
                json.dumps(
                    {
                        "op_id": op_id,
                        "status": "pending_commit",
                        "authorized_by": args.authorized_by.strip(),
                        "provider": provider,
                        "stage_declared": stage,
                        "stage_runtime_proven": False,
                        "self_id": self_id,
                        "survey": meta,
                        "backup": str(target_backup),
                        "inserted_owners": inserted_owners,
                        "inserted_routes": inserted_routes,
                        "already_routable": already,
                        "skipped_conflicts": [
                            {"group": gid, "reasons": found} for gid, found in conflicts
                        ],
                        "verified_routable": len(targets),
                    },
                    ensure_ascii=True,  # 纯 ASCII：任意默认编码读取都能解析（审计可移植）
                    indent=2,
                ),
                encoding="utf-8",
            )
        except BaseException:
            con.rollback()
            raise
        con.commit()

        applied = STATS / f"authorize-groups-{stamp}-{op_id}.applied.json"
        try:
            applied.write_text(
                json.dumps(
                    {
                        "op_id": op_id,
                        "status": "committed",
                        "authorized_by": args.authorized_by.strip(),
                        "provider": provider,
                        "backup": str(target_backup),
                        "inserted_owners": inserted_owners,
                        "inserted_routes": inserted_routes,
                        "total_routable": len(already) + len(targets),
                        "total_enabled": len(enabled),
                        "skipped_conflicts": [
                            {"group": gid, "reasons": found} for gid, found in conflicts
                        ],
                    },
                    ensure_ascii=True,  # 纯 ASCII：任意默认编码读取都能解析（审计可移植）
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            print(
                f"COMMITTED_BUT_AUDIT_EXPORT_FAILED op_id={op_id} plan={audit} "
                f"err={type(exc).__name__}（变更已提交，结果文件导出失败——按 op_id 找回真实结果）"
            )
            return 3

        print(
            f"AUTHORIZE_OK op_id={op_id} 新增归属 {len(inserted_owners)} 行、"
            f"路由 {len(inserted_routes)} 行；现在可路由 "
            f"{len(already) + len(targets)}/{len(enabled)} 群（阶段仍 {stage}）；审计 {audit}"
        )
        if conflicts:
            print(
                "SKIPPED_CONFLICTS="
                + ",".join(gid for gid, _ in conflicts)
                + "（需人工决定，脚本不覆盖、不建立跨通道映射）"
            )
        print("ROLLBACK_SQL:")
        for gid in inserted_routes:
            print(
                f"  delete from {ROUTES} where message_provider='{provider}' "
                f"and external_group_id='{gid}';"
            )
        for gid in inserted_owners:
            print(
                f"  delete from {OWNERS} where external_group_id='{gid}' and provider='{provider}';"
            )
        return 0
    finally:
        con.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
