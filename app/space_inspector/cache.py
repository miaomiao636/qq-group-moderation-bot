"""Local observation index. Task records remain authoritative and are never removed."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .contracts import BLOCKED, RESTRICTED, UNCONFIRMED, InspectionError, Observation, numeric_id
from .history import _ordinary
from .store import EVIDENCE_CONTRACT, Store

_APPLICATION_ID = 0x51514348
_COLUMNS = (
    "viewer",
    "qq",
    "contract",
    "checked_at",
    "source_task",
    "source_visit",
    "status",
    "reason",
    "evidence",
)


class ObservationCache:
    def __init__(self, path: Path):
        self.db: sqlite3.Connection | None = None
        self.warning = ""
        try:
            path = path.absolute()
            if (path.exists() or path.is_symlink()) and (
                not _ordinary(path, directory=False) or path.stat().st_size > 256 * 1024**2
            ):
                raise ValueError
            if not _ordinary(path.parent, directory=True):
                raise ValueError
            for suffix in ("-wal", "-shm", "-journal"):
                sidecar = path.with_name(path.name + suffix)
                if (sidecar.exists() or sidecar.is_symlink()) and (
                    not _ordinary(sidecar, directory=False)
                    or sidecar.stat().st_size > 256 * 1024**2
                ):
                    raise ValueError
            create = not path.exists()
            self.db = sqlite3.connect(path, timeout=0.1)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA trusted_schema=OFF")
            if create:
                self.db.execute("""CREATE TABLE cached (
viewer TEXT NOT NULL, qq TEXT NOT NULL, contract TEXT NOT NULL, checked_at TEXT NOT NULL,
source_task TEXT NOT NULL, source_visit INTEGER NOT NULL, status TEXT NOT NULL,
reason TEXT NOT NULL, evidence TEXT NOT NULL, PRIMARY KEY(viewer,qq,contract))""")
                self.db.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                self.db.commit()
            if self.db.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID:
                raise ValueError
            schema = self.db.execute(
                "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if [tuple(row) for row in schema] != [("table", "cached")]:
                raise ValueError
            if tuple(row[1] for row in self.db.execute("PRAGMA table_info(cached)")) != _COLUMNS:
                raise ValueError
            self.db.execute("PRAGMA journal_mode=WAL")
        except (sqlite3.Error, OSError, ValueError):
            self._disable()

    def _disable(self) -> None:
        self.close()
        self.warning = "历史索引不可用，本轮改为实时访问；原任务记录保留。"

    def remember(self, store: Store, qq: str | None = None) -> None:
        if self.db is None:
            return
        metadata = store.metadata
        try:
            query = "SELECT * FROM observations"
            rows = store.db.execute(query + (" WHERE qq=?" if qq else ""), (qq,) if qq else ())
            with self.db:
                for row in rows:
                    evidence = json.loads(row["evidence_json"])
                    # Never refresh the age or provenance by caching a reuse of a reuse.
                    if "reuse" in evidence:
                        continue
                    observed = datetime.fromisoformat(row["checked_at"])
                    if not timedelta(0) <= datetime.now(UTC) - observed <= timedelta(hours=24):
                        continue
                    self.db.execute(
                        """INSERT INTO cached VALUES (?,?,?,?,?,?,?,?,?)
ON CONFLICT(viewer,qq,contract) DO UPDATE SET checked_at=excluded.checked_at,
source_task=excluded.source_task,source_visit=excluded.source_visit,status=excluded.status,
reason=excluded.reason,evidence=excluded.evidence
WHERE julianday(excluded.checked_at)>=julianday(cached.checked_at)""",
                        (
                            metadata["viewer_qq"],
                            row["qq"],
                            EVIDENCE_CONTRACT,
                            row["checked_at"],
                            store.folder.name,
                            row["visit_id"],
                            row["status"],
                            row["reason"],
                            row["evidence_json"],
                        ),
                    )
        except (sqlite3.Error, OSError, ValueError, TypeError):
            self._disable()

    def lookup(
        self, store: Store, qq: str, hours: int, *, now: datetime | None = None
    ) -> Observation | None:
        if self.db is None or hours == 0:
            return None
        now = now or datetime.now(UTC)
        try:
            numeric_id(qq)
            if type(hours) is not int or not 1 <= hours <= 24:
                raise ValueError
            row = self.db.execute(
                "SELECT * FROM cached WHERE viewer=? AND qq=? AND contract=?",
                (store.metadata["viewer_qq"], qq, EVIDENCE_CONTRACT),
            ).fetchone()
            if row is None or row["status"] == BLOCKED or row["source_task"] == store.folder.name:
                return None
            if row["status"] not in {RESTRICTED, UNCONFIRMED} or len(row["evidence"]) > 2048:
                return None
            observed = datetime.fromisoformat(row["checked_at"])
            if not timedelta(0) <= now - observed <= timedelta(hours=hours):
                return None
            evidence = json.loads(row["evidence"])
            if type(evidence) is not dict or "reuse" in evidence:
                return None
            evidence["reuse"] = {
                "source_task": row["source_task"],
                "source_visit": row["source_visit"],
                "reused_at": now.isoformat(),
                "contract": EVIDENCE_CONTRACT,
            }
            candidate = Observation(qq, row["status"], row["reason"], row["checked_at"], evidence)
            store.validate_observation(candidate)
            return candidate
        except (ValueError, TypeError, KeyError, InspectionError):
            return None
        except sqlite3.Error:
            self._disable()
            return None

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None
