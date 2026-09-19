"""固定 UTC 半开窗口统计导出（只读；主审 r132 要求）。

窗口语义：`[start, end)`——含起点、不含终点，全部按 UTC。
只读生产库，不写任何数据、不停服、不碰真实群。

用法：
    uv run python scripts/window_stats.py --start 2026-09-18T17:00:00Z --out docs/evidence/stats
不带 `--end` 时以"导出时刻"为窗口终点；默认窗口起点为本次部署时间。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 去重键：判定用 message_id，动作用幂等键（一条消息多动作只算一次）
DEDUPE_NOTE = "判定去重键=message_id；动作去重键=action_intents.idempotency_key / action_logs.id"
FAILURE_NOTE = (
    "`code 1200` 只表示 QQ 侧调用超时，**不等于客户端最终没有撤回**；"
    "失败列只能作为「请求失败」证据，不能推断最终效果。"
)


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" ")


def collect(*, db: Path, start: datetime, end: datetime) -> dict[str, object]:
    lo, hi = _iso(start), _iso(end)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    q = lambda sql, *a: con.execute(sql, a).fetchall()  # noqa: E731

    account = [
        line.split("=", 1)[1].strip()
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines()
        if line.startswith("ONEBOT_SELF_ID=")
    ]
    # A08：本账号口径——判定统计必须限定 `onebot:<self_id>:`（否则报告标一个账号、数字里混着另一个）
    self_id = account[0] if account else ""
    scope = f"onebot:{self_id}:%" if self_id else "%"
    # A08：授权必须按 **(provider, external_group_id)** 判定——同一群号在 qq_official 已授权、
    # 在 onebot 未授权时，不能被"只看群号"的集合掩盖。
    authorized_rows = q(
        "select provider, external_group_id from provider_group_settings "
        "where action_enabled=1 order by 1, 2"
    )
    authorized = [f"{p}:{g}" for p, g in authorized_rows]
    authorized_keys = {(str(p), str(g)) for p, g in authorized_rows}
    decisions = q(
        "select verdict, count(*) from shadow_decisions "
        "where created_at >= ? and created_at < ? and message_id like ? "
        "group by verdict order by 2 desc",
        lo,
        hi,
        scope,
    )
    by_kind = q(
        "select kind, verdict, count(*) from shadow_decisions "
        "where created_at >= ? and created_at < ? and message_id like ? "
        "group by kind, verdict order by 3 desc",
        lo,
        hi,
        scope,
    )
    # A08：另一个 self_id 的消息**不得混入**本账号统计——按 message_id 前缀显式分列。
    other_account = q(
        "select count(*) from shadow_decisions where created_at >= ? and created_at < ? "
        "and (? <> '' and message_id not like ?)",
        lo,
        hi,
        self_id,
        f"onebot:{self_id}:%",
    )
    intents = q(
        "select status, count(*) from action_intents "
        "where created_at >= ? and created_at < ? group by status order by 2 desc",
        lo,
        hi,
    )
    actions = q(
        "select action, ok, err_code, attempts, count(*) from action_logs "
        "where created_at >= ? and created_at < ? "
        "group by action, ok, err_code, attempts order by 5 desc",
        lo,
        hi,
    )
    per_group = q(
        "select provider, external_group_id, "
        "sum(case when verdict='violation_high' then 1 else 0 end), count(*) "
        "from shadow_decisions where created_at >= ? and created_at < ? and message_id like ? "
        "group by provider, external_group_id order by 4 desc",
        lo,
        hi,
        scope,
    )
    # A08 / R6-03：越界核查必须看**实际外发目标**，且与结果表**共用同一分类**：
    # `attempts > 0` = **已尝试发送**（失败/超时同样有发送尝试，**不能为清零而剔除**）→ 才算目标；
    # `attempts = 0` = **未发送**（急停/群开关/阶段拦截）→ 单列，**不进目标集合**；
    # `attempts IS NULL` = **无法判定** → 单列，也不进目标集合（零证据不当越界）。
    action_targets = q(
        "select provider, external_group_id, count(*) from action_logs "
        "where created_at >= ? and created_at < ? and attempts > 0 group by 1, 2 order by 3 desc",
        lo,
        hi,
    )
    not_sent_targets = q(
        "select provider, external_group_id, count(*) from action_logs "
        "where created_at >= ? and created_at < ? and attempts = 0 group by 1, 2 order by 3 desc",
        lo,
        hi,
    )
    unknown_attempt_targets = q(
        "select provider, external_group_id, count(*) from action_logs "
        "where created_at >= ? and created_at < ? and attempts is null group by 1, 2 order by 3 desc",
        lo,
        hi,
    )
    outside_action_targets = [
        f"{p}:{g}" for p, g, _c in action_targets if (str(p), str(g)) not in authorized_keys
    ]
    # 观察面（非越界）：窗口内出现过判定的群里，哪些**未开启真实动作**——只用于说明覆盖范围。
    # 必须按 (provider, group) 判定：同一群号在 qq_official 已授权，不能顺带给 onebot 授权。
    outside = sorted(
        {str(g) for p, g, _vh, _total in per_group if (str(p), str(g)) not in authorized_keys}
    )
    return {
        "window": {"start_utc": _iso(start), "end_utc": _iso(end), "semantics": "[start, end)"},
        "account": account,
        "self_id": self_id,
        "other_account_decisions": other_account[0][0] if other_account else 0,
        "authorized_action_groups": authorized,
        "decisions_by_verdict": decisions,
        "decisions_by_kind_verdict": by_kind,
        "action_intents_by_status": intents,
        "action_logs_by_action_ok": actions,
        # R6-04：动作日志的 message_id 是**原始消息 ID**（不是判定表的复合键 `onebot:<self_id>:<id>`），
        # 两个账号可以出现相同原始 ID——因此**不能**把判定表的 LIKE 前缀条件照搬到动作/意图表，
        # 那会把真实日志整片过滤掉或错误归属。此处**如实标明**：动作/意图为**全库（全局）统计**，
        # 含其它账号与无法归属部分，与本账号判定**分列**，不构成同一分子/分母。
        "action_scope": "global_unattributed",
        "action_not_sent_targets": not_sent_targets,
        "action_unknown_attempt_targets": unknown_attempt_targets,
        "per_group": per_group,
        "boundary_check": {
            "action_targets_outside_authorized": outside_action_targets,
            "groups_seen_outside_authorized": outside,
            "note": (
                "越界 = **已尝试外发**（`attempts > 0`）的动作目标（`action_logs` 的 `provider:group`）"
                "不在 `action_enabled=1` 的授权集合内——此项**必须为空**；`attempts=0`（未发送）"
                "与 `attempts IS NULL`（无法判定）单列、不计入目标集合；"
                "**注意**：这里比的是**导出时刻**的授权集合，只能说明与导出时的授权一致，"
                "**不能**证明动作发生时未越权（未绑定历史授权快照，该事实按 NOT_PROVEN 处理）；"
                '另："出现过判定但未开动作"只是观察面，不等于动作越界。'
            ),
        },
        "dedupe": DEDUPE_NOTE,
        "failure_semantics": FAILURE_NOTE,
    }


def _status_label(ok: object, code: object, attempts: object) -> str:
    """A08：真实"发送前跳过"（attempts=0）不是请求失败；失败与超时必须分开写。"""
    if int(attempts or 0) == 0:
        return "未发送（跳过/拦截）"
    if ok:
        return "成功"
    return "超时（最终效果未知）" if code == 1200 else "失败（最终效果未知）"


def render(data: dict[str, object], *, deployment: str, prompt_version: str, db: Path) -> str:
    lines = [
        "# 固定 UTC 半开窗口统计（只读导出）",
        "",
        f"- 导出时刻（UTC）：{datetime.now(UTC).isoformat(timespec='seconds')}",
        f"- 数据库：`{db.name}`（只读连接，未写任何数据）",
        f"- 部署 SHA / 提示词版本：`{deployment}` / `{prompt_version}`",
        f"- 统计窗口：`{data['window']['start_utc']}` → `{data['window']['end_utc']}`"
        f"（{data['window']['semantics']}，UTC）",
        f"- 机器人账号（self_id）：{data['account'] or '未配置'}",
        f"- 其它账号消息（**仅判定表**可分离）：{data['other_account_decisions']} 条"
        "（R6-04：判定表按 `onebot:<self_id>:` 分离；**动作/意图表**的 message_id 是**原始消息 ID**、"
        "两个账号可重复，无法安全归属 → 下表动作/意图为**全库（全局）统计**，含其它账号与不可归属部分，"
        "**不是**本账号判定的分子）",
        "- **动作状态语义**（A08）：`SKIPPED` = **未发送**（急停/群开关/阶段拦截，不是请求失败）；"
        "`ok=0` 且 `err_code` 为空 = 调用未成功但**最终效果未知**；`err_code=1200` = 调用超时"
        '（同样不等于客户端最终未撤回）。三类不得混写成同一种"失败"。',
        f"- 动作已启用群（{len(data['authorized_action_groups'])} 个）："
        + ", ".join(str(g) for g in data["authorized_action_groups"]),
        f"- 去重键：{data['dedupe']}",
        "",
        "## 判定（分母）",
        "",
        "| verdict | 条数 |",
        "| --- | --- |",
    ]
    lines += [f"| {v} | {c} |" for v, c in data["decisions_by_verdict"]] or ["| （无） | 0 |"]
    lines += ["", "### 按消息类型", "", "| kind | verdict | 条数 |", "| --- | --- | --- |"]
    lines += [f"| {k} | {v} | {c} |" for k, v, c in data["decisions_by_kind_verdict"]]
    lines += ["", "## 动作意图（状态）", "", "| status | 条数 |", "| --- | --- |"]
    lines += [f"| {s} | {c} |" for s, c in data["action_intents_by_status"]] or ["| （无） | 0 |"]
    lines += [
        "",
        "## 动作结果（分子）",
        "",
        "| 动作 | 结果 | err_code | attempts | 条数 |",
        "| --- | --- | --- | --- | --- |",
    ]
    lines += [
        f"| {a} | {_status_label(ok, code, attempts)} | {code if code is not None else '-'} | "
        f"{attempts} | {c} |"
        for a, ok, code, attempts, c in data["action_logs_by_action_ok"]
    ] or ["| （无） | - | - | - | 0 |"]
    lines += [
        "",
        "## 按群分布",
        "",
        "| provider | 群 | violation_high | 判定总数 |",
        "| --- | --- | --- | --- |",
    ]
    lines += [f"| {p} | {g} | {vh} | {total} |" for p, g, vh, total in data["per_group"]]
    lines += [
        "",
        "## 越界核查",
        "",
        "- **已尝试外发**（attempts>0）动作的目标（provider:group）不在授权集合内（必须为空）："
        + (
            ", ".join(str(g) for g in data["boundary_check"]["action_targets_outside_authorized"])
            or "**无**"
        ),
        "- **未发送**（attempts=0，拦截/跳过）的目标（单列、不算越界）："
        + (", ".join(f"{p}:{g}×{c}" for p, g, c in data["action_not_sent_targets"]) or "无"),
        "- **无法判定**（attempts 为空）的目标（单列、不算越界）："
        + (", ".join(f"{p}:{g}×{c}" for p, g, c in data["action_unknown_attempt_targets"]) or "无"),
        "- 出现过判定、但未启用真实动作的群（观察面）："
        + (
            ", ".join(str(g) for g in data["boundary_check"]["groups_seen_outside_authorized"])
            or "无"
        ),
        f"- 说明：{data['boundary_check']['note']}",
        "",
        "## 失败语义（不得混写）",
        "",
        f"- {data['failure_semantics']}",
        "",
        "## 可复算",
        "",
        "本文件由 `scripts/window_stats.py` 生成；同一窗口与同一数据库重跑可复现同样数字。",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="固定 UTC 半开窗口统计导出（只读）")
    parser.add_argument("--db", default=str(ROOT / "data" / "moderation.db"))
    parser.add_argument(
        "--start",
        default="2026-09-18T17:01:22Z",
        help=(
            "窗口起点（UTC，含）。默认=**已证明生效的时刻**（9ca1066 部署：新进程启动于 "
            "UTC 17:01:22，主审 A09：旧默认 17:00:00 会含部署前 82–88 秒）"
        ),
    )
    parser.add_argument("--end", default=None, help="窗口终点（UTC，不含）；缺省=导出时刻")
    parser.add_argument("--out", default=str(ROOT / "docs" / "evidence" / "stats"))
    parser.add_argument("--deployment-sha", default="9ca1066")
    parser.add_argument("--prompt-version", default=None)
    args = parser.parse_args(argv)

    prompt_version = args.prompt_version
    if prompt_version is None:
        env = (ROOT / ".env").read_text(encoding="utf-8")
        for line in env.splitlines():
            if line.startswith("AI_PROMPT_VERSION="):
                prompt_version = line.split("=", 1)[1].strip()

    start = _parse_utc(args.start)
    end = _parse_utc(args.end) if args.end else datetime.now(UTC)
    data = collect(db=Path(args.db), start=start, end=end)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"window-{start:%Y%m%dT%H%M%SZ}-{end:%Y%m%dT%H%M%SZ}.md"
    out.write_text(
        render(
            data,
            deployment=args.deployment_sha,
            prompt_version=prompt_version or "?",
            db=Path(args.db),
        ),
        encoding="utf-8",
    )
    print(f"WINDOW_STATS_OK {out}")
    print(json.dumps({"verdicts": data["decisions_by_verdict"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
