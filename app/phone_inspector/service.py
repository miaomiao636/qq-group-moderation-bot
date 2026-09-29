"""Local orchestration shared by the desktop window and acceptance-test CLI."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .device import Phone
from .engine import Scanner
from .locks import FileLock
from .storage import Store, data_root


def default_adb() -> str:
    local = data_root() / "platform-tools" / "adb.exe"
    return str(local) if local.is_file() else (shutil.which("adb") or "")


def program_stamp() -> dict[str, Any]:
    source = Path(__file__).parent
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source,
            capture_output=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        revision = (
            result.stdout.decode("ascii").strip() if result.returncode == 0 else "unversioned"
        )
    except (OSError, subprocess.TimeoutExpired):
        revision = "unversioned"
    return {
        "tool_version": "1.0",
        "execution_sha": revision,
        "argv": sys.argv,
        "code_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(source.glob("*.py"))
        },
    }


def execute(
    adb: Path,
    *,
    root: Path | None = None,
    resume: Path | None = None,
    limit: int = 10,
    stop: threading.Event | None = None,
    notify: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    root = root or data_root()
    event = stop if stop is not None else threading.Event()
    phone = Phone(adb, event)
    identity = phone.connect()
    folder = resume or root / "tasks" / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    )
    # The lock location is global even when users select a different output root.
    locks = data_root() / "locks"
    folder_key = hashlib.sha256(str(folder.resolve()).casefold().encode()).hexdigest()
    with (
        FileLock(locks / (identity.device_hash + ".lock")),
        FileLock(locks / (folder_key + ".lock")),
    ):
        store = Store(folder, create=resume is None)
        try:
            if notify:
                notify({"task_folder": str(folder)})
            stats = Scanner(phone, store, notify, program_stamp()).run(limit)
            report = {
                "task_folder": str(folder),
                "stats": stats,
                "rows": store.observations(),
                "group": store.metadata(),
            }
            if notify:
                notify(report)
            return report
        finally:
            store.close()


def export_task(folder: Path) -> Path:
    key = hashlib.sha256(str(folder.resolve()).casefold().encode()).hexdigest()
    with FileLock(data_root() / "locks" / (key + ".lock")):
        store = Store(folder)
        try:
            return store.export()
        finally:
            store.close()


def remember_adb(path: str) -> None:
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    target = root / "settings.json"
    temporary = root / ("settings-" + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps({"adb": path}, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)


def configured_adb() -> str:
    try:
        settings = json.loads((data_root() / "settings.json").read_text(encoding="utf-8"))
        value = settings.get("adb")
        return value if isinstance(value, str) else default_adb()
    except (OSError, ValueError, AttributeError):
        return default_adb()
