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
"""

# R9-10-R：WHERE 必须拼在 `order by` **之前**（旧实现把它们接在 `ORDER BY … DESC` 后面，
# `--since/--until` 三种用法全部 SQL 语法错）；且所有计数共用**同一个** UTC 半开范围。
_ORDER = " order by created_at desc"
_RANGE = " and created_at >= ? and created_at < ?"
_COUNT = "select count(*) from shadow_decisions"
_COUNT_IMAGE = "select count(*) from shadow_decisions where kind='image'"


def _cell(value: object) -> str:
    return "-" if value is None else str(value)


def _parse_utc(text: str) -> str:
    """把窗口时间**解析并规范化**为 UTC ``YYYY-MM-DD HH:MM:SS`` 字符串。

    主审 C05：此前直接把输入字符串与库里的时间比较——`2026-01-02T00:00:00Z` 这类
    ISO 写法与库里的空格写法**不等价**，会漏掉起点、纳入终点；非法/逆序还会生成"成功报告"。
    支持：`YYYY-MM-DD HH:MM:SS`、带 `T`/`Z`/`±HH:MM` 偏移的 ISO 8601（统一折算到 UTC）。
    """
    raw = (text or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"不支持的时间格式：{text!r}（支持 'YYYY-MM-DD HH:MM:SS' 或 ISO 8601 带偏移）"
        ) from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC)
    # C05-R（主审第十一轮）：**受支持的精度必须保留**——只写 `%S` 会把 `.500000`
    # 悄悄截成 `.000000`，窗口被无声改变（必选集合错、计数还可能恰好相同）。
    # 非零小数秒原样写回字符串；整秒（含库里的整秒写法）保持原格式，比较语义不变。
    if parsed.microsecond:
        return parsed.strftime("%Y-%m-%d %H:%M:%S.%f")
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="shadow 观察报告（只读）")
    parser.add_argument("--db", default=str(ROOT / "data" / "moderation.db"))
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--since", default=None, help="UTC 起始（含），如 2026-09-19 13:40:00")
    parser.add_argument("--until", default=None, help="UTC 结束（不含）")
    args = parser.parse_args(argv)

    db = Path(args.db)
    window: tuple[str, str] | None = None
    if args.since or args.until:
        # C05（主审第十轮）：**先解析并规范化到 UTC**，再校验起点早于终点；
        # 非法/逆序一律拒绝生成报告（此前直接按字符串比较会漏起点、纳终点，甚至给出假成功）。
        try:
            start = _parse_utc(args.since) if args.since else "0000"
            end = _parse_utc(args.until) if args.until else "9999"
        except ValueError as exc:
            print(f"WINDOW_INVALID {exc} → 拒绝生成报告")
            return 2
        if start >= end:
            print(f"WINDOW_INVALID 起点不早于终点：{start} → {end} → 拒绝生成报告")
            return 2
        window = (start, end)
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        # Keep rows and every denominator on the same SQLite read snapshot.
        con.execute("BEGIN")
        if window is not None:
            # 起点**含**、终点**不含**；观测、总数、图片数**同一范围**（窗外一律不计入）。
            lo, hi = window
            rows = con.execute(SQL + _RANGE + _ORDER, (lo, hi)).fetchall()
            total = con.execute(
                _COUNT + " where created_at >= ? and created_at < ?", (lo, hi)
            ).fetchone()[0]
            image_total = con.execute(
                "select count(*) from shadow_decisions where kind='image'"
                " and created_at >= ? and created_at < ?",
                (lo, hi),
            ).fetchone()[0]
            scope = f"UTC 半开窗口 `{lo}` → `{hi}`（起点含、终点不含；所有计数共用该范围）"
            windowed = True
        else:
            rows = con.execute(SQL + _ORDER).fetchall()
            total = con.execute(_COUNT).fetchone()[0]
            image_total = con.execute(_COUNT_IMAGE).fetchone()[0]
            scope = "**全库留存累计**（未按时间/账号/部署版本分离）"
            windowed = False
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
        "- **口径警示**：本报告**不是**「窗口内覆盖率」的证据 —— "
        + (
            f"窗口口径下分子（窗口内观察 {len(rows)} 条）与分母（**同一 UTC 半开范围**的判定 "
            f"{total} 条、其中图片判定 {image_total} 条）**范围相同**，可以相除得到**窗口内**"
            "的比例；但它仍不等于业务命中率（观察样本不是全量判定）。"
            if windowed
            else f"默认口径下分子（观察条数）与分母（全库留存判定 {total} 条、其中图片判定 "
            f"{image_total} 条）**不同范围**，不得相除得出覆盖率或命中率；"
            "需要窗口口径请用 `--since/--until` 重出。"
        ),
        f"- **有 `image_hash` 观察的判定**：{len(rows)} 条"
        + (
            f"（窗口内判定 {total} 条，其中图片判定 {image_total} 条；**同一 UTC 半开范围**）"
            if windowed
            else f"（全库留存累计判定 {total} 条，其中图片判定 {image_total} 条；"
            "两者**不同范围**，不可相除）"
        ),
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
    # Never overwrite sealed evidence, even when exports happen in the same second.
    with out.open("x", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
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
