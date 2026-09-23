"""Desktop orchestration. It has no moderation database or action adapter access."""

from __future__ import annotations

import os
import sys
import threading
import time
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
    BrowserConfirmationRequired,
    Group,
    InspectionError,
    MemberPageUnrecognized,
    Observation,
    PlatformAccessBlocked,
    numeric_id,
)
from .export_paths import default_export_root
from .options import ScanOptions

EXPERIMENT_BATCH_SIZE = 300
EXPERIMENT_DELAY_SECONDS = 30.0


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
    max_checks: int | None = None,
    continuous: bool = False,
    batch_pause: float = 60.0,
    cache_lookup: Callable[[str], Observation | None] | None = None,
    on_saved: Callable[[Observation], None] | None = None,
    defer_qq: str = "",
    deferred_qqs: frozenset[str] = frozenset(),
) -> None:
    """Persist one completed visit before progressing; interruption never marks it normal."""
    viewer_qq = numeric_id(viewer_qq)
    if max_checks is not None and (type(max_checks) is not int or max_checks <= 0):
        raise InspectionError("每批检查人数必须是正整数。")
    pending = store.pending()
    if type(continuous) is not bool or delay < 0 or batch_pause < 0:
        raise InspectionError("巡检间隔或续批设置无效。")
    pending = [qq for qq in pending if qq != defer_qq and qq not in deferred_qqs]
    batch = pending if max_checks is None or continuous else pending[:max_checks]
    live_count = 0
    reused = 0
    started = time.monotonic()
    for index, qq in enumerate(batch):
        if stop.is_set():
            return
        observation = cache_lookup(qq) if cache_lookup is not None else None
        if observation is None:
            if live_count:
                boundary = continuous and max_checks is not None and live_count % max_checks == 0
                seconds = max(delay, batch_pause) if boundary else delay
                if continuous:
                    on_progress(
                        {
                            **store.summary(),
                            "phase": "批间休息" if boundary else "等待下一次访问",
                            "wait_seconds": seconds,
                            "run_live": live_count,
                            "run_reused": reused,
                        }
                    )
                if stop.wait(seconds):
                    return
            observation = browser.inspect(qq, viewer_qq)
            live_count += 1
        else:
            reused += 1
        if observation.qq != qq:
            raise InspectionError("页面账号与当前成员不一致，巡检已暂停。")
        store.save(observation)
        if on_saved is not None:
            on_saved(observation)
        progress: dict[str, object] = dict(store.summary())
        if continuous or cache_lookup is not None:
            remaining = len(batch) - index - 1
            progress.update(
                {
                    "phase": "检查中",
                    "run_live": live_count,
                    "run_reused": reused,
                    "remaining": remaining,
                    "eta_seconds": int((time.monotonic() - started) / (index + 1) * remaining),
                }
            )
        on_progress(progress)
        if observation.status == BLOCKED:
            reason = REASONS.get(observation.reason, "页面状态无法确认")
            if observation.reason == "platform_access_blocked":
                raise PlatformAccessBlocked(
                    f"{reason}。任务已暂停，已完成结果保留；当前成员仍未完成，不能据此判断成员异常。"
                )
            message = (
                f"巡检已暂停（QQ {observation.qq}）：{reason}。"
                "请查看专用浏览器；处理后可继续，已完成结果会保留。"
            )
            if observation.reason in {"login_required", "viewer_missing_or_changed"}:
                raise BrowserConfirmationRequired(message)
            if (
                observation.reason == "unrecognized_page"
                and observation.evidence.get("viewer_qq") == viewer_qq
                and observation.evidence.get("ready_state") == "complete"
                and observation.evidence.get("page_url") == f"https://user.qzone.qq.com/{qq}"
            ):
                raise MemberPageUnrecognized(message, qq)
            raise InspectionError(message)


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
        self.export_root = default_export_root()
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
        self._deferred: set[str] = set()

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
        self._deferred = set()

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

    def scan(
        self,
        stop: threading.Event,
        on_progress: Callable[[dict[str, object]], None],
        options: ScanOptions | None = None,
    ) -> None:
        if self._store is None:
            raise InspectionError("请先选择群创建任务，或载入已有任务。")
        self._directory.verify_identity()
        viewer = self.viewer()
        self._store.bind_source(self.source_id)
        self._store.bind_viewer(viewer)
        if options is not None:
            self._optimized_scan(stop, on_progress, viewer, options)
            return
        run_scan(
            self._store,
            self._browser,
            viewer,
            stop,
            on_progress,
            delay=EXPERIMENT_DELAY_SECONDS,
            max_checks=EXPERIMENT_BATCH_SIZE,
        )

    def _optimized_scan(
        self,
        stop: threading.Event,
        on_progress: Callable[[dict[str, object]], None],
        viewer: str,
        options: ScanOptions,
    ) -> None:
        from .cache import ObservationCache

        options.validate()
        assert self._store is not None
        store = self._store
        if not options.defer_qq:
            self._deferred.clear()
        if options.defer_qq:
            row = store.db.execute(
                "SELECT * FROM observations WHERE qq=?", (options.defer_qq,)
            ).fetchone()
            if row is None or row["status"] != BLOCKED or row["reason"] != "unrecognized_page":
                raise InspectionError("只能将当前未识别的成员页面留待人工复查。")
            import json

            evidence = json.loads(row["evidence_json"])
            if evidence["viewer_qq"] != viewer or evidence["ready_state"] != "complete":
                raise InspectionError("页面身份未确认，不能跳过继续。")
            self._deferred.add(options.defer_qq)
        cache = ObservationCache(self.root / "observations.sqlite3")
        try:
            cache.remember(store)
            self._browser.configure_scan(stop, options.lightweight)

            def progress(payload: dict[str, object]) -> None:
                payload["reused"] = store.reused_count()
                if cache.warning:
                    payload["warning"] = cache.warning
                elif self._browser.loading_warning:
                    payload["warning"] = self._browser.loading_warning
                on_progress(payload)

            run_scan(
                store,
                self._browser,
                viewer,
                stop,
                progress,
                delay=options.delay_seconds,
                max_checks=options.batch_size,
                continuous=options.continuous,
                batch_pause=options.batch_pause_seconds,
                cache_lookup=lambda qq: cache.lookup(store, qq, options.reuse_hours),
                on_saved=lambda observation: cache.remember(store, observation.qq),
                deferred_qqs=frozenset(self._deferred),
            )
        finally:
            active_error = sys.exception()
            cleanup_failed = False
            for cleanup in (cache.close, self._browser.finish_scan):
                try:
                    cleanup()
                except Exception:
                    cleanup_failed = True
            if cleanup_failed and active_error is None:
                raise InspectionError("本轮结果已保存，但浏览器清理未完成；请关闭并重开巡检。")

    def summary(self) -> dict[str, object]:
        return (
            {
                **self._store.summary(),
                "task_label": self._store.task_label(),
                "reused": self._store.reused_count(),
                "deferred": (
                    len(self._deferred.intersection(self._store.pending()))
                    if self._store.metadata["prepared"]
                    else 0
                ),
            }
            if self._store
            else {}
        )

    def rows(self) -> list[dict[str, object]]:
        return self._store.rows(limit=200) if self._store else []

    def export(self) -> Path:
        if self._store is None:
            raise InspectionError("请先创建或载入任务。")
        return self._store.export(self.export_root)

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
