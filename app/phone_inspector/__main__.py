"""Run a desktop window, or a bounded CLI acceptance scan."""

from __future__ import annotations

import argparse
import json
import signal
import threading
from pathlib import Path

from .service import configured_adb, execute, export_task


def main() -> None:
    parser = argparse.ArgumentParser(description="QQ 手机辅助巡检（独立进程）")
    parser.add_argument("--cli", action="store_true", help="命令行运行；默认打开桌面窗口")
    parser.add_argument("--adb", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--limit", type=int, default=10, help="本次最多查看次数；0 为遍历列表")
    parser.add_argument("--export", action="store_true")
    args = parser.parse_args()
    if not args.cli:
        from .gui import main as gui_main

        gui_main()
        return
    import io
    import sys

    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda _signum, _frame: stop.set())
    result = execute(
        args.adb or Path(configured_adb()),
        root=args.output_root,
        resume=args.resume,
        limit=args.limit,
        stop=stop,
    )
    if args.export:
        result["export_folder"] = str(export_task(Path(result["task_folder"])))
    # Account rows remain in the local task and exports; console is a progress summary.
    result.pop("rows", None)
    result.pop("group", None)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
