"""Immutable snapshots (keyless public config, or legacy encrypted recovery).

SQLite uses an online snapshot; files have a bounded capture interval, not a
cross-filesystem transaction. No completed backup or source evidence is deleted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from app.reports.backup import backup_sqlite
from app.reports.backup_crypto import copy_plain, seal_file, unseal_file
from app.reports.backup_paths import checked, private_directory, regular, verify_private


class BackupError(RuntimeError):
    """Only fixed non-sensitive error codes cross the CLI boundary."""


def _write_object(
    source: Path, target: Path, key: bytes, context: bytes, *, check: Callable[[], None]
) -> tuple[str, int]:
    return (
        seal_file(source, target, key, context, check=check)
        if key
        else copy_plain(source, target, check=check)
    )


def _read_object(
    source: Path, target: Path | None, key: bytes, context: bytes, *, check: Callable[[], None]
) -> tuple[str, int]:
    return (
        unseal_file(source, target, key, context, check=check)
        if key
        else copy_plain(source, target, check=check)
    )


@dataclass(frozen=True)
class BackupConfig:
    repo: Path
    destination: Path
    state_dir: Path
    key_file: Path
    expected_revision: str = "e1c7d4b8a902"
    max_bytes: int = 100 * 1024**3
    min_free_bytes: int = 5 * 1024**3
    timeout_seconds: float = 1800
    owner_sid: str | None = None
    mode: str = "encrypted"  # Legacy configs retain readability; new init defaults to plain.
    source_kind: str = "git"
    source_manifest_sha256: str = ""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(path: Path, data: dict[str, Any]) -> None:
    checked(path, exists=False)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".partial")
    with temporary.open("x", encoding="utf-8") as stream:
        if os.name != "nt":
            temporary.chmod(0o600)
        json.dump(data, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def _identity(config: BackupConfig, key: bytes, *, create: bool = False) -> None:
    path = checked(config.destination / "store.json", exists=False)
    expected = {
        "format": 1,
        "key_check": hmac.new(key, b"QQBotBackup key identity v1", hashlib.sha256).hexdigest(),
    }
    if config.mode == "plain":
        expected = {"format": 2, "protection": "plain", "service_credentials": "omitted"}
    if path.exists():
        if json.loads(regular(path).read_text(encoding="utf-8")) != expected:
            raise BackupError("BACKUP_KEY_MISMATCH")
    elif create and not (config.destination / "objects").exists():
        _json(path, expected)
    else:
        raise BackupError("BACKUP_IDENTITY_MISSING")


def _prepare(config: BackupConfig) -> bytes:
    roots = [
        checked(config.repo),
        checked(config.destination, exists=False),
        checked(config.state_dir),
    ]
    if any(_overlaps(a, b) for i, a in enumerate(roots) for b in roots[i + 1 :]):
        raise BackupError("OVERLAPPING_ROOTS")
    if config.max_bytes <= 0 or config.min_free_bytes < 0 or config.timeout_seconds <= 0:
        raise BackupError("INVALID_LIMITS")
    private_directory(config.state_dir, config.owner_sid)
    private_directory(config.destination, config.owner_sid)
    if config.mode == "plain":
        return b""
    if config.mode != "encrypted":
        raise BackupError("UNKNOWN_BACKUP_MODE")
    if regular(config.key_file).parent != config.state_dir.absolute():
        raise BackupError("KEY_OUTSIDE_PRIVATE_STATE")
    verify_private(config.key_file, config.owner_sid)
    key = config.key_file.read_bytes()
    if len(key) != 32:
        raise BackupError("INVALID_KEY")
    return key


@contextmanager
def backup_lock(root: Path) -> Iterator[None]:
    path = checked(root / "backup.lock", exists=False)
    with path.open("a+b") as handle:
        handle.seek(0)
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BackupError("BACKUP_ALREADY_RUNNING") from exc
        yield


def _walk(root: Path) -> Iterator[Path]:
    checked(root)
    for path in sorted(root.iterdir()):
        checked(path)
        if path.is_dir():
            yield from _walk(path)
        else:
            yield regular(path)


def _files(repo: Path, *, public_config: bool = False) -> list[tuple[str, Path]]:
    result = [] if public_config else [(".env", regular(repo / ".env"))]
    if public_config:
        rule = regular(repo / "config/ai_prompt_rules.txt")
        result.append(("config/ai_prompt_rules.txt", rule))
    folders = (
        ("data/media", "data/sample_pool")
        if public_config
        else ("config", "data/media", "data/sample_pool")
    )
    for name in folders:
        root = checked(repo / name)
        for path in _walk(root):
            rel = path.relative_to(repo).as_posix()
            # .bin can be a committed generic attachment, not just a download.
            if "_frames" in path.parts or path.suffix in {".part", ".partial", ".lock"}:
                continue
            result.append((rel, path))
    return result


def _database(path: Path, revision: str, deadline: float) -> dict[str, Any]:
    # Existing stdlib-only authority reader; backup CLI requires a source checkout.
    from scripts.image_decision_authority import read_authority

    with closing(sqlite3.connect(regular(path).as_uri() + "?mode=ro", uri=True)) as con:
        con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        if con.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise BackupError("DATABASE_INTEGRITY")
        if con.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("DATABASE_FOREIGN_KEYS")
        if con.execute("SELECT version_num FROM alembic_version").fetchall() != [(revision,)]:
            raise BackupError("DATABASE_REVISION")
        tables = (
            "cases",
            "action_intents",
            "onebot_inbox",
            "provider_group_settings",
            "allowlist_members",
        )
        counts = {
            name: con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] for name in tables
        }
    return {
        "revision": revision,
        "table_counts": counts,
        "authority_rows": len(read_authority(path)),
    }


def _media_coverage(db: Path, files: list[tuple[str, Path]]) -> dict[str, Any]:
    from scripts.retention_audit import _read_sources

    refs, counts, warnings = _read_sources(db, datetime.now(UTC), 30, 2_000_000, 4_000_000)
    if warnings:
        raise BackupError("MEDIA_REFERENCE_SCHEMA")
    available = {
        name.removeprefix("data/media/") for name, _ in files if name.startswith("data/media/")
    }
    missing = sorted(set(refs) - available)
    return {
        "referenced_files": len(refs),
        "included_references": len(set(refs) & available),
        "missing_at_capture": missing,
        "invalid_references": counts.get("invalid_file_references", 0),
        "invalid_payloads": counts.get("invalid_payload", 0),
    }


def _deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise BackupError("BACKUP_TIMEOUT")


def _fingerprint(path: Path) -> tuple[int, int, int, int]:
    s = regular(path).stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns


def _hash(path: Path, deadline: float) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with regular(path).open("rb") as stream:
        while data := stream.read(1024 * 1024):
            _deadline(deadline)
            digest.update(data)
            size += len(data)
    return digest.hexdigest(), size


def _capacity(config: BackupConfig, used: int, additional: int) -> None:
    if used + additional > config.max_bytes:
        raise BackupError("BACKUP_QUOTA")
    if shutil.disk_usage(config.destination).free < config.min_free_bytes + additional:
        raise BackupError("BACKUP_DISK_FREE")


def _restore(
    config: BackupConfig,
    snapshot_id: str,
    target: Path,
    key: bytes,
    deadline: float,
    *,
    materialize: bool = True,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[a-f0-9]{32}", snapshot_id):
        raise BackupError("INVALID_SNAPSHOT_ID")
    manifest_path = regular(
        config.destination / "snapshots" / (snapshot_id + (".enc" if key else ".json"))
    )
    if manifest_path.stat().st_size > 128 * 1024**2:
        raise BackupError("MANIFEST_TOO_LARGE")
    _free(target, manifest_path.stat().st_size, config.min_free_bytes)
    manifest_temp = target / ".backup-manifest.json"
    _read_object(
        manifest_path,
        manifest_temp,
        key,
        ("manifest:" + snapshot_id).encode(),
        check=lambda: _deadline(deadline),
    )
    manifest = json.loads(manifest_temp.read_text(encoding="utf-8"))
    if manifest.get("format") != (1 if key else 2) or manifest.get("snapshot_id") != snapshot_id:
        raise BackupError("INVALID_MANIFEST")

    def needs_file(name: str) -> bool:
        return (
            materialize
            or name == "data/moderation.db"
            or (name == "recovery-source.zip" and manifest.get("source_kind") == "bundle")
        )

    sizes = [e["size"] for e in manifest["files"] if needs_file(e["path"])]
    if any(type(size) is not int or size < 0 for size in sizes):
        raise BackupError("INVALID_MANIFEST_SIZE")
    _free(target, sum(sizes), config.min_free_bytes)
    seen: set[str] = set()
    for entry in manifest["files"]:
        _deadline(deadline)
        name, object_id = entry["path"], entry["object"]
        relative = PurePosixPath(name)
        if (
            not name
            or relative.is_absolute()
            or ".." in relative.parts
            or "\\" in name
            or ":" in name
            or name.casefold() in seen
            or str(relative) != name
            or any(part.endswith((".", " ")) for part in relative.parts)
            or any(
                part.split(".")[0].upper()
                in {
                    "CON",
                    "PRN",
                    "AUX",
                    "NUL",
                    *(f"COM{i}" for i in range(1, 10)),
                    *(f"LPT{i}" for i in range(1, 10)),
                }
                for part in relative.parts
            )
            or not (
                name
                in {
                    ".env" if key else ".env.recovery-template",
                    "data/moderation.db",
                    "recovery-source.zip",
                }
                or name.startswith(("config/", "data/media/", "data/sample_pool/"))
            )
            or name.casefold() in {".backup-manifest.json", "restore_verified.json"}
            or (not key and name.startswith("config/") and name != "config/ai_prompt_rules.txt")
            or not re.fullmatch(r"[a-f0-9]{64}", object_id)
        ):
            raise BackupError("INVALID_MANIFEST_PATH")
        seen.add(name.casefold())
        output = checked(target / name, exists=False)
        write_file = needs_file(name)
        if write_file:
            output.parent.mkdir(parents=True, exist_ok=True)
            _free(target, entry["size"], config.min_free_bytes)
        actual = _read_object(
            regular(config.destination / "objects" / (object_id + (".enc" if key else ".blob"))),
            output if write_file else None,
            key,
            ("object:" + object_id).encode(),
            check=lambda: _deadline(deadline),
        )
        if actual != (entry["sha256"], entry["size"]):
            raise BackupError("OBJECT_DIGEST_MISMATCH")
    if manifest.get("source_kind") == "bundle":
        from app.reports.backup_source import verify_archive

        if not manifest.get("source_archived") or "recovery-source.zip" not in seen:
            raise BackupError("DELIVERY_SOURCE_MISSING")
        verify_archive(
            target / "recovery-source.zip",
            manifest["source_manifest_sha256"],
            manifest["source_sha"],
        )
    database = _database(target / "data/moderation.db", config.expected_revision, deadline)
    if database != manifest["database"]:
        raise BackupError("DATABASE_SUMMARY_MISMATCH")
    receipt = {
        "status": "verified",
        "snapshot_id": snapshot_id,
        "file_count": len(seen),
        "verified_at": _now(),
    }
    _json(target / "RESTORE_VERIFIED.json", receipt)
    return receipt


def _free(path: Path, amount: int, reserve: int) -> None:
    if shutil.disk_usage(path).free < reserve + amount:
        raise BackupError("STAGING_DISK_FREE")


def restore_snapshot(config: BackupConfig, snapshot_id: str, restore_to: Path) -> dict[str, Any]:
    key = _prepare(config)
    target = checked(restore_to, exists=False)
    if target.exists() or any(
        _overlaps(target, checked(p)) for p in (config.repo, config.destination, config.state_dir)
    ):
        raise BackupError("RESTORE_TARGET_NOT_FRESH")
    private_directory(target, config.owner_sid)
    with backup_lock(config.destination):
        _identity(config, key)
        if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[a-f0-9]{32}", snapshot_id):
            raise BackupError("INVALID_SNAPSHOT_ID")
        receipt = json.loads(
            regular(config.destination / "attempts" / (snapshot_id + ".json")).read_text(
                encoding="utf-8"
            )
        )
        if receipt.get("status") != "success" or receipt.get("snapshot_id") != snapshot_id:
            raise BackupError("SNAPSHOT_NOT_VERIFIED")
        if not key or "manifest_sha256" in receipt:
            manifest_path = regular(
                config.destination / "snapshots" / (snapshot_id + (".enc" if key else ".json"))
            )
            actual = _hash(manifest_path, time.monotonic() + config.timeout_seconds)
            if actual != (receipt.get("manifest_sha256"), receipt.get("manifest_size")):
                raise BackupError("MANIFEST_RECEIPT_MISMATCH")
        try:
            return _restore(
                config, snapshot_id, target, key, time.monotonic() + config.timeout_seconds
            )
        except Exception as exc:
            _json(
                target / "RESTORE_FAILED.json",
                {"status": "failed", "error": "RESTORE_FAILED", "snapshot_id": snapshot_id},
            )
            raise BackupError("RESTORE_FAILED") from exc


def run_backup(config: BackupConfig, *, source_sha: str) -> dict[str, Any]:
    key = _prepare(config)
    if not re.fullmatch(r"[a-f0-9]{40}", source_sha):
        raise BackupError("INVALID_SOURCE_SHA")
    deadline = time.monotonic() + config.timeout_seconds
    snapshot_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex
    receipt: dict[str, Any] = {
        "snapshot_id": snapshot_id,
        "source_sha": source_sha,
        "started_at": _now(),
        "status": "failed",
    }
    with backup_lock(config.destination):
        _identity(config, key, create=True)
        for folder in ("objects", "snapshots", "attempts"):
            checked(config.destination / folder, exists=False).mkdir(exist_ok=True)
        try:
            used = sum(p.stat().st_size for p in _walk(config.destination))
            _capacity(config, used, 0)
            with tempfile.TemporaryDirectory(
                prefix="capture-", dir=config.state_dir
            ) as staging_name:
                staging = Path(staging_name)
                db = regular(config.repo / "data/moderation.db")
                _free(staging, db.stat().st_size * 2 + 128 * 1024**2, config.min_free_bytes)
                snapshot = backup_sqlite(
                    f"sqlite:///{db}",
                    timeout_seconds=config.timeout_seconds,
                    destination_dir=staging,
                )
                database = _database(snapshot, config.expected_revision, deadline)
                source_files = _files(config.repo.absolute(), public_config=not key)
                if not key:
                    from app.reports.backup_public_config import write_recovery_template

                    env_source = regular(config.repo / ".env")
                    env_before = _fingerprint(env_source)
                    template = staging / "public-env"
                    write_recovery_template(env_source, template)
                    if env_before != _fingerprint(env_source):
                        raise BackupError("SOURCE_CHANGED")
                    source_files.append((".env.recovery-template", template))
                coverage = _media_coverage(snapshot, source_files)
                source_files.insert(0, ("data/moderation.db", snapshot))
                source_archived = (config.repo / ".git").exists()
                if config.source_kind == "bundle":
                    from app.reports.backup_source import archive_bundle

                    archive = staging / "source.zip"
                    archive_bundle(config.repo, config.source_manifest_sha256, source_sha, archive)
                    source_files.append(("recovery-source.zip", archive))
                    source_archived = True
                elif config.source_kind != "git":
                    raise BackupError("UNKNOWN_SOURCE_KIND")
                elif source_archived:
                    archive = staging / "source.zip"
                    with archive.open("xb") as out:
                        subprocess.run(
                            [
                                "git",
                                "-c",
                                f"safe.directory={config.repo.as_posix()}",
                                "archive",
                                "--format=zip",
                                source_sha,
                            ],
                            cwd=config.repo,
                            stdout=out,
                            stderr=subprocess.PIPE,
                            check=True,
                            timeout=60,
                        )
                    if not key:
                        with zipfile.ZipFile(archive) as bundle:
                            for name in bundle.namelist():
                                leaf = PurePosixPath(name).name.casefold()
                                if leaf == ".env" or (
                                    leaf.startswith(".env.") and leaf != ".env.example"
                                ):
                                    raise BackupError("ARCHIVE_CONTAINS_CREDENTIAL_FILE")
                                if (
                                    name.casefold().startswith("config/")
                                    and not name.endswith("/")
                                    and name.casefold() != "config/ai_prompt_rules.txt"
                                ):
                                    raise BackupError("ARCHIVE_UNREVIEWED_CONFIG")
                    source_files.append(("recovery-source.zip", archive))
                entries = []
                reused = 0
                for name, path in source_files:
                    _deadline(deadline)
                    before = _fingerprint(path)
                    digest, size = _hash(path, deadline)
                    object_id = (
                        digest
                        if not key
                        else hmac.new(
                            key, b"object-id:" + bytes.fromhex(digest), hashlib.sha256
                        ).hexdigest()
                    )
                    final = checked(
                        config.destination / "objects" / (object_id + (".enc" if key else ".blob")),
                        exists=False,
                    )
                    if final.exists():
                        actual = _read_object(
                            regular(final),
                            None,
                            key,
                            ("object:" + object_id).encode(),
                            check=lambda: _deadline(deadline),
                        )
                        reused += 1
                    else:
                        _capacity(config, used, size + 32)
                        partial = final.with_name(final.name + "." + uuid.uuid4().hex + ".partial")
                        actual = _write_object(
                            path,
                            partial,
                            key,
                            ("object:" + object_id).encode(),
                            check=lambda: _deadline(deadline),
                        )
                        if (
                            actual != (digest, size)
                            or _read_object(
                                partial,
                                None,
                                key,
                                ("object:" + object_id).encode(),
                                check=lambda: _deadline(deadline),
                            )
                            != actual
                        ):
                            raise BackupError("SOURCE_CHANGED")
                        partial.rename(final)
                        used += size + 32
                    if actual != (digest, size) or before != _fingerprint(path):
                        raise BackupError("SOURCE_CHANGED")
                    entries.append(
                        {
                            "path": name,
                            "object": object_id,
                            "sha256": digest,
                            "size": size,
                            "captured_at": _now(),
                        }
                    )
                manifest = {
                    "format": 1 if key else 2,
                    "protection": config.mode,
                    "credentials_omitted": not bool(key),
                    "snapshot_id": snapshot_id,
                    "source_sha": source_sha,
                    "source_archived": source_archived,
                    "source_kind": config.source_kind,
                    "source_manifest_sha256": config.source_manifest_sha256,
                    "capture_start": receipt["started_at"],
                    "capture_end": _now(),
                    "filesystem_atomic": False,
                    "files": entries,
                    "database": database,
                    "media_coverage": coverage,
                    "scope": [
                        "online SQLite",
                        ".env" if key else ".env.recovery-template (public allowlist)",
                        "config" if key else "config/ai_prompt_rules.txt",
                        "data/media",
                        "data/sample_pool",
                        "source archive",
                    ],
                    "excluded": [
                        "prior backups",
                        "logs/historical drill outputs",
                        "NapCat/QQ login session",
                        "derived JSON authority export",
                        "temporary downloads and frames",
                    ],
                    "retention": "No automatic deletion; approved disposal list required",
                }
                manifest_plain = staging / "manifest.json"
                _json(manifest_plain, manifest)
                _capacity(config, used, manifest_plain.stat().st_size + 32)
                sealed = (
                    config.destination / "snapshots" / (snapshot_id + (".enc" if key else ".json"))
                )
                partial_manifest = sealed.with_suffix(".partial")
                manifest_digest = _hash(manifest_plain, deadline)
                written_manifest = _write_object(
                    manifest_plain,
                    partial_manifest,
                    key,
                    ("manifest:" + snapshot_id).encode(),
                    check=lambda: _deadline(deadline),
                )
                if written_manifest != manifest_digest:
                    raise BackupError("MANIFEST_WRITE_MISMATCH")
                partial_manifest.rename(sealed)
                manifest_bytes = _hash(sealed, deadline)
                if not key and manifest_bytes != manifest_digest:
                    raise BackupError("MANIFEST_WRITE_MISMATCH")
                restore_to = staging / "verify"
                restore_to.mkdir(mode=0o700)
                _restore(config, snapshot_id, restore_to, key, deadline, materialize=False)
                if _hash(sealed, deadline) != manifest_bytes:
                    raise BackupError("MANIFEST_CHANGED")
                receipt.update(
                    status="success",
                    completed_at=_now(),
                    file_count=len(entries),
                    reused_objects=reused,
                    verified=True,
                    protection=config.mode,
                    credentials_omitted=not bool(key),
                    manifest_sha256=manifest_bytes[0],
                    manifest_size=manifest_bytes[1],
                    missing_media_count=len(coverage["missing_at_capture"]),
                    invalid_media_references=coverage["invalid_references"],
                    invalid_payloads=coverage["invalid_payloads"],
                )
                _json(config.destination / "attempts" / (snapshot_id + ".json"), receipt)
                _json(config.destination / "last_success.json", receipt)
                return receipt
        except Exception as exc:
            receipt.update(
                status="failed",
                completed_at=_now(),
                error=str(exc) if isinstance(exc, BackupError) else "BACKUP_FAILED",
            )
            _json(config.destination / "attempts" / (snapshot_id + ".json"), receipt)
            raise BackupError(receipt["error"]) from exc
