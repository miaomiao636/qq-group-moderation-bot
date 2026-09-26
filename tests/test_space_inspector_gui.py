"""Only synthetic worker services run here; no Tk window or browser is started."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from pathlib import Path
from typing import cast

from app.space_inspector.contracts import Group, InspectionError, PlatformAccessBlocked
from app.space_inspector.service import Service
from app.space_inspector.worker import Command, Event, run_worker


class FakeService:
    source_id = "12345678"
    export_root = Path("synthetic-exports")
    export_settings_error = ""
    current_folder: Path | None = None

    def __init__(self, calls: list[tuple[str, int]]) -> None:
        self.calls = calls
        self.record("init")

    def record(self, action: str) -> None:
        self.calls.append((action, threading.get_ident()))

    def groups(self) -> list[Group]:
        self.record("groups")
        return [Group("23456789", "合成群", 2)]

    def open_browser(self) -> None:
        self.record("browser")

    def history(self) -> list[dict[str, object]]:
        self.record("history")
        return [{"folder": Path("saved-task"), "checked": 3, "total": 10}]

    def viewer(self) -> str:
        self.record("viewer")
        return "34567890"

    def create(self, group_ids: list[str]) -> None:
        assert group_ids == ["23456789"]
        self.record("create")
        self.current_folder = Path("synthetic-task")

    def resume(self, folder: Path) -> None:
        self.record("resume")
        self.current_folder = folder

    def scan(self, stop: threading.Event, on_progress: Callable[[dict[str, object]], None]) -> None:
        self.record("scan")
        assert not stop.is_set()
        on_progress({"total": 2, "checked": 1, "pending": 1})

    def summary(self) -> dict[str, object]:
        self.record("summary")
        return {"total": 2, "checked": 1, "restricted": 1, "pending": 1}

    def rows(self) -> list[dict[str, object]]:
        self.record("rows")
        return [{"qq": "45678901", "status": "RESTRICTION_OBSERVED", "reason": "synthetic"}]

    def export(self) -> Path:
        self.record("export")
        return Path("synthetic-output.csv")

    def close(self) -> None:
        self.record("close")


def drain(events: queue.Queue[Event]) -> list[Event]:
    result = []
    while not events.empty():
        result.append(events.get_nowait())
    return result


def test_all_service_calls_belong_to_one_worker_thread() -> None:
    calls: list[tuple[str, int]] = []
    commands: queue.Queue[Command] = queue.Queue()
    events: queue.Queue[Event] = queue.Queue(maxsize=100)
    commands.put(("groups", None))
    commands.put(("browser", None))
    commands.put(("viewer", None))
    commands.put(("create", ["23456789"]))
    commands.put(("export", None))
    commands.put(("resume", Path("saved-task")))
    commands.put(("scan", None))
    commands.put(("close", None))
    worker = threading.Thread(
        target=run_worker,
        args=(commands, events, threading.Event(), lambda: cast(Service, FakeService(calls))),
    )
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert {thread for _, thread in calls} == {worker.ident}
    assert worker.ident != threading.get_ident()
    assert calls[0][0] == "init"
    assert calls[-1][0] == "close"
    assert [action for action, _ in calls if action == "scan"] == ["scan", "scan"]
    emitted = drain(events)
    assert [kind for kind, _ in emitted] == [
        "ready",
        "browser_opened",
        "viewer",
        "created",
        "progress",
        "scanned",
        "exported",
        "loaded",
        "progress",
        "scanned",
        "closed",
    ]
    assert emitted[0][1]["source_id"] == "12345678"
    assert emitted[7][1]["folder"] == Path("saved-task")


def test_close_waits_for_scan_to_observe_stop_and_then_closes_service() -> None:
    calls: list[tuple[str, int]] = []
    entered_scan = threading.Event()
    stop = threading.Event()

    class Pausable(FakeService):
        def scan(
            self, signal: threading.Event, on_progress: Callable[[dict[str, object]], None]
        ) -> None:
            self.record("scan_enter")
            entered_scan.set()
            assert signal.wait(2)
            self.record("scan_saved")

    commands: queue.Queue[Command] = queue.Queue()
    events: queue.Queue[Event] = queue.Queue(maxsize=100)
    commands.put(("create", ["23456789"]))
    worker = threading.Thread(
        target=run_worker,
        args=(commands, events, stop, lambda: cast(Service, Pausable(calls))),
    )
    worker.start()
    assert entered_scan.wait(2)
    stop.set()
    commands.put(("close", None))
    worker.join(timeout=2)
    assert not worker.is_alive()
    order = [action for action, _ in calls]
    assert order.index("scan_saved") < order.index("close")
    assert [kind for kind, _ in drain(events)][-2:] == ["scanned", "closed"]


def test_failed_scan_reports_saved_snapshot_and_remains_exportable() -> None:
    calls: list[tuple[str, int]] = []

    class Blocked(FakeService):
        def scan(
            self, stop: threading.Event, on_progress: Callable[[dict[str, object]], None]
        ) -> None:
            self.record("blocked_scan")
            raise InspectionError("巡检已暂停，请确认登录。")

    commands: queue.Queue[Command] = queue.Queue()
    events: queue.Queue[Event] = queue.Queue(maxsize=100)
    commands.put(("create", ["23456789"]))
    commands.put(("export", None))
    commands.put(("close", None))
    run_worker(commands, events, threading.Event(), lambda: cast(Service, Blocked(calls)))
    emitted = drain(events)
    error = next(payload for kind, payload in emitted if kind == "error")
    assert error["folder"] == Path("synthetic-task")
    assert error["summary"] == {"total": 2, "checked": 1, "restricted": 1, "pending": 1}
    assert error["message"] == "巡检已暂停，请确认登录。"
    assert any(kind == "exported" for kind, _ in emitted)
    assert emitted[-1][0] == "closed"


def test_unexpected_service_error_is_redacted_and_close_still_runs() -> None:
    calls: list[tuple[str, int]] = []

    class Broken(FakeService):
        def groups(self) -> list[Group]:
            raise RuntimeError("synthetic-secret https://example.invalid/private")

    commands: queue.Queue[Command] = queue.Queue()
    events: queue.Queue[Event] = queue.Queue(maxsize=100)
    commands.put(("groups", None))
    commands.put(("close", None))
    run_worker(commands, events, threading.Event(), lambda: cast(Service, Broken(calls)))
    emitted = drain(events)
    assert emitted[0][0] == "error"
    assert "synthetic-secret" not in str(emitted)
    assert "https://" not in str(emitted)
    assert calls[-1][0] == "close"


def test_pause_during_create_does_not_start_scanning() -> None:
    calls: list[tuple[str, int]] = []
    stop = threading.Event()
    stop.set()
    commands: queue.Queue[Command] = queue.Queue()
    events: queue.Queue[Event] = queue.Queue(maxsize=100)
    commands.put(("create", ["23456789"]))
    commands.put(("close", None))
    run_worker(commands, events, stop, lambda: cast(Service, FakeService(calls)))
    assert "scan" not in [action for action, _ in calls]
    assert any(kind == "scanned" for kind, _ in drain(events))


def test_close_failure_preserves_worker_until_a_successful_retry() -> None:
    calls: list[tuple[str, int]] = []

    class CloseRetry(FakeService):
        close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            self.record("close")
            if self.close_calls == 1:
                raise InspectionError("专用浏览器未关闭，锁仍保留。")

    commands: queue.Queue[Command] = queue.Queue()
    events: queue.Queue[Event] = queue.Queue(maxsize=100)
    commands.put(("groups", None))
    commands.put(("close", None))
    commands.put(("close", None))
    run_worker(commands, events, threading.Event(), lambda: cast(Service, CloseRetry(calls)))
    emitted = drain(events)
    assert [kind for kind, _ in emitted] == ["ready", "closing_failed", "closed"]
    assert emitted[1][1]["message"] == "请手动关闭专用浏览器后重试退出。"
    assert [action for action, _ in calls].count("close") == 2
    assert [action for action, _ in calls].count("init") == 1


def test_history_does_not_open_browser_replace_task_or_start_scan() -> None:
    calls: list[tuple[str, int]] = []
    commands: queue.Queue[Command] = queue.Queue()
    events: queue.Queue[Event] = queue.Queue(maxsize=100)
    fake = FakeService(calls)
    fake.current_folder = Path("current-task")
    commands.put(("history", None))
    commands.put(("close", None))
    worker = threading.Thread(
        target=run_worker,
        args=(commands, events, threading.Event(), lambda: cast(Service, fake)),
    )
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert fake.current_folder == Path("current-task")
    assert calls == [
        ("init", threading.get_ident()),
        ("history", worker.ident),
        ("close", worker.ident),
    ]
    assert drain(events) == [
        ("history", {"tasks": [{"folder": Path("saved-task"), "checked": 3, "total": 10}]}),
        ("closed", {}),
    ]


def test_platform_block_requires_browser_confirmation_but_keeps_task_exportable():
    class Blocked(FakeService):
        def scan(self, stop, on_progress):
            raise PlatformAccessBlocked("QQ 空间访问被腾讯安全防护拦截，请停止重试")

    commands, events = queue.Queue(), queue.Queue(maxsize=100)
    commands.put(("create", ["23456789"]))
    commands.put(("export", None))
    commands.put(("close", None))
    run_worker(commands, events, threading.Event(), lambda: cast(Service, Blocked([])))
    emitted = drain(events)
    error = next(payload for kind, payload in emitted if kind == "error")
    assert error["requires_browser_confirmation"] is True
    assert error["folder"] == Path("synthetic-task")
    assert any(kind == "exported" for kind, _ in emitted)
