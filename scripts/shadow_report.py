"""图片哈希 **shadow 观察**报告（只读）。

用法：
    uv run python scripts/shadow_report.py [--db data/moderation.db] [--limit 20]

口径（主审 R9-10 校准）：
- 只统计判定明细里**确实写了 `image_hash`** 的记录（即 shadow 生效期间）；
- **默认是"全库留存累计"**，不是某个时间窗：未按时间/账号/部署版本分离，
  因此**不得**用来计算 shadow 期间的覆盖率或图片命中率（报告里显式写明）；
  需要窗口口径时用 `--since/--until`（UTC 半开区间），此时所有计数共用同一范围；
- `matched=true` = 该图命中生效名单（距离 <= 阈值）；`would_allow=true` 是**观察器的原始信号**，
  **不是** enforce 的执行承诺（enforce 未实现、未授权，不能据此推断将来实际执行效果）；
- 多帧图只按**首帧**参与哈希（`frame_scope=first_frame`）——逐行与摘要都披露该限制；
- 本报告**只读**，不改变任何判定、不写库（除报告文件）。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs" / "evidence" / "stats"

SQL = """
select created_at, verdict, external_group_id,
       json_extract(detail_json, '$.image_hash.mode')         as mode,
       json_extract(detail_json, '$.image_hash.checked')      as checked,
       json_extract(detail_json, '$.image_hash.matched')      as matched,
       json_extract(detail_json, '$.image_hash.distance')     as distance,
       json_extract(detail_json, '$.image_hash.would_allow')  as would_allow,
       json_extract(detail_json, '$.image_hash.blocked_by')   as blocked_by,
       json_extract(detail_json, '$.image_hash.unavailable')  as unavailable,
       json_extract(detail_json, '$.image_hash.frame_scope')  as frame_scope,
       message_id
from shadow_decisions
where json_extract(detail_json, '$.image_hash') is not null
order by created_at desc
"""


def _cell(value: object) -> str:
    return "-" if value is None else str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="shadow 观察报告（只读）")
    parser.add_argument("--db", default=str(ROOT / "data" / "moderation.db"))
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--since", default=None, help="UTC 起始（含），如 2026-09-19 13:40:00")
    parser.add_argument("--until", default=None, help="UTC 结束（不含）")
    args = parser.parse_args(argv)

    db = Path(args.db)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        if args.since or args.until:
            lo, hi = args.since or "0000", args.until or "9999"
            rows = con.execute(f"{SQL} and created_at >= ? and created_at < ?", (lo, hi)).fetchall()
            scope = f"UTC 半开窗口 `{lo}` → `{hi}`（所有计数共用该范围）"
            windowed = True
        else:
            rows = con.execute(SQL).fetchall()
            scope = "**全库留存累计**（未按时间/账号/部署版本分离）"
            windowed = False
        total = con.execute("select count(*) from shadow_decisions").fetchone()[0]
        image_total = con.execute(
            "select count(*) from shadow_decisions where kind='image'"
        ).fetchone()[0]
    finally:
        con.close()

    modes = Counter(r[3] for r in rows)
    checked = [r for r in rows if int(r[4] or 0) > 0]
    matched = [r for r in rows if r[5] in (1, True)]
    would_allow = [r for r in matched if r[7] in (1, True)]
    unavailable = [r for r in rows if r[9]]
    unavailable_reasons = Counter(str(r[9]) for r in unavailable)
    first_frame = [r for r in rows if r[10] == "first_frame"]

    lines = [
        "# 图片哈希 shadow 观察报告（只读）",
        "",
        f"- 生成时刻（UTC）：{datetime.now(UTC).isoformat(timespec='seconds')}",
        f"- 数据库：`{db.name}`（只读连接）",
        f"- **统计范围**：{scope}",
        "- **口径警示**：本报告**不是**「窗口内覆盖率」的证据 —— 默认口径下分子（观察条数）与"
        f"分母（全库留存判定 {total} 条、其中图片判定 {image_total} 条）**不同范围**，"
        "不得相除得出覆盖率或命中率；需要窗口口径请用 `--since/--until` 重出。",
        f"- **有 `image_hash` 观察的判定**：{len(rows)} 条（全库留存累计判定 {total} 条，"
        f"其中图片判定 {image_total} 条；两者**不同范围**，不可相除）",
        f"- 模式分布：{dict(modes) or '（无）'}",
        f"- **命中（matched）**：{len(matched)} 条；其中 **would_allow**：{len(would_allow)} 条"
        "（raw 观察信号，**不是** enforce 执行承诺——enforce 未实现、未授权）",
        f"- 检查过附件的判定：{len(checked)} 条；标记 unavailable：{len(unavailable)} 条",
    ]
    if unavailable_reasons:
        lines.append(
            "- **unavailable 原因分布**："
            + ", ".join(
                f"`{reason}`×{count}" for reason, count in unavailable_reasons.most_common()
            )
            + "（异常观察**保留并单列**，不当成命中，也不隐藏）"
        )
    if first_frame:
        lines.append(
            f"- **多帧图（`frame_scope=first_frame`）**：{len(first_frame)} 条 —— "
            "**仅按首帧参与哈希**，命中**不代表整段动画等价**，不能据此推断整图可放行"
        )
    lines += [
        "",
        f"## 最近 {args.limit} 条观察",
        "",
        "| 时间 | 群 | 原判定 | mode | checked | matched | 距离 | would_allow | blocked_by | "
        "unavailable | 首帧 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows[: args.limit]:
        lines.append(
            "| "
            + " | ".join(
                _cell(x)
                for x in (
                    r[0],
                    r[2],
                    r[1],
                    r[3],
                    r[4],
                    r[5],
                    r[6],
                    r[7],
                    r[8],
                    r[9],
                    "是" if r[10] == "first_frame" else "-",
                )
            )
            + " |"
        )
    if matched:
        lines += [
            "",
            "## 命中样本（raw 观察信号，**不是** enforce 放行承诺）",
            "",
            "以下逐条列出原始观察字段；`would_allow` 只是**观察器的候选信号**，"
            "enforce 未实现、未复验、未获授权，**不得**当作「将来会放行」的证明。",
            "",
        ]
        for r in matched[:20]:
            scope_note = "（多帧图：仅首帧参与哈希）" if r[10] == "first_frame" else ""
            lines.append(
                f"- `{r[0]}` 群 {r[2]} 原判 {r[1]} 距离 {_cell(r[6])} "
                f"would_allow={_cell(r[7])} blocked_by={_cell(r[8])} "
                f"unavailable={_cell(r[9])}{scope_note}"
            )
    out = OUT_DIR / f"shadow-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"SHADOW_REPORT_OK {out}")
    print(
        json.dumps(
            {
                "observations": len(rows),
                "matched": len(matched),
                "would_allow": len(would_allow),
                "modes": dict(modes),
                "scope": "window" if windowed else "all_retained",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
