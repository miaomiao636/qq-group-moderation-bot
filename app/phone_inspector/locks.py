"""OS-released locks: a crash cannot leave a stale logical device lock."""

from __future__ import annotations

import sys
from pathlib import Path
from types import TracebackType
from typing import BinaryIO

from .pages import InspectionError


class FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.file: BinaryIO | None = None

    def __enter__(self) -> FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise InspectionError("该手机或任务正被另一个巡检窗口使用。") from exc
        self.file = handle
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.file:
            self.file.close()
            self.file = None
