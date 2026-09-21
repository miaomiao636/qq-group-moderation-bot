"""Read-only N03 preflight, explicitly invoked and never used by cleanup.

Only source timestamps and media references are projected out of database JSON.
File contents are not opened. Paths are represented by SHA-256 of scope + relative
path, because legacy filenames can themselves contain QQ numbers or original text.
mtime is diagnostic only. No result authorizes deletion or proves N03 closure.
The SQL transaction is a consistent read snapshot; the filesystem inventory is a
bounded live observation, not an atomic snapshot of a running application.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.fs_guard import chain_has_link  # noqa: E402

_TABLES = (
    ("shadow_decisions", "detail_json", "media_files", "name"),
    ("violation_records", "message_snapshot_json", "attachments", "filename"),
)
_DIRECTORIES = frozenset(
    {"backups", "sample_pool", "t002_media", "_dbg_tmp", "purge_drill", "loadtest"}
)
_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".mp4",
        ".mp3",
        ".wav",
        ".db",
        ".json",
        ".jsonl",
        ".txt",
        ".bin",
        ".partial",
        ".part",
    }
)


class AuditError(ValueError):
    """Only fixed, non-sensitive reason codes may be surfaced."""


def _absolute(path: Path) -> Path:
    if ".." in path.parts:
        raise AuditError("unsafe_input_path")
    return path.absolute()


def _check_chain(path: Path) -> None:
    if chain_has_link(Path(path.anchor), path):
        raise AuditError("unsafe_input_path")


def _date(value: object, *, aware_required: bool = True) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            if aware_required:
                return None
            result = result.replace(tzinfo=UTC)
        return result.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _file_name(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 255:
        return None
    if value in {".", ".."} or any(c in value for c in "/\\:\x00"):
        return None
    return value


@dataclass(frozen=True)
class Source:
    stamp: datetime | None
    quality: str


def _source_projection(table: str, column: str, array: str, field: str) -> str:
    # All identifiers/JSON paths come exclusively from the closed _TABLES tuple.
    obj = f"CASE WHEN json_valid({column}) THEN CASE WHEN json_type({column})='object' THEN {column} ELSE '{{}}' END ELSE '{{}}' END"
    valid = f"CASE WHEN json_valid({column}) THEN json_type({column})='object' ELSE 0 END"
    entry = "CASE WHEN j.type='object' THEN j.value ELSE '{}' END"
    return f"""
        WITH sources AS (
            SELECT rowid AS record_id, {obj} AS obj, {valid} AS valid, created_at
            FROM {table}
        )
        SELECT record_id, valid,
            CASE WHEN typeof(created_at)='text' AND length(created_at)<=64 THEN created_at END,
            json_type(obj, '$.sent_at'),
            CASE WHEN json_type(obj, '$.sent_at')='text' AND length(json_extract(obj, '$.sent_at'))<=64
                THEN json_extract(obj, '$.sent_at') END,
            json_type(obj, '$.{array}'), j.type,
            json_type({entry}, '$.{field}'),
            CASE WHEN json_type({entry}, '$.{field}')='text' AND length(json_extract({entry}, '$.{field}'))<=255
                THEN json_extract({entry}, '$.{field}') END
        FROM sources LEFT JOIN json_each(
            CASE WHEN json_type(obj, '$.{array}')='array'
                THEN json_extract(obj, '$.{array}') ELSE '[]' END
        ) AS j ON 1 ORDER BY record_id
    """


def _read_sources(
    db: Path, now: datetime, days: int, max_sources: int, max_references: int
) -> tuple[dict[str, list[Source]], dict[str, int], list[str]]:
    refs: dict[str, list[Source]] = {}
    counts: Counter[str] = Counter(
        {
            "sources": 0,
            "invalid_payload": 0,
            "invalid_file_references": 0,
            "expired_source_processed_recent": 0,
        }
    )
    warnings: list[str] = []
    deadline = time.monotonic() + 30
    try:
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=1)) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            connection.execute("BEGIN")
            for table, column, array, field in _TABLES:
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if not exists:
                    warnings.append(f"missing_{table}")
                    continue
                previous_id: int | None = None
                source = Source(None, "missing")
                record_files: set[str] = set()
                for (
                    record_id,
                    valid,
                    created_raw,
                    sent_type,
                    sent_raw,
                    array_type,
                    entry_type,
                    filename_type,
                    filename,
                ) in connection.execute(_source_projection(table, column, array, field)):
                    counts["reference_rows"] += 1
                    if counts["reference_rows"] > max_references:
                        raise AuditError("reference_limit_exceeded")
                    if record_id != previous_id:
                        previous_id = record_id
                        record_files.clear()
                        counts["sources"] += 1
                        if counts["sources"] > max_sources:
                            raise AuditError("source_limit_exceeded")
                        counts["invalid_payload"] += int(not valid)
                        counts["invalid_media_shape"] += int(array_type not in (None, "array"))
                        stamp = _date(sent_raw)
                        quality = "known"
                        if sent_type in (None, "null") or sent_raw == "":
                            quality = "missing"
                        elif stamp is None:
                            quality = "invalid"
                        elif stamp > now:
                            quality = "future"
                            stamp = None
                        source = Source(stamp, quality)
                        counts[f"source_time_{quality}"] += 1
                        created = _date(created_raw, aware_required=False)
                        if stamp and stamp + timedelta(days=days) <= now:
                            counts["known_source_expired"] += 1
                            if created and created > now - timedelta(days=days):
                                counts["expired_source_processed_recent"] += 1
                    if entry_type is None:
                        continue
                    name = _file_name(filename) if filename_type == "text" else None
                    if name is None:
                        counts["invalid_file_references"] += 1
                    elif name not in record_files:
                        record_files.add(name)
                        refs.setdefault(name, []).append(source)
            connection.rollback()
    except sqlite3.Error:
        raise AuditError("database_read_failed_or_timed_out") from None
    return refs, dict(counts), warnings


def _identifier(scope: str, relative: Path) -> str:
    return hashlib.sha256(f"{scope}/{relative.as_posix()}".encode()).hexdigest()


def audit(
    db: Path,
    data_root: Path,
    *,
    as_of: str,
    days: int = 15,
    evidence_roots: Sequence[Path] = (),
    max_entries: int = 100_000,
    max_sources: int = 100_000,
    max_references: int = 500_000,
) -> dict[str, Any]:
    now = _date(as_of)
    if now is None or not 1 <= days <= 3650 or min(max_entries, max_sources, max_references) <= 0:
        raise AuditError("invalid_audit_parameters")
    db, data_root = _absolute(db), _absolute(data_root)
    roots = [_absolute(root) for root in evidence_roots]
    for path in (db, data_root, *roots):
        _check_chain(path)
    if not db.is_file() or not data_root.is_dir() or any(not root.is_dir() for root in roots):
        raise AuditError("missing_or_invalid_input")
    refs, summary, warnings = _read_sources(db, now, days, max_sources, max_references)
    files: list[dict[str, Any]] = []
    blocked: list[dict[str, str]] = []
    visited_files: set[Path] = set()
    matched_names: set[str] = set()
    entries_seen = 0

    def blocked_item(path: Path, scope: str, root: Path) -> None:
        blocked.append(
            {
                "scope": scope,
                "path_sha256": _identifier(scope, path.relative_to(root)),
                "reason": "link_or_unreadable",
            }
        )

    def children(directory: Path) -> list[Path]:
        nonlocal entries_seen
        result = []
        try:
            _check_chain(directory)
            with os.scandir(directory) as iterator:
                for item in iterator:
                    entries_seen += 1
                    if entries_seen > max_entries:
                        raise AuditError("entry_limit_exceeded")
                    result.append(Path(item.path))
        except OSError:
            raise AuditError("filesystem_scan_failed") from None
        return sorted(result)

    def observe(path: Path, scope: str, root: Path) -> None:
        if path in visited_files:
            return
        visited_files.add(path)
        if chain_has_link(Path(path.anchor), path):
            blocked_item(path, scope, root)
            return
        try:
            stat = path.stat(follow_symlinks=False)
            mtime = datetime.fromtimestamp(stat.st_mtime, UTC)
        except (OSError, ValueError, OverflowError):
            blocked_item(path, scope, root)
            return
        sources = refs.get(path.name, []) if scope == "media" else []
        if sources:
            matched_names.add(path.name)
        known = [source.stamp for source in sources if source.stamp is not None]
        unknown = sum(source.quality != "known" for source in sources)
        oldest = min(known) if known else None
        expires = oldest + timedelta(days=days) if oldest and not unknown else None
        if scope == "backups":
            status = "backup_content_not_inspected"
        elif scope == "sample_pool":
            status = "sample_content_not_inspected"
        elif scope.startswith("external_evidence"):
            status = "evidence_content_not_inspected"
        elif scope != "media":
            status = "copy_content_not_inspected"
        elif not sources:
            status = "no_source_reference"
        elif unknown:
            status = "source_time_unknown_review_required"
        elif expires is not None and expires <= now:
            status = "known_source_expired_review_required"
        else:
            status = "known_source_within_window_review_required"
        files.append(
            {
                "scope": scope,
                "path_sha256": _identifier(scope, path.relative_to(root)),
                "extension": path.suffix.lower() if path.suffix.lower() in _EXTENSIONS else "other",
                "bytes": stat.st_size,
                "mtime": mtime.isoformat(),
                "status": status,
                "source_references": len(sources),
                "unknown_source_references": unknown,
                "oldest_known_source_at": oldest.isoformat() if oldest else None,
                "expires_at": expires.isoformat() if expires else None,
                "mtime_after_source": bool(oldest and mtime > oldest),
                "safe_to_delete": False,
            }
        )

    def walk(target: Path, scope: str, root: Path) -> None:
        stack = [target]
        while stack:
            path = stack.pop()
            if chain_has_link(Path(path.anchor), path):
                blocked_item(path, scope, root)
                continue
            if path.is_dir():
                stack.extend(reversed(children(path)))
            elif path.is_file():
                observe(path, scope, root)

    for item in children(data_root):
        name = item.name
        if name == "media":
            if chain_has_link(Path(item.anchor), item):
                blocked_item(item, "media", data_root)
                continue
            if not item.is_dir():
                warnings.append("media_root_not_directory")
                continue
            for media in children(item):
                if chain_has_link(Path(media.anchor), media):
                    blocked_item(media, "media", data_root)
                elif media.is_file():
                    observe(media, "media", data_root)
                elif media.name == "_frames":
                    walk(media, "media_frames", data_root)
        elif name in _DIRECTORIES:
            walk(item, name, data_root)
        elif name.startswith("media_snapshot_"):
            walk(item, "media_snapshot", data_root)
        elif name.startswith("backup_shadow_") and name.endswith(".json"):
            walk(item, "backup_shadow", data_root)
    for index, root in enumerate(roots, 1):
        walk(root, f"external_evidence_{index}", root)
    return {
        "schema_version": 1,
        "status": "inventory_completed",
        "as_of": now.isoformat(),
        "retention_days": days,
        "read_only": True,
        "deletion_authorized": False,
        "n03_closed": False,
        "filesystem_snapshot_atomic": False,
        "scope_note": "Only explicit known roots; no file contents inspected; missing sources and backups require review.",
        "path_identifier": "sha256(scope + '/' + relative POSIX path); original paths are omitted",
        "source_summary": summary,
        "warnings": warnings,
        "files": files,
        "blocked_entries": blocked,
        "entries_seen": entries_seen,
        "unmatched_reference_names": len(set(refs) - matched_names),
    }


def _write_report(report: dict[str, Any], output: Path, inputs: Sequence[Path]) -> None:
    output = _absolute(output)
    _check_chain(output.parent)
    if any(output == path or path in output.parents for path in inputs):
        raise AuditError("output_inside_audited_inputs")
    if output.exists() or output.is_symlink():
        raise AuditError("output_already_exists")
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="retention-audit-", suffix=".partial", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        _check_chain(output.parent)
        try:
            os.link(temporary, output)  # Atomic exclusive publication; no overwrite.
        except FileExistsError:
            raise AuditError("output_already_exists") from None
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only source-time retention inventory; never deletes files or closes N03."
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--as-of", required=True, help="Timezone-aware ISO 8601 observation time")
    parser.add_argument(
        "--out", type=Path, required=True, help="New report outside all audited inputs"
    )
    parser.add_argument("--days", type=int, default=15)
    parser.add_argument("--evidence-root", type=Path, action="append", default=[])
    parser.add_argument("--max-entries", type=int, default=100_000)
    parser.add_argument("--max-sources", type=int, default=100_000)
    parser.add_argument("--max-references", type=int, default=500_000)
    args = parser.parse_args(argv)
    try:
        report = audit(
            args.db,
            args.data_root,
            as_of=args.as_of,
            days=args.days,
            evidence_roots=args.evidence_root,
            max_entries=args.max_entries,
            max_sources=args.max_sources,
            max_references=args.max_references,
        )
        _write_report(
            report,
            args.out,
            [
                _absolute(args.db),
                _absolute(args.data_root),
                *[_absolute(root) for root in args.evidence_root],
            ],
        )
    except AuditError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1
    except OSError:
        print(json.dumps({"status": "failed", "error": "filesystem_operation_failed"}))
        return 1
    print(json.dumps({"status": "retention_audit_written", "n03_closed": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
