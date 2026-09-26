"""Independent task storage; no production database, credentials, or raw pages."""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.space_inspector.contracts import (
    BLOCKED,
    LABELS,
    NONFRIEND_NOTICE,
    REASONS,
    RESTRICTED,
    RESTRICTION_NOTICES,
    UNCONFIRMED,
    Group,
    InspectionError,
    Observation,
    numeric_id,
)
from app.space_inspector.export_paths import group_stem

APPLICATION_ID = 0x51515349
SCHEMA_VERSION = 1
MAX_MEMBERSHIPS = 1_000_000
MAX_UNIQUE_MEMBERS = 200_000
MAX_GROUPS = 2000
MAX_VISITS = 1_000_000
_COLUMNS = {
    "meta": ("key", "value"),
    "groups": ("group_id", "name", "declared_count", "snapshot_count", "saved_at"),
    "membership": ("group_id", "qq"),
    "visits": ("id", "qq", "status", "reason", "checked_at", "evidence_json"),
    "observations": ("qq", "visit_id", "status", "reason", "checked_at", "evidence_json"),
}
_EVIDENCE_KEYS = {
    "page_url",
    "viewer_qq",
    "notice",
    "notice_source",
    "ready_state",
    "panel_count",
    "report_icon_count",
}
EVIDENCE_CONTRACT = "qzone-dom-v1"
_SOURCES = {
    "qzone_top_level_error",
    "qzone_profile",
    "qzone_permission_page",
    "qzone_unopened_page",
    "qzone_nonfriend_page",
    "unrecognized_page",
    "login_redirect",
    "navigation_failure",
    "platform_access_block",
}
_SELECT_ROWS = """
SELECT g.name AS group_name, g.group_id, m.qq,
       COALESCE(o.status, 'PENDING') AS status,
       COALESCE(o.checked_at, '') AS checked_at,
       COALESCE(o.reason, '') AS reason,
       COALESCE(o.evidence_json, '{}') AS evidence_json
FROM membership m JOIN groups g ON g.group_id=m.group_id
LEFT JOIN observations o ON o.qq=m.qq
"""


def _utc() -> str:
    return datetime.now(UTC).isoformat()


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise InspectionError("任务时间格式无效。")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise InspectionError("任务时间格式无效。") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise InspectionError("任务时间必须为 UTC。")
    return value


def _safe_csv(value: object) -> str:
    text = str(value)
    if text.startswith(("\t", "\r", "\n")) or text.lstrip(" \t\r\n").startswith(
        ("=", "+", "-", "@")
    ):
        return "'" + text
    return text


class Store:
    def __init__(self, folder: Path, create: bool = False):
        self.folder = Path(folder).resolve()
        database = self.folder / "task.sqlite3"
        if create:
            if database.exists():
                raise InspectionError("任务已存在，不能覆盖。")
            self.folder.mkdir(parents=True, exist_ok=True)
        elif not database.is_file():
            raise InspectionError("找不到已有巡检任务。")
        self._db: sqlite3.Connection | None = None
        try:
            if create:
                with database.open("xb"):
                    pass
            self._db = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=10)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA trusted_schema=OFF")
            if create:
                self._create()
            else:
                self._validate()
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
        except (sqlite3.Error, OSError, ValueError, TypeError, InspectionError):
            self.close()
            raise InspectionError("任务文件无效或不属于本巡检工具，未继续读取。") from None

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            raise InspectionError("任务已关闭。")
        return self._db

    @classmethod
    def validate_connection(cls, db: sqlite3.Connection) -> None:
        """Validate an offline snapshot without opening a writable task store."""
        reader = cls.__new__(cls)
        reader._db = db
        old_factory = db.row_factory
        db.row_factory = sqlite3.Row
        try:
            reader._validate()
        finally:
            reader._db = None
            db.row_factory = old_factory

    def _create(self) -> None:
        self.db.executescript("""
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE groups(group_id TEXT PRIMARY KEY, name TEXT NOT NULL, declared_count INTEGER NOT NULL,
 snapshot_count INTEGER NOT NULL, saved_at TEXT NOT NULL);
CREATE TABLE membership(group_id TEXT NOT NULL REFERENCES groups(group_id), qq TEXT NOT NULL,
 PRIMARY KEY(group_id,qq));
CREATE INDEX membership_qq ON membership(qq);
CREATE TABLE visits(id INTEGER PRIMARY KEY AUTOINCREMENT, qq TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('RESTRICTION_OBSERVED','UNCONFIRMED','BLOCKED')),
 reason TEXT NOT NULL, checked_at TEXT NOT NULL, evidence_json TEXT NOT NULL);
CREATE INDEX visits_qq_id ON visits(qq,id);
CREATE TABLE observations(qq TEXT PRIMARY KEY, visit_id INTEGER NOT NULL UNIQUE REFERENCES visits(id),
 status TEXT NOT NULL CHECK(status IN ('RESTRICTION_OBSERVED','UNCONFIRMED','BLOCKED')),
 reason TEXT NOT NULL, checked_at TEXT NOT NULL, evidence_json TEXT NOT NULL);
""")
        with self.db:
            self.db.executemany(
                "INSERT INTO meta VALUES (?,?)",
                [
                    ("created_at", _utc()),
                    ("source_self_id", ""),
                    ("viewer_qq", ""),
                    ("prepared", "0"),
                ],
            )
            self.db.execute(f"PRAGMA application_id={APPLICATION_ID}")
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _validate(self) -> None:
        if (
            self.db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
            or self.db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
        ):
            raise InspectionError("任务版本不受支持。")
        schema = self.db.execute(
            "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if any(row["type"] in ("view", "trigger") for row in schema) or {
            row["name"] for row in schema if row["type"] == "table"
        } != set(_COLUMNS):
            raise InspectionError("任务表结构无效。")
        for table, names in _COLUMNS.items():
            if (
                tuple(row["name"] for row in self.db.execute(f"PRAGMA table_info({table})"))
                != names
            ):
                raise InspectionError("任务表结构无效。")
        raw = dict(self.db.execute("SELECT key,value FROM meta"))
        if set(raw) != {"created_at", "source_self_id", "viewer_qq", "prepared"} or raw[
            "prepared"
        ] not in ("0", "1"):
            raise InspectionError("任务元数据无效。")
        _timestamp(raw["created_at"])
        for key in ("source_self_id", "viewer_qq"):
            if raw[key]:
                numeric_id(raw[key])
        counts = self.db.execute(
            "SELECT (SELECT COUNT(*) FROM groups), (SELECT COUNT(*) FROM membership), (SELECT COUNT(DISTINCT qq) FROM membership), (SELECT COUNT(*) FROM visits)"
        ).fetchone()
        if any(
            value > maximum
            for value, maximum in zip(
                counts, (MAX_GROUPS, MAX_MEMBERSHIPS, MAX_UNIQUE_MEMBERS, MAX_VISITS), strict=True
            )
        ):
            raise InspectionError("任务规模超出支持范围。")
        if (counts[0] and not raw["source_self_id"]) or (
            counts[3] and (not raw["viewer_qq"] or raw["prepared"] != "1")
        ):
            raise InspectionError("任务身份或快照状态不完整。")
        if raw["prepared"] == "1" and not counts[0]:
            raise InspectionError("已准备任务缺少群快照。")
        if self.db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise InspectionError("任务关联记录无效。")
        for group in self.db.execute("SELECT * FROM groups"):
            self._group(Group(group["group_id"], group["name"], group["declared_count"]))
            _timestamp(group["saved_at"])
            actual = self.db.execute(
                "SELECT COUNT(*) FROM membership WHERE group_id=?", (group["group_id"],)
            ).fetchone()[0]
            if actual != group["snapshot_count"]:
                raise InspectionError("群快照计数不一致。")
        for member in self.db.execute("SELECT DISTINCT qq FROM membership"):
            numeric_id(member[0])
        if self.db.execute(
            "SELECT 1 FROM visits WHERE length(evidence_json)>2048 LIMIT 1"
        ).fetchone():
            raise InspectionError("任务依据大小无效。")
        for visit in self.db.execute(
            "SELECT qq,status,reason,checked_at,evidence_json FROM visits"
        ):
            self._validated_observation(
                Observation(
                    visit["qq"],
                    visit["status"],
                    visit["reason"],
                    visit["checked_at"],
                    json.loads(visit["evidence_json"]),
                )
            )
        inconsistent = self.db.execute("""SELECT 1 FROM observations o LEFT JOIN visits v ON v.id=o.visit_id
WHERE v.id IS NULL OR o.qq!=v.qq OR o.status!=v.status OR o.reason!=v.reason OR o.checked_at!=v.checked_at OR o.evidence_json!=v.evidence_json
OR o.visit_id!=(SELECT MAX(id) FROM visits WHERE qq=o.qq) LIMIT 1""").fetchone()
        missing = self.db.execute(
            "SELECT 1 FROM visits v LEFT JOIN observations o ON o.qq=v.qq WHERE o.qq IS NULL LIMIT 1"
        ).fetchone()
        if inconsistent or missing:
            raise InspectionError("任务最新记录与历史不一致。")

    @property
    def metadata(self) -> dict[str, object]:
        result: dict[str, object] = dict(self.meta())
        result["prepared"] = result["prepared"] == "1"
        return result

    def meta(self) -> dict[str, str]:
        return dict(self.db.execute("SELECT key,value FROM meta"))

    def _bind(self, key: str, qq: str) -> None:
        qq = numeric_id(qq)
        existing = self.metadata[key]
        if existing and existing != qq:
            raise InspectionError("该任务已绑定其他账号，请新建任务。")
        with self.db:
            self.db.execute("UPDATE meta SET value=? WHERE key=?", (qq, key))

    def bind_source(self, qq: str) -> None:
        self._bind("source_self_id", qq)

    def bind_viewer(self, qq: str) -> None:
        self._bind("viewer_qq", qq)

    @staticmethod
    def _group(group: Group) -> None:
        numeric_id(group.group_id)
        if (
            not isinstance(group.name, str)
            or not group.name
            or len(group.name) > 500
            or "\x00" in group.name
            or type(group.member_count) is not int
            or not 0 <= group.member_count <= 20_000
        ):
            raise InspectionError("群快照字段无效。")

    def add_snapshot(self, group: Group, members: list[str]) -> None:
        if self.metadata["prepared"] or not self.metadata["source_self_id"]:
            raise InspectionError("任务已封存或尚未绑定成员来源账号。")
        self._group(group)
        if not isinstance(members, list) or len(members) > 20_000:
            raise InspectionError("群成员快照数量无效。")
        member_ids = sorted({numeric_id(member) for member in members})
        existing = self.db.execute(
            "SELECT * FROM groups WHERE group_id=?", (group.group_id,)
        ).fetchone()
        if existing:
            saved = [
                row[0]
                for row in self.db.execute(
                    "SELECT qq FROM membership WHERE group_id=? ORDER BY qq", (group.group_id,)
                )
            ]
            if (
                existing["name"] != group.name
                or existing["declared_count"] != group.member_count
                or saved != member_ids
            ):
                raise InspectionError("已有群快照不一致，不能覆盖。")
            return
        with self.db:
            self.db.execute(
                "INSERT INTO groups VALUES (?,?,?,?,?)",
                (group.group_id, group.name, group.member_count, len(member_ids), _utc()),
            )
            self.db.executemany(
                "INSERT INTO membership VALUES (?,?)",
                ((group.group_id, member) for member in member_ids),
            )
            counts = self.db.execute(
                "SELECT (SELECT COUNT(*) FROM groups), (SELECT COUNT(*) FROM membership), (SELECT COUNT(DISTINCT qq) FROM membership)"
            ).fetchone()
            if any(
                value > maximum
                for value, maximum in zip(
                    counts, (MAX_GROUPS, MAX_MEMBERSHIPS, MAX_UNIQUE_MEMBERS), strict=True
                )
            ):
                raise InspectionError("任务规模超出支持范围，请分批建立任务。")

    def seal_snapshots(self) -> None:
        if (
            not self.metadata["source_self_id"]
            or not self.db.execute("SELECT 1 FROM groups LIMIT 1").fetchone()
        ):
            raise InspectionError("请先完成至少一个群的成员快照。")
        with self.db:
            self.db.execute("UPDATE meta SET value='1' WHERE key='prepared'")

    def pending(self) -> list[str]:
        if not self.metadata["prepared"]:
            raise InspectionError("成员快照尚未完成，请补全或新建任务。")
        return [
            row[0]
            for row in self.db.execute(
                "SELECT DISTINCT m.qq FROM membership m LEFT JOIN observations o ON o.qq=m.qq WHERE o.qq IS NULL OR o.status=? ORDER BY m.qq",
                (BLOCKED,),
            )
        ]

    def _validated_observation(self, observation: Observation) -> str:
        qq = numeric_id(observation.qq)
        if (
            observation.status not in (RESTRICTED, UNCONFIRMED, BLOCKED)
            or not isinstance(observation.reason, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,79}", observation.reason) is None
        ):
            raise InspectionError("巡检结果或原因码无效。")
        _timestamp(observation.checked_at)
        if not self.db.execute("SELECT 1 FROM membership WHERE qq=? LIMIT 1", (qq,)).fetchone():
            raise InspectionError("该成员不在本任务群快照中。")
        value = observation.evidence
        if type(value) is not dict or set(value) not in (
            _EVIDENCE_KEYS,
            _EVIDENCE_KEYS | {"reuse"},
        ):
            raise InspectionError("巡检依据字段无效。")
        if "reuse" in value:
            reuse = value["reuse"]
            if (
                observation.status not in (RESTRICTED, UNCONFIRMED)
                or type(reuse) is not dict
                or set(reuse) != {"source_task", "source_visit", "reused_at", "contract"}
                or not isinstance(reuse["source_task"], str)
                or re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", reuse["source_task"]) is None
                or type(reuse["source_visit"]) is not int
                or reuse["source_visit"] <= 0
                or reuse["contract"] != EVIDENCE_CONTRACT
            ):
                raise InspectionError("历史观察来源无效。")
            reused_at = datetime.fromisoformat(_timestamp(reuse["reused_at"]))
            if (
                not timedelta(0)
                <= reused_at - datetime.fromisoformat(observation.checked_at)
                <= timedelta(hours=24)
            ):
                raise InspectionError("历史观察已过期或时间顺序无效。")
        if any(
            not isinstance(value[key], str)
            for key in ("page_url", "viewer_qq", "notice", "notice_source", "ready_state")
        ):
            raise InspectionError("巡检依据类型无效。")
        if (
            value["notice"] not in ("", *RESTRICTION_NOTICES, NONFRIEND_NOTICE)
            or value["notice_source"] not in _SOURCES
            or value["ready_state"] not in ("complete", "interactive", "loading", "unknown")
        ):
            raise InspectionError("巡检依据内容无效。")
        for key in ("panel_count", "report_icon_count"):
            count = value[key]
            if type(count) is not int or not 0 <= count <= 10:
                raise InspectionError("巡检依据计数无效。")
        viewer = self.metadata["viewer_qq"]
        if value["viewer_qq"] and value["viewer_qq"] != viewer:
            raise InspectionError("巡检依据登录账号不匹配。")
        page_url = value["page_url"]
        if page_url and page_url not in (
            f"https://user.qzone.qq.com/{qq}",
            f"https://user.qzone.qq.com/{qq}/",
            f"https://user.qzone.qq.com/{qq}/main",
        ):
            raise InspectionError("巡检依据页面与目标不匹配。")
        if observation.status == RESTRICTED and (
            not page_url
            or not viewer
            or value["viewer_qq"] != viewer
            or value["notice"] not in RESTRICTION_NOTICES
            or value["notice_source"] != "qzone_top_level_error"
            or value["ready_state"] != "complete"
            or value["panel_count"] != 1
            or value["report_icon_count"] != 1
        ):
            raise InspectionError("限制提示依据不完整。")
        nonfriend_page = value["notice_source"] == "qzone_nonfriend_page"
        if observation.status == UNCONFIRMED and (
            not page_url
            or not viewer
            or value["viewer_qq"] != viewer
            or value["notice"] != (NONFRIEND_NOTICE if nonfriend_page else "")
            or value["notice_source"]
            not in {
                "qzone_profile",
                "qzone_permission_page",
                "qzone_unopened_page",
                "qzone_nonfriend_page",
            }
            or value["ready_state"] != "complete"
            or value["panel_count"] != (0 if value["notice_source"] == "qzone_profile" else 1)
            or value["report_icon_count"] != (1 if nonfriend_page else 0)
        ):
            raise InspectionError("未观察到提示的页面依据不完整，不能记作已完成。")
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def validate_observation(self, observation: Observation) -> None:
        """Validate a cache candidate before using it; membership and viewer are task-bound."""
        self._validated_observation(observation)

    def reused_count(self) -> int:
        return int(
            self.db.execute(
                "SELECT COUNT(*) FROM observations WHERE json_type(evidence_json, '$.reuse')='object'"
            ).fetchone()[0]
        )

    def save(self, observation: Observation) -> None:
        if not self.metadata["prepared"] or not self.metadata["viewer_qq"]:
            raise InspectionError("请先完成快照并绑定查看账号。")
        evidence = self._validated_observation(observation)
        previous = self.db.execute(
            "SELECT status FROM observations WHERE qq=?", (observation.qq,)
        ).fetchone()
        if previous and previous[0] != BLOCKED:
            raise InspectionError("该成员已有完成记录，不会自动重复检查。")
        if self.db.execute("SELECT COUNT(*) FROM visits").fetchone()[0] >= MAX_VISITS:
            raise InspectionError("任务历史已达到支持上限，请新建任务。")
        with self.db:
            values = (
                observation.qq,
                observation.status,
                observation.reason,
                observation.checked_at,
                evidence,
            )
            cursor = self.db.execute(
                "INSERT INTO visits(qq,status,reason,checked_at,evidence_json) VALUES (?,?,?,?,?)",
                values,
            )
            self.db.execute(
                """INSERT INTO observations VALUES (?,?,?,?,?,?) ON CONFLICT(qq) DO UPDATE SET
visit_id=excluded.visit_id,status=excluded.status,reason=excluded.reason,checked_at=excluded.checked_at,evidence_json=excluded.evidence_json""",
                (observation.qq, cursor.lastrowid, *values[1:]),
            )

    def summary(self) -> dict[str, int]:
        row = self.db.execute("""SELECT COUNT(*),
COALESCE(SUM(o.status IN ('RESTRICTION_OBSERVED','UNCONFIRMED')),0),
COALESCE(SUM(o.status='RESTRICTION_OBSERVED'),0),
COALESCE(SUM(o.status='UNCONFIRMED'),0),
COALESCE(SUM(o.qq IS NULL OR o.status='BLOCKED'),0)
FROM (SELECT DISTINCT qq FROM membership) m LEFT JOIN observations o ON o.qq=m.qq""").fetchone()
        return dict(
            zip(("total", "checked", "restricted", "unconfirmed", "pending"), row, strict=True)
        )

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, object]:
        result = dict(row)
        result["evidence"] = json.loads(result.pop("evidence_json"))
        return result

    def rows(self, limit: int = 200) -> list[dict[str, object]]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise InspectionError("显示条数必须为 1 至 1000。")
        return [
            self._row(row)
            for row in self.db.execute(_SELECT_ROWS + " ORDER BY g.group_id,m.qq LIMIT ?", (limit,))
        ]

    def task_label(self) -> str:
        groups = self.db.execute(
            "SELECT name,group_id FROM groups ORDER BY group_id LIMIT 10"
        ).fetchall()
        names = "、".join(f"{row['name'][:60]}（{row['group_id']}）" for row in groups)
        created = datetime.fromisoformat(str(self.metadata["created_at"])).astimezone()
        return f"{created:%Y-%m-%d %H:%M:%S} · {names or '尚未保存群快照'}"

    @staticmethod
    def _csv_row(row: dict[str, object]) -> list[str]:
        evidence = row["evidence"]
        assert isinstance(evidence, dict)
        reuse = evidence.get("reuse", {})
        return [_safe_csv(row[key]) for key in ("group_name", "group_id", "qq")] + [
            LABELS[str(row["status"])],
            str(row["checked_at"]),
            REASONS.get(str(row["reason"]), str(row["reason"])),
            "历史观察" if reuse else "本任务访问" if row["checked_at"] else "尚未访问",
            str(reuse.get("reused_at", "")),
            str(reuse.get("source_task", "")),
        ]

    def export(self, destination_root: Path | None = None) -> Path:
        self.db.execute("BEGIN")
        try:
            header = [
                "群名",
                "群号",
                "QQ号",
                "结果",
                "时间",
                "依据",
                "观察来源",
                "复用时间",
                "来源任务",
            ]
            groups = [
                dict(row) for row in self.db.execute("SELECT * FROM groups ORDER BY group_id")
            ]
            exported_at = datetime.now(UTC).astimezone()
            if destination_root is None:
                # Retain the old low-level export contract for existing local integrations.
                target = (
                    self.folder
                    / "exports"
                    / (
                        exported_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
                        + "-"
                        + uuid.uuid4().hex[:12]
                    )
                )
            else:
                stem = (
                    group_stem(groups[0]["name"], groups[0]["group_id"]) if groups else "未封存任务"
                )
                if len(groups) > 1:
                    stem += f"_等{len(groups)}群"
                target = (
                    destination_root
                    / f"{stem}_{exported_at:%Y-%m-%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
                )
            target.mkdir(parents=True, exist_ok=False)
            metadata = self.metadata
            summary = self.summary()
            with (
                (target / "report.csv").open("x", encoding="utf-8-sig", newline="") as all_file,
                (target / "restricted.csv").open(
                    "x", encoding="utf-8-sig", newline=""
                ) as restricted_file,
                (target / "report.json").open("x", encoding="utf-8") as report,
            ):
                all_writer, restricted_writer = csv.writer(all_file), csv.writer(restricted_file)
                all_writer.writerow(header)
                restricted_writer.writerow(header)
                report.write('{"metadata":' + json.dumps(metadata, ensure_ascii=False))
                report.write(',"task_id":' + json.dumps(self.folder.name, ensure_ascii=False))
                report.write(',"exported_at":' + json.dumps(exported_at.isoformat()))
                report.write(
                    ',"scope":"成员关联依据为任务保存的群快照；非服务器实时成员证明。QQ空间提示不等同于账号永久失效。"'
                )
                report.write(',"groups":' + json.dumps(groups, ensure_ascii=False))
                report.write(',"summary":' + json.dumps(summary) + ',"rows":[')
                first = True
                for raw in self.db.execute(_SELECT_ROWS + " ORDER BY g.group_id,m.qq"):
                    row = self._row(raw)
                    csv_row = self._csv_row(row)
                    all_writer.writerow(csv_row)
                    if row["status"] == RESTRICTED:
                        restricted_writer.writerow(csv_row)
                    report.write(("" if first else ",") + json.dumps(row, ensure_ascii=False))
                    first = False
                report.write("]}")
            if destination_root is not None:
                for group in groups:
                    stem = group_stem(group["name"], group["group_id"])
                    with (
                        (target / f"{stem}_全部成员.csv").open(
                            "x", encoding="utf-8-sig", newline=""
                        ) as group_all,
                        (target / f"{stem}_观察到限制.csv").open(
                            "x", encoding="utf-8-sig", newline=""
                        ) as group_restricted,
                    ):
                        writers = (csv.writer(group_all), csv.writer(group_restricted))
                        for writer in writers:
                            writer.writerow(header)
                        for raw in self.db.execute(
                            _SELECT_ROWS + " WHERE g.group_id=? ORDER BY m.qq", (group["group_id"],)
                        ):
                            row = self._row(raw)
                            csv_row = self._csv_row(row)
                            writers[0].writerow(csv_row)
                            if row["status"] == RESTRICTED:
                                writers[1].writerow(csv_row)
            explanation = (
                "QQ 空间限制巡检结果\n"
                f"来源任务：{self.folder.name}\n"
                f"任务创建时间：{metadata['created_at']}\n"
                f"本次导出时间：{exported_at.isoformat()}（本机时区）\n"
                f"群成员来源账号：{metadata['source_self_id']}\n"
                f"空间查看账号：{metadata['viewer_qq']}\n"
                f"群快照已封存：{metadata['prepared']}\n"
                f"去重成员：{summary['total']}；已完成：{summary['checked']}；"
                f"限制提示：{summary['restricted']}；未观察到提示：{summary['unconfirmed']}；"
                f"未完成（含访问受阻）：{summary['pending']}\n\n"
                "restricted.csv 仅含明确空间违规限制提示；report.csv 包含全部快照成员。\n"
                "未观察到提示不代表账号正常；空间限制不等于 QQ 账号永久失效。\n"
                "群关联来自本次保存的成员快照，不保证服务器实时成员关系或完整性。\n"
                "report.json 的 saved_at 为群快照本地保存时间，checked_at 为页面检查时间。\n"
                f"历史观察复用：{self.reused_count()} 人；保留原检查时间，不代表本次重新访问。\n"
                "历史观察的 evidence.reuse 记录复用时间及来源任务/访问记录；限制结果也须结合原观察时间复核。\n"
                "接口群人数与快照人数可能不同；下列数值仅供核对。\n"
            )
            if destination_root is not None:
                explanation += (
                    "\n优先打开带群名和群号的 CSV：全部成员含尚未完成者；观察到限制仅含明确提示。\n"
                    "report.csv / restricted.csv 是多群合并总表，report.json 是完整结构化记录，保留供兼容核验。\n"
                    "每次导出是独立快照，不覆盖旧导出；群名过长或含文件名禁用字符时仅对文件名缩写。\n"
                    "只有存在 COMPLETE 标记时，本次导出才完整；CSV 结果不是可继续检查的任务目录。\n\n"
                )
            for group in groups:
                explanation += (
                    f"{group['name']}（群 {group['group_id']}）：接口人数 {group['declared_count']}，"
                    f"快照人数 {group['snapshot_count']}\n"
                )
            (target / "说明.txt").write_text(explanation, encoding="utf-8-sig")
            marker = target / ".complete.tmp"
            marker.write_text("export complete\n", encoding="utf-8")
            marker.rename(target / "COMPLETE")
            return target
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
            raise InspectionError("导出未完成，原任务记录仍保留。") from None
        finally:
            self.db.rollback()

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None
