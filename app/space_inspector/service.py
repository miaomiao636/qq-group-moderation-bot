"""Desktop orchestration. It has no moderation database or action adapter access."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from app.phone_inspector.locks import FileLock
from app.phone_inspector.pages import InspectionError as LockError

from .browser import Browser
from .contracts import (
    BLOCKED,
    REASONS,
    Group,
    InspectionError,
    Observation,
    PlatformAccessBlocked,
    numeric_id,
)


class ScanStore(Protocol):
    def pending(self) -> list[str]: ...
    def save(self, observation: Observation) -> None: ...
    def summary(self) -> Mapping[str, object]: ...


class PageReader(Protocol):
    def inspect(self, qq: str, viewer_qq: str) -> Observation: ...


def run_scan(
    store: ScanStore,
    browser: PageReader,
    viewer_qq: str,
    stop: threading.Event,
    on_progress: Callable[[dict[str, object]], None],
    *,
    delay: float = 2.0,
) -> None:
    """Persist one completed visit before progressing; interruption never marks it normal."""
    viewer_qq = numeric_id(viewer_qq)
    for qq in store.pending():
        if stop.is_set():
            return
        observation = browser.inspect(qq, viewer_qq)
        if observation.qq != qq:
            raise InspectionError("页面账号与当前成员不一致，巡检已暂停。")
        store.save(observation)
        on_progress(dict(store.summary()))
        if observation.status == BLOCKED:
            reason = REASONS.get(observation.reason, "页面状态无法确认")
            if observation.reason == "platform_access_blocked":
                raise PlatformAccessBlocked(
                    f"{reason}。任务已暂停，已完成结果保留；当前成员仍未完成，不能据此判断成员异常。"
                )
            raise InspectionError(
                f"巡检已暂停（QQ {observation.qq}）：{reason}。"
                "请查看专用浏览器；处理后可继续，已完成结果会保留。"
            )
        if stop.wait(delay):
            return


def data_root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
    return base / "QQSpaceInspector"


class Service:
    def __init__(self) -> None:
        from app.config import get_settings

        from .directory import from_local_config
        from .store import Store

        self._store_type = Store
        try:
            self.source_id = numeric_id(get_settings().onebot_self_id)
        except Exception:
            raise InspectionError("请先在本项目配置中填写有效的机器人 QQ 号。") from None
        self.root = data_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(self.root / "desktop.lock")
        try:
            self._lock.__enter__()
        except LockError:
            raise InspectionError("另一个空间巡检窗口正在使用专用浏览器，请先关闭它。") from None
        try:
            config_dir = Path(os.environ.get("QQ_SPACE_NAPCAT_CONFIG_DIR", "D:/QQ/config"))
            self._directory = from_local_config(config_dir, self.source_id)
            self._browser = Browser(self.root / "browser")
        except Exception:
            self._lock.__exit__(None, None, None)
            raise
        self._store: Store | None = None
        self._task_lock: FileLock | None = None
        self.current_folder: Path | None = None

    def groups(self) -> list[Group]:
        return self._directory.groups()

    def history(self) -> list[dict[str, object]]:
        from .history import list_tasks

        return list_tasks(self.root / "tasks")

    def open_browser(self) -> None:
        self._browser.open()

    def viewer(self) -> str:
        return self._browser.viewer()

    def _release_task(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None
        if self._task_lock is not None:
            self._task_lock.__exit__(None, None, None)
            self._task_lock = None
        self.current_folder = None

    def _open_task(self, folder: Path, *, create: bool) -> None:
        self._release_task()
        folder = folder.resolve()
        if not create and not folder.is_dir():
            raise InspectionError("请选择本工具已有的任务文件夹。")
        lock = FileLock(folder / "task.lock")
        try:
            lock.__enter__()
        except LockError:
            raise InspectionError("此任务正在被另一个巡检窗口使用。") from None
        try:
            store = self._store_type(folder, create=create)
        except Exception:
            lock.__exit__(None, None, None)
            raise
        self._store, self._task_lock, self.current_folder = store, lock, folder

    def create(self, group_ids: list[str]) -> None:
        ids = {numeric_id(group_id) for group_id in group_ids}
        if not 1 <= len(ids) <= 10:
            raise InspectionError("每次请选择 1 至 10 个群。")
        viewer = self.viewer()
        available = {group.group_id: group for group in self.groups()}
        if not ids.issubset(available):
            raise InspectionError("所选群已不在当前机器人群列表中，请刷新后重试。")
        snapshots = [
            (available[group_id], self._directory.members(group_id)) for group_id in sorted(ids)
        ]
        if sum(len(members) for _, members in snapshots) > 50000:
            raise InspectionError("所选成员数量超出本次巡检上限，请分批选群。")
        folder = (
            self.root / "tasks" / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8])
        )
        self._open_task(folder, create=True)
        assert self._store is not None
        self._store.bind_source(self.source_id)
        self._store.bind_viewer(viewer)
        for group, members in snapshots:
            self._store.add_snapshot(group, members)
        self._store.seal_snapshots()

    def resume(self, folder: Path) -> None:
        # Loading saved evidence for review/export must work without a website login.
        # Live identities are checked when the user explicitly continues scanning.
        self._open_task(folder, create=False)

    def scan(self, stop: threading.Event, on_progress: Callable[[dict[str, object]], None]) -> None:
        if self._store is None:
            raise InspectionError("请先选择群创建任务，或载入已有任务。")
        self._directory.verify_identity()
        viewer = self.viewer()
        self._store.bind_source(self.source_id)
        self._store.bind_viewer(viewer)
        run_scan(self._store, self._browser, viewer, stop, on_progress)

    def summary(self) -> dict[str, object]:
        return dict(self._store.summary()) if self._store else {}

    def rows(self) -> list[dict[str, object]]:
        return self._store.rows(limit=200) if self._store else []

    def export(self) -> Path:
        if self._store is None:
            raise InspectionError("请先创建或载入任务。")
        return self._store.export()

    def close(self) -> None:
        failed = False
        for cleanup in (self._release_task, self._browser.close, self._directory.close):
            try:
                cleanup()
            except Exception:
                failed = True
        if failed:
            raise InspectionError("关闭未完成。请手动关闭专用浏览器，再重试退出；任务记录保留。")
        else:
            self._lock.__exit__(None, None, None)
