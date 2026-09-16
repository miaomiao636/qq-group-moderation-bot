#!/usr/bin/env python3
"""保留期清理 · 隔离副本演练 + 备份恢复校验 + 旧损坏盘点（只读生产）。

安全设计（绝不触碰生产数据/媒体）：
- 用 SQLite backup API 从生产库做**一致性快照**到 `data/purge_drill/drill.db`；
- 运行 purge 前将 `app.runtime.pipeline.MEDIA_DIR` 替换为隔离媒体目录
  （purge 的媒体步骤只作用于该目录）；
- 生产库只读、生产 `data/media/` 完全不访问；本脚本所有写入都在
  `data/purge_drill/` 内。

演练内容：
1. 在副本上注入"超期"与"未到期"对照数据（含原文快照/事件/日志/反馈）；
2. 隔离媒体目录放一个 20 天前文件 + 一个今天文件；
3. 跑 `purge_expired` → 验证：超期原文被清、未到期保留、旧媒体删且新媒体留；
4. 用真实备份 `data/backups/w0-20260910-175738/moderation.db` 做恢复校验
   （可打开、schema 版本、关键表计数）；
5. 输出旧目录/文件盘点清单（归属与 15 天到期窗口评估，不删除）。

用法：`.venv\\Scripts\\python.exe scripts/purge_drill.py`
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
DRILL_DIR = ROOT / "data" / "purge_drill"
DRILL_DB = DRILL_DIR / "drill.db"
DRILL_MEDIA = DRILL_DIR / "media"
PROD_DB = ROOT / "data" / "moderation.db"
BACKUP_DB = ROOT / "data" / "backups" / "w0-20260910-175738" / "moderation.db"
REPORT = DRILL_DIR / "report.json"


def _snapshot_db() -> None:
    src = sqlite3.connect(f"file:{PROD_DB.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(DRILL_DB)
    src.backup(dst)
    src.close()
    dst.close()
    print(f"[snapshot] {PROD_DB.name} -> {DRILL_DB.name}")


def _insert_row(c: sqlite3.Connection, table: str, values: dict[str, object]) -> int:
    """按表结构插入：未提供的 NOT NULL 无默认列用类型安全占位补齐。"""
    full: dict[str, object] = {}
    for row in c.execute(f"PRAGMA table_info({table})"):
        name, typ, notnull, dflt, pk = str(row[1]), str(row[2]), row[3], row[4], row[5]
        if name in values:
            full[name] = values[name]
        elif notnull and dflt is None and not pk:
            t = typ.upper()
            if any(k in t for k in ("INT", "FLOAT", "REAL", "BOOL")):
                full[name] = 0
            else:
                full[name] = "{}" if name.upper().endswith("JSON") else ""
    sql = f"INSERT INTO {table} ({','.join(full)}) VALUES ({','.join('?' * len(full))})"
    rowid = c.execute(sql, tuple(full.values())).lastrowid
    return int(rowid or 0)


def _inject() -> dict[str, list[int]]:
    now = datetime.now(UTC).replace(tzinfo=None)
    c = sqlite3.connect(DRILL_DB)
    ids: dict[str, list[int]] = {}

    def add(name: str, table: str, values: dict[str, object]) -> None:
        ids.setdefault(name, []).append(_insert_row(c, table, values))

    old45 = (now - timedelta(days=45)).isoformat(sep=" ")
    old60 = (now - timedelta(days=60)).isoformat(sep=" ")
    old200 = (now - timedelta(days=200)).isoformat(sep=" ")
    fresh = now.isoformat(sep=" ")

    # 超期（应被清理）
    add(
        "violation_old",
        "violation_records",
        {
            "group_openid": "G_DRILL",
            "member_openid": "M_DRILL",
            "message_id": "DRILL_OLD_V",
            "category": "ad",
            "confidence": 0.95,
            "message_snapshot_json": '{"text": "DRILL原始违规原文-应被清除"}',
            "created_at": old45,
        },
    )
    add(
        "event_old",
        "processed_events",
        {"message_id": "DRILL_OLD_E", "processed_at": old60, "event_type": "t"},
    )
    add(
        "action_old",
        "action_logs",
        {"action": "recall", "group_openid": "G_DRILL", "ok": 1, "created_at": old200},
    )
    add(
        "shadow_old",
        "shadow_decisions",
        {
            "message_id": "DRILL_OLD_S",
            "group_openid": "G_DRILL",
            "member_openid": "M_DRILL",
            "kind": "text",
            "verdict": "record_only",
            "category": "ad",
            "confidence": 0.5,
            "reason": "drill",
            "detail_json": '{"text_preview": "DRILL影子原文-应被清除"}',
            "created_at": old45,
        },
    )
    add(
        "feedback_old",
        "feedback_records",
        {
            "message_id": "DRILL_OLD_F",
            "label": "confirmed_violation",
            "category": "ad",
            "operator": "drill",
            "reason": "drill",
            "sample_text_masked": "DRILL反馈原文-应被清除",
            "created_at": old45,
        },
    )
    # 未到期对照（应保留）
    add(
        "violation_new",
        "violation_records",
        {
            "group_openid": "G_DRILL",
            "member_openid": "M_DRILL",
            "message_id": "DRILL_NEW_V",
            "category": "ad",
            "confidence": 0.95,
            "message_snapshot_json": '{"text": "未到期原文-应保留"}',
            "created_at": fresh,
        },
    )
    add(
        "event_new",
        "processed_events",
        {"message_id": "DRILL_NEW_E", "processed_at": fresh, "event_type": "t"},
    )
    add(
        "shadow_new",
        "shadow_decisions",
        {
            "message_id": "DRILL_NEW_S",
            "group_openid": "G_DRILL",
            "member_openid": "M_DRILL",
            "kind": "text",
            "verdict": "record_only",
            "category": "ad",
            "confidence": 0.5,
            "reason": "drill",
            "detail_json": '{"text_preview": "未到期影子原文-应保留"}',
            "created_at": fresh,
        },
    )
    c.commit()
    c.close()
    print("[inject] drill rows:", json.dumps(ids, ensure_ascii=False))
    return ids


def _make_media() -> tuple[Path, Path]:
    DRILL_MEDIA.mkdir(parents=True, exist_ok=True)
    old_file = DRILL_MEDIA / "drill_old.jpg"
    new_file = DRILL_MEDIA / "drill_new.jpg"
    old_file.write_bytes(b"old")
    new_file.write_bytes(b"new")
    old_ts = time.time() - 20 * 86400
    os.utime(old_file, (old_ts, old_ts))
    print("[media] drill_old.jpg mtime=-20d, drill_new.jpg mtime=now")
    return old_file, new_file


async def _run_purge() -> dict[str, int]:
    import app.runtime.pipeline as pipeline

    pipeline.MEDIA_DIR = DRILL_MEDIA  # 隔离媒体目录（purge 只作用于它）

    from app.db import SessionLocal
    from app.reports.cleanup import purge_expired

    async with SessionLocal() as session:
        stats = await purge_expired(session)
    print("[purge] stats:", json.dumps(stats, ensure_ascii=False))
    return stats


def _verify(ids: dict[str, list[int]], old_file: Path, new_file: Path) -> dict[str, object]:
    """按注入时返回的精确行 ID 校验（R-112 N05：禁止日期窗口近似）。

    旧版 action_logs 用固定 `< '2026-01-01'` 窗口查询，注入时间与实际执行日
    偏移后会假通过；本版所有校验均按 `ids` 中的精确主键。
    """
    c = sqlite3.connect(DRILL_DB)
    checks: dict[str, object] = {}

    def one(sql: str, params: tuple[object, ...] = ()) -> tuple[object, ...] | None:
        row = c.execute(sql, params).fetchone()
        return tuple(row) if row is not None else None

    def field_by_id(table: str, column: str, ids_key: str) -> object:
        # lastrowid = 隐含 rowid：对所有表通用（部分表无显式 id 列）
        row = one(f"SELECT {column} FROM {table} WHERE rowid=?", (ids[ids_key][0],))
        return row[0] if row else None

    def count_by_id(table: str, ids_key: str) -> int:
        row = one(f"SELECT COUNT(*) FROM {table} WHERE rowid=?", (ids[ids_key][0],))
        return int(row[0]) if row else -1

    checks["violation_old_snapshot"] = field_by_id(
        "violation_records", "message_snapshot_json", "violation_old"
    )
    checks["violation_new_snapshot"] = field_by_id(
        "violation_records", "message_snapshot_json", "violation_new"
    )
    checks["event_old_remaining"] = count_by_id("processed_events", "event_old")
    checks["event_new_remaining"] = count_by_id("processed_events", "event_new")
    checks["action_old_remaining"] = count_by_id("action_logs", "action_old")
    checks["shadow_old_detail"] = field_by_id("shadow_decisions", "detail_json", "shadow_old")
    checks["shadow_new_detail"] = field_by_id("shadow_decisions", "detail_json", "shadow_new")
    checks["feedback_old_text"] = field_by_id(
        "feedback_records", "sample_text_masked", "feedback_old"
    )
    checks["media_old_exists"] = old_file.exists()
    checks["media_new_exists"] = new_file.exists()
    c.close()
    return checks


def _evaluate(checks: dict[str, object]) -> list[str]:
    """按预期显式判定全部检查；返回失败说明列表（空 = 全部通过）。

    R-112 N05：旧版只返回字段、never 判失败。任何残留/误删都必须产生
    非空失败列表，由 main 返回非零退出码。
    """
    failures: list[str] = []
    if '"purged"' not in str(checks.get("violation_old_snapshot") or ""):
        failures.append("violation_old_snapshot：超期原文未被清除")
    if "未到期原文" not in str(checks.get("violation_new_snapshot") or ""):
        failures.append("violation_new_snapshot：未到期对照缺失（误删/误清）")
    if checks.get("event_old_remaining") != 0:
        failures.append(f"event_old_remaining={checks.get('event_old_remaining')}：超期事件未清理")
    if checks.get("event_new_remaining") != 1:
        failures.append(f"event_new_remaining={checks.get('event_new_remaining')}：未到期对照缺失")
    if checks.get("action_old_remaining") != 0:
        failures.append(
            f"action_old_remaining={checks.get('action_old_remaining')}：超期动作日志未清理"
        )
    if "DRILL影子原文" in str(checks.get("shadow_old_detail") or ""):
        failures.append("shadow_old_detail：超期影子原文未被清除")
    if "未到期影子原文" not in str(checks.get("shadow_new_detail") or ""):
        failures.append("shadow_new_detail：未到期影子对照缺失（误删/误清）")
    if checks.get("feedback_old_text"):
        failures.append("feedback_old_text：超期反馈原文未被清除")
    if checks.get("media_old_exists"):
        failures.append("media_old_exists：超期媒体未删除")
    if not checks.get("media_new_exists"):
        failures.append("media_new_exists：未到期媒体缺失（误删）")
    return failures


def _recover_check() -> dict[str, object]:
    out = DRILL_DIR / "recovered.db"
    if out.exists():
        out.unlink()
    shutil.copyfile(BACKUP_DB, out)
    c = sqlite3.connect(f"file:{out.as_posix()}?mode=ro", uri=True)
    ver = c.execute("SELECT version_num FROM alembic_version").fetchone()
    tables = [
        str(r[0])
        for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    ]
    counts: dict[str, int] = {}
    for t in ("shadow_decisions", "violation_records", "cases", "notification_notices"):
        if t in tables:
            counts[t] = int(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
    c.close()
    info: dict[str, object] = {
        "backup_file": BACKUP_DB.name,
        "alembic_version": str(ver[0]) if ver else None,
        "tables": len(tables),
        "counts": counts,
        "openable": True,
    }
    print("[recover] backup ok:", json.dumps(info, ensure_ascii=False))
    return info


def _inventory() -> list[dict[str, object]]:
    items: list[dict[str, object]] = []

    def stat_dir(p: Path, note: str) -> None:
        if not p.exists():
            return
        files = [f for f in p.rglob("*") if f.is_file()]
        total = sum(f.stat().st_size for f in files)
        newest = max((f.stat().st_mtime for f in files), default=0)
        items.append(
            {
                "path": str(p.relative_to(ROOT)),
                "files": len(files),
                "size_mb": round(total / 1e6, 2),
                "newest_file": (
                    datetime.fromtimestamp(newest).strftime("%m-%d %H:%M") if newest else None
                ),
                "note": note,
            }
        )

    stat_dir(
        ROOT / "data" / "media_snapshot_20260911",
        "09-10 手工快照（含 _frames 视频帧）；不在自动清理范围；"
        "按源时间 15 天窗口 ≈09-20~09-25 到期后需人工核实清理",
    )
    stat_dir(ROOT / "data" / "t002_media", "T-002 时代原始违规图归档（09-05，到期 ≈09-20）")
    stat_dir(ROOT / "data" / "_dbg_tmp", "调试残留（09-14，可随时清理）")
    stat_dir(ROOT / "data" / "backups" / "w0-20260910-175738", "W0 备份（保留）")
    shadow_bak = ROOT / "data" / "backup_shadow_1090875633.json"
    if shadow_bak.exists():
        items.append(
            {
                "path": str(shadow_bak.relative_to(ROOT)),
                "files": 1,
                "size_mb": round(shadow_bak.stat().st_size / 1e6, 2),
                "newest_file": datetime.fromtimestamp(shadow_bak.stat().st_mtime).strftime(
                    "%m-%d %H:%M"
                ),
                "note": "影子数据手工导出（09-11）；按 15 天政策 ≈09-26 到期",
            }
        )
    for it in items:
        print("[inventory]", json.dumps(it, ensure_ascii=False))
    return items


def main() -> int:
    DRILL_DIR.mkdir(parents=True, exist_ok=True)
    if DRILL_DB.exists():
        DRILL_DB.unlink()
    _snapshot_db()

    ids = _inject()
    old_file, new_file = _make_media()

    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DRILL_DB.as_posix()}"
    stats = asyncio.run(_run_purge())
    checks = _verify(ids, old_file, new_file)
    failures = _evaluate(checks)
    recovery = _recover_check()
    inventory = _inventory()

    report: dict[str, object] = {
        "purge_stats": stats,
        "checks": checks,
        "failures": failures,
        "passed": not failures,
        "recovery": recovery,
        "inventory": inventory,
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[report]", str(REPORT))
    if failures:
        for item in failures:
            print(f"[FAIL] {item}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
