"""One owner thread for browser and task storage, independent of Tk availability."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from .contracts import (
    BrowserConfirmationRequired,
    InspectionError,
    MemberPageUnrecognized,
    PlatformAccessBlocked,
)
from .options import ScanOptions
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

    def failure(
        message: str, operation: str, *, platform_blocked: bool = False, defer_qq: str = ""
    ) -> None:
        payload: dict[str, object] = {"message": message, "operation": operation}
        if service is not None and operation == "groups":
            payload.update(export_location())
        if platform_blocked:
            payload["requires_browser_confirmation"] = True
        if defer_qq:
            payload["defer_qq"] = defer_qq
        if service is not None and operation in {"create", "resume", "scan"}:
            with suppress(Exception):
                payload.update(snapshot())
        emit("error", payload)

    def export_location() -> dict[str, object]:
        assert service is not None
        return {
            "export_root": str(service.export_root) if not service.export_settings_error else "",
            "export_settings_error": service.export_settings_error,
        }

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
                emit(
                    "ready",
                    {
                        "groups": service.groups(),
                        "source_id": service.source_id,
                        **export_location(),
                    },
                )
            elif kind == "set_export_root":
                if not isinstance(payload, Path):
                    raise InspectionError("请选择导出文件夹。")
                service.set_export_root(payload)
                emit("export_location_saved", export_location())
            elif kind == "history":
                emit("history", {"tasks": service.history()})
            elif kind == "browser":
                service.open_browser()
                emit("browser_opened", {})
            elif kind == "viewer":
                emit("viewer", {"viewer": service.viewer()})
            elif kind == "create":
                options = None
                if isinstance(payload, dict):
                    options = payload.get("options")
                    payload = payload.get("groups")
                    if not isinstance(options, ScanOptions):
                        raise InspectionError("巡检设置格式无效。")
                    options.validate()
                if not isinstance(payload, list) or not all(isinstance(x, str) for x in payload):
                    raise InspectionError("请选择需要检查的群。")
                service.create(payload)
                emit("created", snapshot())
                if not stop.is_set():
                    if options is None:
                        service.scan(stop, progress)
                    else:
                        service.scan(stop, progress, options)
                emit("scanned", snapshot())
            elif kind == "resume":
                if not isinstance(payload, Path):
                    raise InspectionError("请选择已保存的巡检任务目录。")
                service.resume(payload)
                emit("loaded", snapshot())
            elif kind == "scan":
                if not stop.is_set():
                    if payload is None:
                        service.scan(stop, progress)
                    elif isinstance(payload, ScanOptions):
                        payload.validate()
                        service.scan(stop, progress, payload)
                    else:
                        raise InspectionError("巡检设置格式无效。")
                emit("scanned", snapshot())
            elif kind == "export":
                emit("exported", {"path": service.export()})
            else:
                raise InspectionError("暂不支持这项操作。")
        except PlatformAccessBlocked as exc:
            failure(str(exc), kind, platform_blocked=True)
        except BrowserConfirmationRequired as exc:
            failure(str(exc), kind, platform_blocked=True)
        except MemberPageUnrecognized as exc:
            failure(str(exc), kind, defer_qq=exc.qq)
        except InspectionError as exc:
            failure(str(exc), kind)
        except Exception:
            failure("操作未完成。请检查本机连接后重试；已保存的任务仍可载入。", kind)
