"""Human-readable output locations, separate from stable internal task identities."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unicodedata
from pathlib import Path

from .contracts import InspectionError


def _checked_path(path: Path, data_root: Path) -> Path:
    if not path.is_absolute() or "\x00" in str(path):
        raise ValueError("invalid export path")
    path = path.resolve()
    if path.is_relative_to(data_root.resolve()):
        raise ValueError("internal task/browser data directory")
    return path


def load_export_root(data_root: Path) -> tuple[Path, bool]:
    """A missing setting uses the default; unreadable settings never silently fall back."""
    try:
        with (data_root / "export-settings.json").open(encoding="utf-8") as stream:
            settings = json.load(stream)
    except FileNotFoundError:
        return default_export_root(), False
    except (OSError, ValueError):
        raise InspectionError("无法读取导出位置设置，请点击“选择导出位置”重新设置。") from None
    try:
        if not isinstance(settings, dict) or not isinstance(settings.get("export_root"), str):
            raise ValueError("invalid export settings")
        return _checked_path(Path(settings["export_root"]), data_root), True
    except (OSError, ValueError, RuntimeError):
        raise InspectionError("导出位置设置无效，请点击“选择导出位置”重新设置。") from None


def save_export_root(data_root: Path, chosen: Path) -> Path:
    """Verify the chosen folder and atomically persist before changing the live value."""
    temporary: Path | None = None
    try:
        path = _checked_path(chosen, data_root)
        if not path.is_dir():
            raise ValueError("not an existing directory")
        with tempfile.TemporaryFile(dir=path) as probe:
            probe.write(b"export directory check")
            probe.flush()
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=data_root, suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump({"export_root": str(path)}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, data_root / "export-settings.json")
        return path
    except (OSError, ValueError, RuntimeError):
        raise InspectionError(
            "导出位置未更改。请选择可写入的现有文件夹，避开巡检内部数据目录；"
            "并检查磁盘连接及设置文件写入权限。"
        ) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def default_export_root() -> Path:
    # Ask Windows for the actual (possibly redirected) desktop, not a guessed path.
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        get_folder = ctypes.windll.shell32.SHGetFolderPathW
        get_folder.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
        ]
        get_folder.restype = ctypes.c_long
        buffer = ctypes.create_unicode_buffer(260)
        if get_folder(None, 0x0010, None, 0, buffer) == 0 and buffer.value:
            return Path(buffer.value) / "QQ空间巡检结果"
    return Path.home() / "QQ空间巡检结果"


def group_stem(name: str, group_id: str) -> str:
    clean = "".join(
        "_" if char in '<>:"/\\|?*' or unicodedata.category(char).startswith("C") else char
        for char in name
    )
    # Bound UTF-16 length too, including emoji, leaving space for the ID and suffixes.
    clean = clean.encode("utf-16-le")[:48].decode("utf-16-le", errors="ignore").strip(" .")
    # A prefix also covers device names with extensions, e.g. NUL.txt.
    if clean.split(".", 1)[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
        *(f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in "¹²³"),
    }:
        clean = "群_" + clean
    return f"{clean or '未命名群'}_群{group_id}"
