"""Explicit offline backup and isolated recovery. Never opens QQ or the main database."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import time
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.phone_inspector.locks import FileLock
from app.reports.backup_paths import checked, private_directory, regular
from app.reports.backup_source import safe_name
from app.space_inspector.cache import _APPLICATION_ID as CACHE_ID
from app.space_inspector.cache import _COLUMNS as CACHE_COLUMNS
from app.space_inspector.export_paths import group_stem
from app.space_inspector.store import _COLUMNS, APPLICATION_ID, SCHEMA_VERSION, Store

TASK_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}")
MAX_FILES = 100_000
MAX_DB = 2 * 1024**3
MAX_CACHE = 256 * 1024**2


def _json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)


def _read_json(path: Path, limit: int = 32 * 1024**2) -> Any:
    with regular(path).open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("JSON_TOO_LARGE")
    return json.loads(data)


def _disjoint(target: Path, roots: list[Path]) -> Path:
    target = checked(target, exists=False)
    if target.exists() or any(
        target == p or target in p.parents or p in target.parents for p in roots
    ):
        raise ValueError("NEW_SEPARATE_TARGET_REQUIRED")
    return target


class Budget:
    def __init__(self, target: Path, maximum: int, reserve: int, seconds: float):
        if maximum <= 0 or reserve < 0 or seconds <= 0:
            raise ValueError("INVALID_BACKUP_LIMITS")
        self.target, self.maximum, self.reserve = target, maximum, reserve
        self.deadline, self.used = time.monotonic() + seconds, 0

    def check(self, additional: int = 0) -> None:
        if (
            time.monotonic() >= self.deadline
            or self.used + additional > self.maximum
            or shutil.disk_usage(self.target).free < self.reserve + additional
        ):
            raise ValueError("BACKUP_TIME_OR_CAPACITY_LIMIT")


def _copy(source: Path, target: Path | None, budget: Budget) -> tuple[str, int]:
    source = regular(source)
    before = source.stat()
    budget.check(before.st_size if target else 0)
    if target:
        checked(target, exists=False)
        target.parent.mkdir(parents=True, exist_ok=True)
    digest, size = hashlib.sha256(), 0
    with ExitStack() as stack:
        reader = stack.enter_context(source.open("rb"))
        writer = stack.enter_context(target.open("xb")) if target else None
        while block := reader.read(1024**2):
            size += len(block)
            budget.check(len(block) if target else 0)
            if target is not None and size + budget.used > budget.maximum:
                raise ValueError("BACKUP_CAPACITY_LIMIT")
            digest.update(block)
            if writer:
                writer.write(block)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
        size,
        after.st_mtime_ns,
        after.st_ino,
    ):
        raise ValueError("SOURCE_CHANGED_DURING_BACKUP")
    if target:
        budget.used += size
    return digest.hexdigest(), size


def _check_db(path: Path, cache: bool, budget: Budget) -> None:
    if regular(path).stat().st_size > (MAX_CACHE if cache else MAX_DB):
        raise ValueError("INSPECTOR_DATABASE_TOO_LARGE")
    with closing(sqlite3.connect(regular(path).as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        db.set_progress_handler(lambda: int(time.monotonic() >= budget.deadline), 1000)
        columns = {"cached": CACHE_COLUMNS} if cache else _COLUMNS
        if db.execute("PRAGMA application_id").fetchone()[0] != (
            CACHE_ID if cache else APPLICATION_ID
        ):
            raise ValueError("INSPECTOR_DATABASE_ID")
        if not cache and db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError("INSPECTOR_DATABASE_VERSION")
        schema = db.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if cache and [(row[0], row[1]) for row in schema] != [("table", "cached")]:
            raise ValueError("INSPECTOR_DATABASE_SCHEMA")
        if any(
            row[0] not in {"table", "index"}
            or (row[0] == "table" and "VIRTUAL" in str(row[2]).upper())
            for row in schema
        ) or {row[1] for row in schema if row[0] == "table"} != set(columns):
            raise ValueError("INSPECTOR_DATABASE_SCHEMA")
        for table, expected in columns.items():
            if tuple(row[1] for row in db.execute(f"PRAGMA table_info({table})")) != expected:
                raise ValueError("INSPECTOR_DATABASE_COLUMNS")
        if (
            db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
            or db.execute("PRAGMA foreign_key_check").fetchone()
        ):
            raise ValueError("INSPECTOR_DATABASE_INTEGRITY")
        if not cache:
            Store.validate_connection(db)


def _snapshot(source: Path, target: Path, cache: bool, budget: Budget) -> None:
    source = regular(source)
    if source.stat().st_size > (MAX_CACHE if cache else MAX_DB):
        raise ValueError("INSPECTOR_DATABASE_TOO_LARGE")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = checked(source.with_name(source.name + suffix), exists=False)
        if sidecar.exists():
            regular(sidecar)
            if sidecar.stat().st_size > MAX_DB:
                raise ValueError("INSPECTOR_DATABASE_TOO_LARGE")
    target.parent.mkdir(parents=True, exist_ok=True)
    budget.check(source.stat().st_size)
    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=1)) as origin,
        closing(sqlite3.connect(target)) as copy,
    ):

        def progress(status: int, remaining: int, total: int) -> None:
            budget.check()
            if target.stat().st_size + budget.used > budget.maximum:
                raise ValueError("BACKUP_CAPACITY_LIMIT")

        origin.backup(copy, pages=256, progress=progress)
    _check_db(target, cache, budget)
    budget.used += target.stat().st_size
    budget.check()


def _export_files(folder: Path, task_ids: set[str]) -> list[Path] | None:
    checked(folder)
    if not folder.is_dir() or not (folder / "report.json").exists():
        return None
    report = _read_json(folder / "report.json", 128 * 1024**2)
    if not isinstance(report, dict) or report.get("task_id") not in task_ids:
        return None
    with regular(folder / "COMPLETE").open("rb") as stream:
        complete = stream.read(64)
    if complete not in {
        b"export complete\n",
        b"export complete\r\n",
    }:
        raise ValueError("INCOMPLETE_INSPECTOR_EXPORT")
    allowed = {"report.json", "report.csv", "restricted.csv", "说明.txt", "COMPLETE"}
    groups = report.get("groups")
    if not isinstance(groups, list) or len(groups) > 2000:
        raise ValueError("INVALID_EXPORT_GROUPS")
    for group in groups:
        stem = group_stem(group["name"], group["group_id"])
        allowed.update({stem + "_全部成员.csv", stem + "_观察到限制.csv"})
    present = {p.name: p for p in folder.iterdir()}
    if not {"report.json", "report.csv", "restricted.csv", "COMPLETE"}.issubset(present):
        raise ValueError("INCOMPLETE_INSPECTOR_EXPORT")
    if set(present).difference(allowed):
        raise ValueError("UNEXPECTED_FILE_IN_INSPECTOR_EXPORT")
    return [regular(p) for p in present.values()]


def create_backup(
    root: Path,
    target: Path,
    export_roots: list[Path],
    *,
    reserve: int = 1024**3,
    maximum: int = 20 * 1024**3,
    seconds: float = 1800,
    owner_sid: str | None = None,
) -> dict[str, Any]:
    root = checked(root)
    if not root.is_dir() or not export_roots:
        raise ValueError("EXPLICIT_INSPECTOR_AND_EXPORT_ROOTS_REQUIRED")
    roots = list(dict.fromkeys(checked(p) for p in export_roots))
    if any(not p.is_dir() or p == root or root in p.parents for p in roots):
        raise ValueError("INVALID_EXPORT_ROOT")
    target = _disjoint(target, [root, *roots])
    with ExitStack() as locks:
        locks.enter_context(FileLock(checked(root / "desktop.lock", exists=False)))
        tasks_root = checked(root / "tasks")
        tasks = sorted(tasks_root.iterdir())
        if len(tasks) > 20_000 or any(
            not TASK_ID.fullmatch(p.name) or not checked(p).is_dir() for p in tasks
        ):
            raise ValueError("INVALID_TASK_DIRECTORY")
        for task in tasks:
            locks.enter_context(FileLock(checked(task / "task.lock", exists=False)))
        settings = checked(root / "export-settings.json", exists=False)
        if settings.exists():
            value = _read_json(settings, 16 * 1024)
            if (
                not isinstance(value, dict)
                or set(value) != {"export_root"}
                or not isinstance(value["export_root"], str)
            ):
                raise ValueError("INVALID_EXPORT_SETTINGS")
            if checked(Path(value["export_root"])) not in roots:
                raise ValueError("CONFIGURED_EXPORT_ROOT_NOT_INCLUDED")
        private_directory(target, owner_sid)
        budget = Budget(target, maximum, reserve, seconds)
        entries: list[dict[str, Any]] = []

        def record(name: str, source: Path, database: bool = False, cache: bool = False) -> None:
            if not safe_name(name) or len(entries) >= MAX_FILES:
                raise ValueError("BACKUP_FILE_LIMIT")
            output = target / name
            if database:
                _snapshot(source, output, cache, budget)
                digest, size = _copy(output, None, budget)
            else:
                digest, size = _copy(source, output, budget)
            entries.append({"path": name, "sha256": digest, "bytes": size})

        for task in tasks:
            record(f"inspector/tasks/{task.name}/task.sqlite3", task / "task.sqlite3", True)
        cache = checked(root / "observations.sqlite3", exists=False)
        if cache.exists():
            record("inspector/observations.sqlite3", cache, True, True)
        if settings.exists():
            record("settings/export-settings.json", settings)
        task_ids = {p.name for p in tasks}
        candidates = [(f"root-{i}", p) for i, p in enumerate(roots)]
        for task in tasks:
            old_exports = checked(task / "exports", exists=False)
            if old_exports.exists():
                candidates.append((f"legacy-{task.name}", old_exports))
        excluded = 0
        for label, directory in candidates:
            children = sorted(directory.iterdir())
            if len(children) > MAX_FILES:
                raise ValueError("EXPORT_DIRECTORY_LIMIT")
            for folder in children:
                budget.check()
                files = _export_files(folder, task_ids)
                if files is None:
                    excluded += 1
                    continue
                for file in files:
                    record(f"exports/{label}/{folder.name}/{file.name}", file)
        manifest = {
            "format": 1,
            "kind": "qqspace-offline-backup",
            "created_at": datetime.now(UTC).isoformat(),
            "source_root": str(root),
            "export_roots": [str(p) for p in roots],
            "task_count": len(tasks),
            "excluded_export_entries": excluded,
            "files": entries,
            "exports_scope": "only_declared_roots_and_legacy_task_exports",
            "excluded": ["browser", "runtime", "locks", "unknown_roots"],
        }
        _json(target / "manifest.json", manifest)
        digest = hashlib.sha256((target / "manifest.json").read_bytes()).hexdigest()
        _json(target / "COMPLETE.json", {"manifest_sha256": digest})
        return {
            "status": "success",
            "task_count": len(tasks),
            "file_count": len(entries),
            "excluded_export_entries": excluded,
            "manifest_sha256": digest,
        }


def _allowed_restore_name(name: str) -> bool:
    parts = Path(name).parts
    return (
        name in {"settings/export-settings.json", "inspector/observations.sqlite3"}
        or (
            len(parts) == 4
            and parts[:2] == ("inspector", "tasks")
            and bool(TASK_ID.fullmatch(parts[2]))
            and parts[3] == "task.sqlite3"
        )
        or (
            len(parts) == 4
            and parts[0] == "exports"
            and (
                parts[3] in {"report.json", "report.csv", "restricted.csv", "说明.txt", "COMPLETE"}
                or parts[3].endswith(("_全部成员.csv", "_观察到限制.csv"))
            )
        )
    )


def restore_backup(
    source: Path,
    target: Path,
    *,
    reserve: int = 1024**3,
    maximum: int = 20 * 1024**3,
    seconds: float = 1800,
    owner_sid: str | None = None,
) -> dict[str, Any]:
    source = checked(source)
    marker = _read_json(source / "COMPLETE.json", 1024)
    with regular(source / "manifest.json").open("rb") as stream:
        raw = stream.read(32 * 1024**2 + 1)
    if len(raw) > 32 * 1024**2 or hashlib.sha256(raw).hexdigest() != marker.get("manifest_sha256"):
        raise ValueError("BACKUP_MANIFEST_HASH")
    manifest = json.loads(raw)
    if manifest.get("format") != 1 or manifest.get("kind") != "qqspace-offline-backup":
        raise ValueError("BACKUP_FORMAT")
    roots = [
        source,
        checked(Path(manifest["source_root"]), exists=False),
        *(checked(Path(p), exists=False) for p in manifest["export_roots"]),
    ]
    target = _disjoint(target, roots)
    entries = manifest.get("files")
    if not isinstance(entries, list) or len(entries) > MAX_FILES:
        raise ValueError("BACKUP_FILE_LIST")
    seen: set[str] = set()
    for entry in entries:
        name, size, digest = entry.get("path"), entry.get("bytes"), entry.get("sha256")
        if (
            not isinstance(name, str)
            or not safe_name(name)
            or not _allowed_restore_name(name)
            or name.casefold() in seen
            or type(size) is not int
            or not 0 <= size <= maximum
            or not isinstance(digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
        ):
            raise ValueError("BACKUP_FILE_ENTRY")
        seen.add(name.casefold())
    if sum(e["bytes"] for e in entries) > maximum:
        raise ValueError("BACKUP_CAPACITY_LIMIT")
    task_ids = {
        Path(e["path"]).parts[2] for e in entries if e["path"].startswith("inspector/tasks/")
    }
    if type(manifest.get("task_count")) is not int or manifest["task_count"] != len(task_ids):
        raise ValueError("BACKUP_TASK_COUNT")
    private_directory(target, owner_sid)
    budget = Budget(target, maximum, reserve, seconds)
    for entry in entries:
        name = entry["path"]
        actual = _copy(source / name, target / name, budget)
        if actual != (entry["sha256"], entry["bytes"]):
            raise ValueError("BACKUP_FILE_HASH")
        if name.endswith(".sqlite3"):
            _check_db(target / name, name.endswith("observations.sqlite3"), budget)
    export_folders = {
        (target / e["path"]).parent for e in entries if e["path"].startswith("exports/")
    }
    for folder in export_folders:
        if _export_files(folder, task_ids) is None:
            raise ValueError("BACKUP_EXPORT_TASK")
    receipt = {
        "status": "verified",
        "file_count": len(entries),
        "task_count": manifest["task_count"],
        "manifest_sha256": marker["manifest_sha256"],
        "settings": "audit_copy_not_activated",
    }
    _json(target / "RESTORE_VERIFIED.json", receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--root", type=Path, required=True)
    create.add_argument("--export-root", type=Path, action="append", required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("--source", type=Path, required=True)
    for command in (create, restore):
        command.add_argument("--to", type=Path, required=True)
        command.add_argument("--owner-sid")
    args = parser.parse_args()
    try:
        result = (
            create_backup(args.root, args.to, args.export_root, owner_sid=args.owner_sid)
            if args.command == "create"
            else restore_backup(args.source, args.to, owner_sid=args.owner_sid)
        )
    except Exception:
        # Never print exception details containing private roots, identities or content.
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": "INSPECTOR_BACKUP_FAILED",
                    "hint": "Close inspector windows; check explicit roots, complete exports, fresh target, capacity and file integrity.",
                }
            )
        )
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
