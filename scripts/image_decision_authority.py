"""A2: SQLite is the authority; JSON is a replaceable, verifiable export.

This module never migrates databases, changes policy or sends actions. Writers must
supply versions observed before the write transaction; conflicts roll back the batch.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MARKER = "image_decision_authority_version"
COLUMNS = {
    "decision_state",
    "decision_source",
    "decision_operator",
    "decision_version",
    "decided_at",
    "history_json",
}
HASH = re.compile(r"[0-9a-f]{16}")


class AuthorityError(RuntimeError):
    """Explicit failure, never an empty authority."""


class ExportPending(AuthorityError):
    """The decision committed; retry export only."""


@dataclass(frozen=True)
class Decision:
    phash: str
    state: str
    source: str
    operator: str
    note: str = ""


def _connect(db: Path, *, readonly: bool = False) -> sqlite3.Connection:
    # mode=rw also refuses to create a misspelled database.
    con = sqlite3.connect(
        db.resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True, timeout=30
    )
    con.row_factory = sqlite3.Row
    return con


def _schema(con: sqlite3.Connection) -> None:
    names = {r[1] for r in con.execute("PRAGMA table_info(image_allowlist)")}
    if not COLUMNS.issubset(names):
        raise AuthorityError("AUTHORITY_UNAVAILABLE: migrate the database explicitly")
    if not con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_settings'"
    ).fetchone():
        raise AuthorityError("AUTHORITY_UNAVAILABLE: system_settings missing")


def _rows(con: sqlite3.Connection, *, ready: bool = True) -> dict[str, dict[str, Any]]:
    _schema(con)
    if ready:
        marker = con.execute("SELECT value FROM system_settings WHERE key=?", (MARKER,)).fetchone()
        if marker is None or marker[0] != "1":
            raise AuthorityError("AUTHORITY_NOT_BACKFILLED")
    rows = {
        str(r["phash"]): dict(r)
        for r in con.execute("SELECT * FROM image_allowlist ORDER BY phash")
    }
    if ready:
        for key, row in rows.items():
            if (
                not HASH.fullmatch(key)
                or row["decision_state"] not in {"allowed", "rejected"}
                or row["decision_version"] < 1
            ):
                raise AuthorityError("AUTHORITY_NOT_BACKFILLED: invalid or undecided row")
            if bool(row["enabled"]) != (row["decision_state"] == "allowed"):
                raise AuthorityError("AUTHORITY_INCONSISTENT: enabled disagrees with decision")
            _history(row)
    return rows


def _history(row: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(row["history_json"])
    except (ValueError, TypeError) as exc:
        raise AuthorityError("AUTHORITY_INCONSISTENT: history invalid") from exc
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("events"), list)
        or "legacy_snapshot" not in data
    ):
        raise AuthorityError("AUTHORITY_INCONSISTENT: history shape invalid")
    if any(not isinstance(item, dict) for item in data["events"]):
        raise AuthorityError("AUTHORITY_INCONSISTENT: history event invalid")
    return data


def read_authority(db: Path) -> dict[str, dict[str, Any]]:
    try:
        with closing(_connect(db, readonly=True)) as con:
            con.execute("BEGIN")
            return _rows(con)
    except sqlite3.Error as exc:
        raise AuthorityError("AUTHORITY_UNAVAILABLE: database unreadable") from exc


def versions(db: Path, hashes: set[str]) -> dict[str, int]:
    rows = read_authority(db)
    return {value: int(rows[value]["decision_version"]) if value in rows else 0 for value in hashes}


def validate_decisions(decisions: list[Decision]) -> dict[str, Decision]:
    by_hash: dict[str, Decision] = {}
    for decision in decisions:
        if not HASH.fullmatch(decision.phash) or decision.state not in {"allowed", "rejected"}:
            raise AuthorityError("DECISION_INVALID")
        if len(decision.source) > 96 or len(decision.operator) > 64:
            raise AuthorityError("DECISION_INVALID: source/operator too long")
        previous = by_hash.get(decision.phash)
        if previous and (previous.state, previous.source, previous.operator) != (
            decision.state,
            decision.source,
            decision.operator,
        ):
            raise AuthorityError("DECISION_CONFLICT: contradictory duplicate hash")
        by_hash.setdefault(decision.phash, decision)
    return by_hash


def commit_decisions(
    db: Path, decisions: list[Decision], *, expected_versions: dict[str, int]
) -> None:
    from scripts.image_allowlist_seed import decision_lock

    by_hash = validate_decisions(decisions)
    if set(by_hash) != set(expected_versions):
        raise AuthorityError("DECISION_INVALID: expected versions required for every hash")
    with decision_lock(db), closing(_connect(db)) as con:
        try:
            con.execute("BEGIN IMMEDIATE")
            rows = _rows(con)
            for key in by_hash:
                actual = int(rows[key]["decision_version"]) if key in rows else 0
                if actual != expected_versions[key]:
                    raise AuthorityError("DECISION_CONFLICT: stale expected_version")
            now = datetime.now(UTC).isoformat()
            operation = uuid.uuid4().hex
            for key, decision in by_hash.items():
                prior = rows.get(key)
                version = expected_versions[key] + 1
                history = _history(prior) if prior else {"legacy_snapshot": None, "events": []}
                note = decision.note[:64] if decision.note else str(prior["note"]) if prior else ""
                history["events"].append(
                    {
                        "version": version,
                        "operation": operation,
                        "state": "approved" if decision.state == "allowed" else "rejected",
                        "source": decision.source,
                        "operator": decision.operator,
                        "at": now,
                        "note": note,
                        "previous_note": str(prior["note"]) if prior else "",
                    }
                )
                values = (
                    int(decision.state == "allowed"),
                    decision.state,
                    decision.source,
                    decision.operator,
                    version,
                    now,
                    json.dumps(history, ensure_ascii=False, sort_keys=True),
                    note,
                )
                if prior:
                    result = con.execute(
                        "UPDATE image_allowlist SET enabled=?,decision_state=?,decision_source=?,decision_operator=?,decision_version=?,decided_at=?,history_json=?,note=? WHERE phash=? AND decision_version=?",
                        (*values, key, expected_versions[key]),
                    )
                    if result.rowcount != 1:
                        raise AuthorityError("DECISION_CONFLICT: CAS failed")
                else:
                    con.execute(
                        "INSERT INTO image_allowlist(enabled,decision_state,decision_source,decision_operator,decision_version,decided_at,history_json,note,phash,source,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (*values, key, decision.source[:16], now, decision.operator),
                    )
            con.commit()
        except BaseException:
            con.rollback()
            raise


def snapshot_payload(rows: dict[str, dict[str, Any]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, row in rows.items():
        journal = _history(row)
        legacy = journal["legacy_snapshot"]
        entry = copy.deepcopy(legacy) if isinstance(legacy, dict) else {}
        history = list(entry.get("history") or []) + copy.deepcopy(journal["events"])
        first = entry.get("first_rejected") or next(
            (x.get("at", "") for x in history if x.get("state") == "rejected"), ""
        )
        entry.update(
            state="approved" if row["decision_state"] == "allowed" else "rejected",
            source=row["decision_source"],
            operator=row["decision_operator"],
            at=row["decided_at"],
            first_rejected=first,
            history=history,
            decision_version=row["decision_version"],
        )
        output[key] = entry
    return output


def export_snapshot(db: Path) -> None:
    from scripts.image_allowlist_seed import _write_snapshot, decision_lock, rejection_snapshot_path

    # Read AFTER acquiring the same cross-process lock as every decision writer.
    with decision_lock(db):
        payload = snapshot_payload(read_authority(db))
        path = rejection_snapshot_path(db)
        if path.exists():
            raw = path.read_bytes()
            try:
                previous = json.loads(raw)
                if not isinstance(previous, dict):
                    raise ValueError("not object")
            except ValueError:
                # Derived corrupt artifacts remain available for diagnosis.
                backup = path.with_name(path.name + ".invalid-" + uuid.uuid4().hex)
                with backup.open("xb") as handle:
                    handle.write(raw)
        _write_snapshot(path, payload)


def observe_authority(db: Path) -> tuple[dict[str, dict[str, Any]], str]:
    """One coordinated observation; only the persistent OS lock file is opened.

    SQLite and JSON contents are read-only. Holding the same lock as all writers
    prevents pairing a prior DB snapshot with a later derived export.
    """
    from scripts.image_allowlist_seed import decision_lock, rejection_snapshot_path

    with decision_lock(db):
        rows = read_authority(db)
        expected = snapshot_payload(rows)
        try:
            raw = json.loads(rejection_snapshot_path(db).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return rows, "missing"
        except (OSError, ValueError):
            return rows, "corrupt"
        return rows, "ok" if raw == expected else "stale"


def export_state(db: Path) -> str:
    return observe_authority(db)[1]


def apply_decisions(
    db: Path, decisions: list[Decision], *, expected_versions: dict[str, int]
) -> None:
    from scripts.image_allowlist_seed import decision_lock

    with decision_lock(db):
        commit_decisions(db, decisions, expected_versions=expected_versions)
        try:
            export_snapshot(db)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            raise ExportPending("DECISIONS_COMMITTED_EXPORT_PENDING: retry --export-only") from exc


def _legacy_entries(db: Path) -> dict[str, Any]:
    from scripts.image_allowlist_seed import _read_snapshot, rejection_snapshot_path

    data = _read_snapshot(rejection_snapshot_path(db))
    for key, entry in data.items():
        if (
            not HASH.fullmatch(key)
            or not isinstance(entry, dict)
            or entry.get("state", "rejected") not in {"approved", "rejected"}
        ):
            raise AuthorityError("BACKFILL_CONFLICT: invalid legacy entry")
        history = entry.get("history", [])
        if not isinstance(history, list) or any(not isinstance(e, dict) for e in history):
            raise AuthorityError("BACKFILL_CONFLICT: invalid legacy history")
    return data


def backfill(db: Path, *, apply: bool = False) -> dict[str, Any]:
    from scripts.image_allowlist_seed import decision_lock

    # Dry-run is read-only: no lock files, no journal or schema changes.
    def run() -> dict[str, Any]:
        with closing(_connect(db, readonly=not apply)) as con:
            con.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
            rows = _rows(con, ready=False)
            marker = con.execute(
                "SELECT value FROM system_settings WHERE key=?", (MARKER,)
            ).fetchone()
            if marker is not None and marker[0] != "1":
                raise AuthorityError("BACKFILL_CONFLICT: unsupported authority version")
            if marker is not None and marker[0] == "1":
                _rows(con)
                return {"status": "already_backfilled", "rows": len(rows), "changed": 0}
            legacy = _legacy_entries(db)
            planned: list[tuple[str, str, str, str, str | None, str]] = []
            conflicts: list[str] = []
            for key in sorted(set(rows) | set(legacy)):
                row, entry = rows.get(key), legacy.get(key)
                if not HASH.fullmatch(key):
                    conflicts.append(key)
                    continue
                if row and int(row["decision_version"]) != 0:
                    conflicts.append(key)
                    continue
                state = "allowed" if row and row["enabled"] else "rejected"
                if entry is not None:
                    legacy_allowed = entry.get("state", "rejected") == "approved"
                    if legacy_allowed != bool(row and row["enabled"]):
                        conflicts.append(key)
                        continue
                source = str(
                    entry.get("source", "")
                    if entry is not None
                    else (row["source"] or "legacy-enabled")
                    if row
                    else "legacy"
                )
                if entry is None and row:
                    batches = set(re.findall(r"(batch-[^:;\s]+):no=\d+", str(row["note"])))
                    if len(batches) > 1:
                        conflicts.append(key)
                        continue
                    if batches:
                        source = "review:" + next(iter(batches))
                operator = str(
                    entry.get("operator", "")
                    if entry is not None
                    else row["created_by"]
                    if row
                    else ""
                )
                # A row creation time is not evidence of a later review decision.
                # Preserve the old value in legacy_row, but do not invent decided_at.
                at = str(entry.get("at") or "") if entry is not None else ""
                if at:
                    try:
                        datetime.fromisoformat(at.replace("Z", "+00:00"))
                    except ValueError:
                        conflicts.append(key)
                        continue
                else:
                    at = None
                if len(source) > 96 or len(operator) > 64:
                    conflicts.append(key)
                    continue
                history = json.dumps(
                    {"legacy_snapshot": entry, "legacy_row": row, "events": []},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                planned.append((key, state, source, operator, at, history))
            if conflicts:
                raise AuthorityError("BACKFILL_CONFLICT: " + ",".join(conflicts))
            before = {k for k, r in rows.items() if r["enabled"]}
            if apply:
                for key, state, source, operator, at, history in planned:
                    if key in rows:
                        con.execute(
                            "UPDATE image_allowlist SET decision_state=?,decision_source=?,decision_operator=?,decision_version=1,decided_at=?,history_json=? WHERE phash=? AND decision_version=0",
                            (state, source, operator, at, history, key),
                        )
                    else:
                        con.execute(
                            "INSERT INTO image_allowlist(phash,note,source,enabled,created_at,created_by,decision_state,decision_source,decision_operator,decision_version,decided_at,history_json) VALUES(?,'',?,0,?,?,'rejected',?,?,1,?,?)",
                            (
                                key,
                                source[:16],
                                datetime.now(UTC).isoformat(),
                                operator,
                                source,
                                operator,
                                at,
                                history,
                            ),
                        )
                con.execute(
                    "INSERT INTO system_settings(key,value,updated_at) VALUES(?,'1',?) ON CONFLICT(key) DO UPDATE SET value='1',updated_at=excluded.updated_at",
                    (MARKER, datetime.now(UTC).isoformat()),
                )
                after = {
                    r[0] for r in con.execute("SELECT phash FROM image_allowlist WHERE enabled=1")
                }
                if before != after:
                    raise AuthorityError("BACKFILL_CONFLICT: active set changed")
                _rows(con)
                con.commit()
            digest = hashlib.sha256(json.dumps(sorted(before)).encode()).hexdigest()
            return {
                "status": "applied" if apply else "dry_run",
                "rows": len(planned),
                "changed": len(planned),
                "enabled_digest": digest,
                "decisions": [
                    {"phash": p[0], "state": p[1], "source": p[2], "version": 1} for p in planned
                ],
            }

    if not apply:
        return run()
    with decision_lock(db):
        return run()
