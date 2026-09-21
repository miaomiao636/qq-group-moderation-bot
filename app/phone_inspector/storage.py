"""Private per-task SQLite storage. No connection to the moderation database."""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .pages import InspectionError

APP_ID = 0x51515049
LABELS = {
    "WARNING_OBSERVED": "观察到 QQ 资料卡账号异常提示",
    "WARNING_NOT_OBSERVED_THIS_VISIT": "本次未观察到该提示，账号状态未判定",
    "INCONCLUSIVE": "无法确认",
    "PENDING": "中断时尚未确认",
}


def now() -> str:
    return datetime.now(UTC).isoformat()


def data_root() -> Path:
    import os

    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
    return base / "QQPhoneInspector"


class Store:
    def __init__(self, folder: Path, *, create: bool = False) -> None:
        self.folder = folder.resolve()
        if create:
            self.folder.mkdir(parents=True, exist_ok=False)
        path = self.folder / "inspection.sqlite3"
        if not create and not path.is_file():
            raise InspectionError("所选目录不是巡检任务。")
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA trusted_schema=OFF")
        if create:
            self.db.executescript(f"""
                PRAGMA application_id={APP_ID};
                PRAGMA user_version=1;
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE passes (id INTEGER PRIMARY KEY, started TEXT NOT NULL,
                    ended TEXT, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                    count_before INTEGER, count_after INTEGER);
                CREATE TABLE visits (id TEXT PRIMARY KEY, pass_id INTEGER NOT NULL,
                    started TEXT NOT NULL, finished TEXT, qq TEXT,
                    result TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                    evidence TEXT NOT NULL DEFAULT '', page_index INTEGER, row_index INTEGER);
                CREATE INDEX visits_qq ON visits(qq, result, finished);
            """)
        if (
            self.db.execute("PRAGMA application_id").fetchone()[0] != APP_ID
            or self.db.execute("PRAGMA user_version").fetchone()[0] != 1
        ):
            self.db.close()
            raise InspectionError("巡检任务格式不受支持；原文件未改动。")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")

    def close(self) -> None:
        self.db.close()

    def metadata(self) -> dict[str, Any]:
        return {
            row["key"]: json.loads(row["value"]) for row in self.db.execute("SELECT * FROM meta")
        }

    def bind(self, identity: dict[str, Any]) -> None:
        previous = self.metadata()
        if previous:
            for key in ("group_id", "device_hash", "android_user", "qq_version"):
                if previous.get(key) != identity.get(key):
                    raise InspectionError("群、设备、QQ 分身或版本与原任务不一致，请新建任务。")
            return
        with self.db:
            self.db.executemany(
                "INSERT INTO meta VALUES (?, ?)",
                [(key, json.dumps(value, ensure_ascii=False)) for key, value in identity.items()],
            )

    def begin_pass(self, count: int | None) -> int:
        with self.db:
            # An interrupted process cannot leave a task looking actively complete.
            self.db.execute(
                "UPDATE passes SET status='PAUSED', ended=?, reason=? WHERE status='RUNNING'",
                (now(), "上次运行中断；保留已有结果，从顶部重新核对。"),
            )
            cursor = self.db.execute(
                "INSERT INTO passes(started,status,count_before) VALUES (?,'RUNNING',?)",
                (now(), count),
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def start_visit(self, pass_id: int, page_index: int, row_index: int) -> str:
        visit_id = uuid.uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO visits(id,pass_id,started,result,page_index,row_index) VALUES (?,?,?,'PENDING',?,?)",
                (visit_id, pass_id, now(), page_index, row_index),
            )
        return visit_id

    def finish_visit(
        self, visit_id: str, qq: str | None, result: str, evidence: str = "", reason: str = ""
    ) -> None:
        import re

        if result not in LABELS or result == "PENDING":
            raise ValueError("Invalid observation result")
        if result != "INCONCLUSIVE" and (qq is None or not re.fullmatch(r"[1-9][0-9]{4,11}", qq)):
            raise InspectionError("缺少明确 QQ 号，不能保存为已确认观察。")
        with self.db:
            cursor = self.db.execute(
                "UPDATE visits SET qq=?, result=?, evidence=?, reason=?, finished=? WHERE id=? AND result='PENDING'",
                (qq, result, evidence, reason, now(), visit_id),
            )
            if cursor.rowcount != 1:
                raise InspectionError("观察记录已经完成或不存在，拒绝覆盖。")

    def end_pass(
        self, pass_id: int, status: str, reason: str, count_after: int | None = None
    ) -> None:
        if status not in {"PAUSED", "END_REACHED"}:
            raise ValueError("Invalid pass status")
        with self.db:
            self.db.execute(
                "UPDATE passes SET status=?, reason=?, ended=?, count_after=? WHERE id=?",
                (status, reason, now(), count_after, pass_id),
            )

    def evidence(self, visit_id: str, contents: dict[str, bytes]) -> str:
        folder = self.folder / "evidence" / visit_id
        folder.mkdir(parents=True, exist_ok=False)
        manifest: dict[str, Any] = {"saved_at": now(), "files": {}}
        for name, raw in contents.items():
            if name not in {
                "observation.json",
                "warning.xml",
                "restricted-profile.xml",
                "warning.png",
            }:
                raise ValueError("Unsupported evidence filename")
            with (folder / name).open("xb") as output:
                output.write(raw)
                output.flush()
                import os

                os.fsync(output.fileno())
            manifest["files"][name] = hashlib.sha256(raw).hexdigest()
        (folder / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return str(folder.relative_to(self.folder))

    def observations(
        self, *, warnings_only: bool = False, limit: int = 200
    ) -> list[dict[str, Any]]:
        condition = "WHERE result='WARNING_OBSERVED'" if warnings_only else "WHERE qq IS NOT NULL"
        # Keep a warning from any visit in this task; a later missing popup cannot erase it.
        sql = f"""WITH ranked AS (SELECT *, row_number() OVER (
            PARTITION BY qq ORDER BY (result='WARNING_OBSERVED') DESC, finished DESC, id DESC) AS rank
            FROM visits {condition}) SELECT * FROM ranked WHERE rank=1 ORDER BY finished DESC LIMIT ?"""
        return [dict(row) for row in self.db.execute(sql, (limit,))]

    def stats(self) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT count(DISTINCT qq) AS checked, count(DISTINCT CASE WHEN result='WARNING_OBSERVED' THEN qq END) AS warnings, sum(CASE WHEN result IN ('PENDING','INCONCLUSIVE') THEN 1 ELSE 0 END) AS unresolved FROM visits"
        ).fetchone()
        latest = self.db.execute("SELECT * FROM passes ORDER BY id DESC LIMIT 1").fetchone()
        return {
            **dict(row),
            "unresolved": row["unresolved"] or 0,
            "latest_pass": dict(latest) if latest else None,
        }

    def pass_qqs(self, pass_id: int) -> set[str]:
        return {
            str(row[0])
            for row in self.db.execute(
                "SELECT DISTINCT qq FROM visits WHERE pass_id=? AND qq IS NOT NULL", (pass_id,)
            )
        }

    def export(self) -> Path:
        self.db.execute("BEGIN")
        try:
            metadata, stats = self.metadata(), self.stats()
            rows = self.observations(warnings_only=True, limit=1_000_000)
        finally:
            self.db.rollback()
        folder = (
            self.folder
            / "exports"
            / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
        )
        folder.mkdir(parents=True, exist_ok=False)
        report = {
            "schema_version": 1,
            "exported_at": now(),
            "task": metadata,
            "stats": stats,
            "interpretation": "只记录 QQ 客户端提示，不代表永久封禁、注销或已确认违规；未出现提示不证明正常。",
            "coverage_note": "中断恢复从顶部重查并合并历史观察；到达列表末尾不代表同一时刻的完整群快照。",
            "warnings": rows,
        }
        (folder / "巡检报告.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        headers = ["群名称", "群号", "QQ号", "观察结果", "观察时间UTC", "本地证据目录"]

        def safe(value: object) -> str:
            text = str(value)
            return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text

        lines = [
            "QQ 手机辅助巡检 — 异常提示观察记录",
            str(report["interpretation"]),
            str(report["coverage_note"]),
            f"任务状态：{stats['latest_pass']}",
            "\t".join(headers),
        ]
        with (folder / "异常提示账号.csv").open("w", encoding="utf-8-sig", newline="") as output:
            writer = csv.writer(output, quoting=csv.QUOTE_ALL)
            writer.writerow(headers)
            for row in rows:
                values = [
                    metadata.get("group_name", ""),
                    metadata.get("group_id", ""),
                    row["qq"],
                    LABELS[row["result"]],
                    row["finished"],
                    str(self.folder / row["evidence"]),
                ]
                writer.writerow([safe(v) for v in values])
                lines.append(
                    "\t".join(
                        str(v).replace("\n", " ").replace("\r", " ").replace("\t", " ")
                        for v in values
                    )
                )
        (folder / "异常提示账号.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return folder
