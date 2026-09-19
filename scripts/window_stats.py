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
    authorized = [
        row[0]
        for row in q(
            "select external_group_id from provider_group_settings "
            "where action_enabled=1 order by external_group_id"
        )
    ]
    decisions = q(
        "select verdict, count(*) from shadow_decisions "
        "where created_at >= ? and created_at < ? group by verdict order by 2 desc",
        lo,
        hi,
    )
    by_kind = q(
        "select kind, verdict, count(*) from shadow_decisions "
        "where created_at >= ? and created_at < ? group by kind, verdict order by 3 desc",
        lo,
        hi,
    )
    intents = q(
        "select status, count(*) from action_intents "
        "where created_at >= ? and created_at < ? group by status order by 2 desc",
        lo,
        hi,
    )
    actions = q(
        "select action, ok, err_code, count(*) from action_logs "
        "where created_at >= ? and created_at < ? group by action, ok, err_code order by 4 desc",
        lo,
        hi,
    )
    per_group = q(
        "select external_group_id, "
        "sum(case when verdict='violation_high' then 1 else 0 end), count(*) "
        "from shadow_decisions where created_at >= ? and created_at < ? "
        "group by external_group_id order by 3 desc",
        lo,
        hi,
    )
    # 越界核查：窗口内判定所属的群，是否都在"动作已启用"的授权集合里
    outside = sorted({row[0] for row in per_group} - set(authorized))
    return {
        "window": {"start_utc": _iso(start), "end_utc": _iso(end), "semantics": "[start, end)"},
        "account": account,
        "authorized_action_groups": authorized,
        "decisions_by_verdict": decisions,
        "decisions_by_kind_verdict": by_kind,
        "action_intents_by_status": intents,
        "action_logs_by_action_ok": actions,
        "per_group": per_group,
        "boundary_check": {
            "groups_seen_outside_authorized": outside,
            "note": "越界=窗口内出现过判定、但其群未启用真实动作（应只记录、不动作）",
        },
        "dedupe": DEDUPE_NOTE,
        "failure_semantics": FAILURE_NOTE,
    }


def render(data: dict[str, object], *, deployment: str, prompt_version: str, db: Path) -> str:
    lines = [
        "# 固定 UTC 半开窗口统计（只读导出）",
        "",
        f"- 导出时刻（UTC）：{datetime.now(UTC).isoformat(timespec='seconds')}",
        f"- 数据库：`{db.name}`（只读连接，未写任何数据）",
        f"- 部署 SHA / 提示词版本：`{deployment}` / `{prompt_version}`",
        f"- 统计窗口：`{data['window']['start_utc']}` → `{data['window']['end_utc']}`"
        f"（{data['window']['semantics']}，UTC）",
        f"- 机器人账号：{data['account']}",
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
        "| 动作 | ok | err_code | 条数 |",
        "| --- | --- | --- | --- |",
    ]
    lines += [
        f"| {a} | {'成功' if ok else '失败'} | {code if code is not None else '-'} | {c} |"
        for a, ok, code, c in data["action_logs_by_action_ok"]
    ] or ["| （无） | - | - | 0 |"]
    lines += ["", "## 按群分布", "", "| 群 | violation_high | 判定总数 |", "| --- | --- | --- |"]
    lines += [f"| {g} | {vh} | {total} |" for g, vh, total in data["per_group"]]
    lines += [
        "",
        "## 越界核查",
        "",
        "- 窗口内出现过判定、但**未启用真实动作**的群："
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
    parser.add_argument("--start", default="2026-09-18T17:00:00Z", help="窗口起点（UTC，含）")
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
