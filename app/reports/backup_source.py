"""Validate pinned delivery contents without pretending a ZIP is a Git checkout."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.reports.backup_paths import checked, regular

MANIFEST = "DELIVERY-MANIFEST.json"
MAX_FILE = 64 * 1024**2
MAX_TOTAL = 256 * 1024**2
PUBLIC_FILES = frozenset(
    {
        "README.md",
        "DELIVERY.md",
        "LICENSE",
        "pyproject.toml",
        "uv.lock",
        ".env.example",
        "alembic.ini",
        "config/ai_prompt_rules.txt",
        "docs/delivery/group-management.md",
        "docs/delivery/space-inspector.md",
        "docs/delivery/acceptance-maintenance.md",
        "scripts/install-services-nssm.ps1",
        "scripts/install-space-inspector.ps1",
        "scripts/napcat-autostart-template.bat",
        "scripts/register_backup_task.ps1",
        "scripts/image_decision_authority.py",
        "scripts/image_allowlist_seed.py",
        "scripts/retention_audit.py",
    }
)
REQUIRED = frozenset(
    {
        "DELIVERY.md",
        "LICENSE",
        "pyproject.toml",
        "uv.lock",
        ".env.example",
        "alembic.ini",
        "config/ai_prompt_rules.txt",
    }
)


@dataclass(frozen=True)
class SourceManifest:
    sha: str
    raw: bytes
    files: dict[str, tuple[str, int]]


def safe_name(name: object) -> bool:
    if not isinstance(name, str) or not name or len(name) > 240:
        return False
    path = PurePosixPath(name)
    return bool(
        not path.is_absolute()
        and str(path) == name
        and not any(c in name for c in '\\:<>"|?*')
        and not any(ord(c) < 32 for c in name)
        and all(
            p not in {".", ".."}
            and not p.endswith((".", " "))
            and p.split(".")[0].upper()
            not in {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10)),
            }
            for p in path.parts
        )
    )


def source_name(name: str) -> bool:
    return name in PUBLIC_FILES or (
        name.startswith(("app/", "alembic/"))
        and PurePosixPath(name).suffix in {".py", ".mako"}
        and not any(p.startswith(".") for p in PurePosixPath(name).parts)
    )


def parse_manifest(raw: bytes, pinned: str) -> SourceManifest:
    if (
        not re.fullmatch(r"[a-f0-9]{64}", pinned)
        or len(raw) > 2 * 1024**2
        or hashlib.sha256(raw).hexdigest() != pinned
    ):
        raise ValueError("DELIVERY_MANIFEST_HASH")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("format") != 1:
        raise ValueError("DELIVERY_MANIFEST_FORMAT")
    sha = value.get("source_sha")
    items = value.get("files")
    if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("DELIVERY_SOURCE_SHA")
    if not isinstance(items, list) or not 1 <= len(items) <= 5000:
        raise ValueError("DELIVERY_FILE_LIST")
    files: dict[str, tuple[str, int]] = {}
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("DELIVERY_FILE_ENTRY")
        name, digest, size = item.get("path"), item.get("sha256"), item.get("bytes")
        if (
            not isinstance(name, str)
            or not safe_name(name)
            or not source_name(name)
            or name.casefold() in seen
            or not isinstance(digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or type(size) is not int
            or not 0 <= size <= MAX_FILE
        ):
            raise ValueError("DELIVERY_FILE_ENTRY")
        seen.add(name.casefold())
        files[name] = digest, size
    if (
        not REQUIRED.issubset(files)
        or not any(n.startswith("app/") for n in files)
        or not any(n.startswith("alembic/") for n in files)
        or sum(size for _, size in files.values()) > MAX_TOTAL
    ):
        raise ValueError("DELIVERY_REQUIRED_FILES")
    return SourceManifest(sha, raw, files)


def _contents(path: Path, expected: tuple[str, int]) -> bytes:
    path = regular(path)
    if path.stat().st_size != expected[1]:
        raise ValueError("DELIVERY_FILE_SIZE")
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE + 1)
    if (hashlib.sha256(data).hexdigest(), len(data)) != expected:
        raise ValueError("DELIVERY_FILE_HASH")
    return data


def validate_bundle(root: Path, pinned: str) -> SourceManifest:
    root = checked(root)
    if (root / ".git").exists() or (root / ".git").is_symlink():
        raise ValueError("BUNDLE_HAS_GIT_METADATA")
    manifest_path = regular(root / MANIFEST)
    with manifest_path.open("rb") as stream:
        manifest = parse_manifest(stream.read(2 * 1024**2 + 1), pinned)
    for name, expected in manifest.files.items():
        _contents(root / name, expected)
    for path in root.iterdir():
        if path.suffix.lower() in {".py", ".pyc", ".pyd", ".pth", ".dll"}:
            raise ValueError("DELIVERY_EXTRA_FILES")
    # Extra runtime modules/resources must not execute without being part of the pin.
    for directory in ("app", "alembic", "scripts"):
        base = root / directory
        if not base.exists():
            continue
        pending = [checked(base)]
        count = 0
        while pending:
            for path in pending.pop().iterdir():
                count += 1
                if count > 10000:
                    raise ValueError("DELIVERY_EXTRA_FILES")
                checked(path)
                if path.is_dir():
                    if path.name != "__pycache__":
                        pending.append(path)
                elif path.relative_to(root).as_posix() not in manifest.files:
                    raise ValueError("DELIVERY_EXTRA_FILES")
    return manifest


def archive_bundle(root: Path, pinned: str, sha: str, target: Path) -> None:
    manifest = validate_bundle(root, pinned)
    if manifest.sha != sha:
        raise ValueError("DELIVERY_SOURCE_SHA")
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED) as archive:

        def write(name: str, content: bytes) -> None:
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, content)

        write(MANIFEST, manifest.raw)
        for name, expected in sorted(manifest.files.items()):
            write(name, _contents(root / name, expected))
    if validate_bundle(root, pinned).raw != manifest.raw:
        raise ValueError("DELIVERY_CHANGED")
    verify_archive(target, pinned, sha)


def verify_archive(path: Path, pinned: str, sha: str) -> None:
    with zipfile.ZipFile(regular(path)) as archive:
        items = archive.infolist()
        names = [item.filename for item in items]
        if len(items) > 5001 or len(set(n.casefold() for n in names)) != len(names):
            raise ValueError("DELIVERY_ARCHIVE_DUPLICATE")
        if MANIFEST not in names:
            raise ValueError("DELIVERY_MANIFEST_MISSING")
        if archive.getinfo(MANIFEST).file_size > 2 * 1024**2:
            raise ValueError("DELIVERY_MANIFEST_SIZE")
        manifest = parse_manifest(archive.read(MANIFEST), pinned)
        if manifest.sha != sha or set(names) != {*manifest.files, MANIFEST}:
            raise ValueError("DELIVERY_ARCHIVE_CONTENTS")
        for item in items:
            if (
                not safe_name(item.filename)
                or item.is_dir()
                or stat.S_IFMT(item.external_attr >> 16) not in {0, stat.S_IFREG}
            ):
                raise ValueError("DELIVERY_ARCHIVE_PATH")
            if item.filename == MANIFEST:
                continue
            expected = manifest.files[item.filename]
            if item.file_size != expected[1]:
                raise ValueError("DELIVERY_FILE_SIZE")
            data = archive.read(item)
            if (hashlib.sha256(data).hexdigest(), len(data)) != expected:
                raise ValueError("DELIVERY_FILE_HASH")
