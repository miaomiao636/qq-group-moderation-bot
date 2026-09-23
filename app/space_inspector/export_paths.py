"""Human-readable output locations, separate from stable internal task identities."""

from __future__ import annotations

import sys
import unicodedata
from pathlib import Path


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
