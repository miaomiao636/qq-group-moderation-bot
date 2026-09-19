"""图片哈希 **shadow 观察**报告（只读）。

用法：
    uv run python scripts/shadow_report.py [--db data/moderation.db] [--limit 20]

口径：
- 只统计判定明细里**确实写了 `image_hash`** 的记录（即 shadow 生效期间）；
- `matched=true` 表示该图命中生效名单（距离 <= 阈值），`would_allow=true` 表示"若开 enforce 就会被放行"；
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
       json_extract(detail_json, '$.image_hash.mode')        as mode,
       json_extract(detail_json, '$.image_hash.checked')     as checked,
       json_extract(detail_json, '$.image_hash.matched')     as matched,
       json_extract(detail_json, '$.image_hash.distance')    as distance,
       json_extract(detail_json, '$.image_hash.would_allow') as would_allow,
       json_extract(detail_json, '$.image_hash.blocked_by')  as blocked_by,
       json_extract(detail_json, '$.image_hash.unavailable') as unavailable,
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
    args = parser.parse_args(argv)

    db = Path(args.db)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(SQL).fetchall()
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

    lines = [
        "# 图片哈希 shadow 观察报告（只读）",
        "",
        f"- 生成时刻（UTC）：{datetime.now(UTC).isoformat(timespec='seconds')}",
        f"- 数据库：`{db.name}`（只读连接）",
        f"- **有 `image_hash` 观察的判定**：{len(rows)} 条"
        f"（窗口内判定总数 {total}，其中图片判定 {image_total}）",
        f"- 模式分布：{dict(modes) or '（无）'}",
        f"- **命中（matched）**：{len(matched)} 条；其中 **would_allow**：{len(would_allow)} 条",
        f"- 检查过附件的判定：{len(checked)} 条；标记 unavailable：{len(unavailable)} 条",
        "",
        f"## 最近 {args.limit} 条观察",
        "",
        "| 时间 | 群 | 原判定 | mode | checked | matched | 距离 | would_allow | blocked_by | unavailable |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows[: args.limit]:
        lines.append(
            "| "
            + " | ".join(
                _cell(x) for x in (r[0], r[2], r[1], r[3], r[4], r[5], r[6], r[7], r[8], r[9])
            )
            + " |"
        )
    if matched:
        lines += ["", "## 命中样本（会进入 enforce 放行的候选）", ""]
        for r in matched[:20]:
            lines.append(
                f"- `{r[0]}` 群 {r[2]} 原判 {r[1]} 距离 {_cell(r[6])} "
                f"would_allow={_cell(r[7])} blocked_by={_cell(r[8])}"
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
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
