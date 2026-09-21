"""One owner thread for browser and task storage, independent of Tk availability."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from .contracts import InspectionError, PlatformAccessBlocked
from .service import Service

Command = tuple[str, object]
Event = tuple[str, dict[str, object]]


def run_worker(
    commands: queue.Queue[Command],
    events: queue.Queue[Event],
    stop: threading.Event,
    service_factory: Callable[[], Service] = Service,
) -> None:
    """The Tk thread receives plain values, never a browser/service object."""
    service: Service | None = None

    def emit(kind: str, payload: dict[str, object]) -> None:
        events.put((kind, payload))

    def progress(payload: dict[str, object]) -> None:
        # Progress is replaceable; completion/error messages are never discarded.
        with suppress(queue.Full):
            events.put_nowait(("progress", dict(payload)))

    def snapshot() -> dict[str, object]:
        assert service is not None
        return {
            "folder": service.current_folder,
            "summary": service.summary(),
            "rows": service.rows(),
        }

    def failure(message: str, operation: str, *, platform_blocked: bool = False) -> None:
        payload: dict[str, object] = {"message": message, "operation": operation}
        if platform_blocked:
            payload["requires_browser_confirmation"] = True
        if service is not None and operation in {"create", "resume", "scan"}:
            with suppress(Exception):
                payload.update(snapshot())
        emit("error", payload)

    while True:
        kind, payload = commands.get()
        if kind == "close":
            try:
                if service is not None:
                    service.close()
            except Exception:
                emit("closing_failed", {"message": "请手动关闭专用浏览器后重试退出。"})
                continue
            emit("closed", {})
            return
        try:
            if service is None:
                service = service_factory()
            if kind == "groups":
                emit("ready", {"groups": service.groups(), "source_id": service.source_id})
            elif kind == "history":
                emit("history", {"tasks": service.history()})
            elif kind == "browser":
                service.open_browser()
                emit("browser_opened", {})
            elif kind == "viewer":
                emit("viewer", {"viewer": service.viewer()})
            elif kind == "create":
                if not isinstance(payload, list) or not all(isinstance(x, str) for x in payload):
                    raise InspectionError("请选择需要检查的群。")
                service.create(payload)
                emit("created", snapshot())
                if not stop.is_set():
                    service.scan(stop, progress)
                emit("scanned", snapshot())
            elif kind == "resume":
                if not isinstance(payload, Path):
                    raise InspectionError("请选择已保存的巡检任务目录。")
                service.resume(payload)
                emit("loaded", snapshot())
            elif kind == "scan":
                if not stop.is_set():
                    service.scan(stop, progress)
                emit("scanned", snapshot())
            elif kind == "export":
                emit("exported", {"path": service.export()})
            else:
                raise InspectionError("暂不支持这项操作。")
        except PlatformAccessBlocked as exc:
            failure(str(exc), kind, platform_blocked=True)
        except InspectionError as exc:
            failure(str(exc), kind)
        except Exception:
            failure("操作未完成。请检查本机连接后重试；已保存的任务仍可载入。", kind)
