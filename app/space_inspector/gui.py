"""Local desktop interface; every service call belongs to one worker thread."""

from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .contracts import LABELS, REASONS, Group, InspectionError
from .export_paths import default_export_root
from .options import ScanOptions
from .service import data_root
from .worker import Command, Event, run_worker


class Window:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self._commands: queue.Queue[Command] = queue.Queue()
        self._events: queue.Queue[Event] = queue.Queue(maxsize=100)
        self._stop = threading.Event()
        self._groups: dict[str, Group] = {}
        self._selected: set[str] = set()
        self._busy = False
        self._scanning = False
        self._closing = False
        self._shutdown_failed = False
        self._logged_in = False
        self._folder: Path | None = None
        self._search_timer: str | None = None
        self._history_dialog: tk.Toplevel | None = None
        self._source = tk.StringVar(value="正在读取机器人账号和群列表……")
        self._viewer = tk.StringVar(value="空间访问账号：尚未确认登录")
        self._search = tk.StringVar()
        self._selected_label = tk.StringVar(value="已选择 0 个群")
        self._status = tk.StringVar(value="正在准备……")
        self._summary = tk.StringVar(value="尚未创建巡检任务")
        self._folder_text = tk.StringVar()
        self._task_text = tk.StringVar(value="尚未创建或载入任务")
        self._export_text = tk.StringVar()
        self._export_root_text = tk.StringVar(value=str(default_export_root()))
        self._automatic = tk.BooleanVar(value=True)
        self._reuse = tk.BooleanVar(value=True)
        self._lightweight = tk.BooleanVar(value=True)
        self._delay = tk.StringVar(value="30")
        self._batch_size = tk.StringVar(value="300")
        self._batch_pause = tk.StringVar(value="60")
        self._defer_qq = ""
        self._build()
        self._worker = threading.Thread(
            target=run_worker,
            args=(self._commands, self._events, self._stop),
            name="space-inspector",
            daemon=False,
        )
        self._worker.start()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._submit("groups")
        self.root.after(100, self._poll)

    def _build(self) -> None:
        self.root.title("QQ 群空间限制巡检")
        self.root.geometry("1040x800")
        self.root.minsize(800, 660)
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(4, weight=2)
        outer.rowconfigure(9, weight=1)

        ttk.Label(outer, text="QQ 群空间限制巡检", font=("Microsoft YaHei UI", 16, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            outer,
            text="只记录 QQ 空间官方限制提示。未观察到提示一律待确认，不表示账号正常，也不会自动处理群成员。",
            wraplength=950,
        ).grid(row=1, column=0, sticky="ew", pady=(4, 10))

        account = ttk.Frame(outer)
        account.grid(row=2, column=0, sticky="ew")
        account.columnconfigure(0, weight=1)
        ttk.Label(account, textvariable=self._source).grid(row=0, column=0, sticky="w")
        ttk.Label(account, textvariable=self._viewer).grid(row=1, column=0, sticky="w", pady=4)
        self._reload = ttk.Button(
            account, text="刷新群列表", command=lambda: self._submit("groups")
        )
        self._reload.grid(row=0, column=1, padx=4)
        self._browser = ttk.Button(
            account, text="打开空间登录", command=lambda: self._submit("browser")
        )
        self._browser.grid(row=1, column=1, padx=4)
        self._confirm = ttk.Button(
            account, text="确认已登录", command=lambda: self._submit("viewer")
        )
        self._confirm.grid(row=1, column=2)

        search = ttk.Frame(outer)
        search.grid(row=3, column=0, sticky="ew", pady=(8, 4))
        search.columnconfigure(1, weight=1)
        ttk.Label(search, text="查找群：").grid(row=0, column=0)
        self._search_entry = ttk.Entry(search, textvariable=self._search)
        self._search_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self._search.trace_add("write", self._search_changed)
        self._select_visible = ttk.Button(search, text="选择筛选结果", command=self._select_all)
        self._select_visible.grid(row=0, column=2, padx=4)
        self._clear_selection = ttk.Button(search, text="清空选择", command=self._clear)
        self._clear_selection.grid(row=0, column=3)
        ttk.Label(search, textvariable=self._selected_label).grid(row=0, column=4, padx=(10, 0))

        self._group_tree = self._tree(outer, 4, ("name", "id", "count"), ("群名称", "群号", "人数"))
        self._group_tree.column("name", width=520, stretch=True)
        self._group_tree.column("id", width=180, stretch=False)
        self._group_tree.column("count", width=70, stretch=False)
        self._group_tree.bind("<<TreeviewSelect>>", self._selection_changed)

        actions = ttk.Frame(outer)
        actions.grid(row=5, column=0, sticky="ew", pady=8)
        self._start_button = ttk.Button(actions, text="开始检查所选群", command=self._start)
        self._start_button.pack(side="left", padx=(0, 6))
        self._resume_button = ttk.Button(actions, text="载入已有任务", command=self._resume)
        self._resume_button.pack(side="left", padx=6)
        self._continue_button = ttk.Button(
            actions, text="继续检查", command=lambda: self._submit("scan")
        )
        self._continue_button.pack(side="left", padx=6)
        self._pause_button = ttk.Button(actions, text="暂停", command=self._pause)
        self._pause_button.pack(side="left", padx=6)
        self._defer_button = ttk.Button(
            actions, text="留待复查并继续", command=lambda: self._submit("scan", self._defer_qq)
        )
        self._defer_button.pack(side="left", padx=6)
        self._export_button = ttk.Button(
            actions, text="导出结果", command=lambda: self._submit("export")
        )
        self._export_button.pack(side="right")

        paths = ttk.Frame(outer)
        paths.grid(row=6, column=0, sticky="ew", pady=(0, 6))
        paths.columnconfigure(1, weight=1)
        for row, (label, variable) in enumerate(
            [
                ("当前任务：", self._task_text),
                ("续扫数据：", self._folder_text),
                ("最近导出：", self._export_text),
                ("导出总目录：", self._export_root_text),
            ]
        ):
            ttk.Label(paths, text=label).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(paths, textvariable=variable, state="readonly").grid(
                row=row, column=1, sticky="ew"
            )
        ttk.Button(paths, text="打开任务数据", command=self._open_task_folder).grid(
            row=1, column=2, padx=(8, 0)
        )
        ttk.Button(paths, text="打开本次结果", command=self._open_export).grid(
            row=2, column=2, padx=(8, 0)
        )
        ttk.Button(
            paths, text="查看所有导出", command=lambda: self._open_export(all_results=True)
        ).grid(row=3, column=2, padx=(8, 0))
        self._choose_export_button = ttk.Button(
            paths, text="选择导出位置", command=self._choose_export_location
        )
        self._choose_export_button.grid(row=3, column=3, padx=(8, 0))
        ttk.Label(outer, textvariable=self._summary, wraplength=950).grid(
            row=7, column=0, sticky="ew", pady=4
        )
        settings = ttk.Frame(outer)
        settings.grid(row=8, column=0, sticky="ew", pady=4)
        self._setting_widgets: list[ttk.Checkbutton | ttk.Combobox] = []
        for column, (label, toggle_variable) in enumerate(
            [
                ("自动续批", self._automatic),
                ("复用 24 小时内历史观察", self._reuse),
                ("轻量加载", self._lightweight),
            ]
        ):
            toggle = ttk.Checkbutton(settings, text=label, variable=toggle_variable)
            toggle.grid(row=0, column=column * 2, columnspan=2, sticky="w", padx=(0, 10))
            self._setting_widgets.append(toggle)
        for column, (label, variable, values) in enumerate(
            [
                ("间隔/秒", self._delay, (30, 20, 10, 5, 60)),
                ("每批人数", self._batch_size, (300, 100, 50, 10)),
                ("批间休息/秒", self._batch_pause, (60, 120, 300, 600)),
            ]
        ):
            ttk.Label(settings, text=label).grid(row=1, column=column * 2, sticky="w")
            selection = ttk.Combobox(
                settings,
                textvariable=variable,
                values=tuple(str(value) for value in values),
                width=7,
                state="readonly",
            )
            selection.grid(row=1, column=column * 2 + 1, sticky="w", padx=(4, 12))
            self._setting_widgets.append(selection)
        ttk.Label(
            settings,
            text="间隔为试验设置，不保证免拦截；历史观察保留原时间。可随时暂停。",
            wraplength=900,
        ).grid(row=2, column=0, columnspan=6, sticky="w")
        self._result_tree = self._tree(
            outer, 9, ("qq", "status", "reason"), ("QQ 号", "观察结果", "说明")
        )
        self._result_tree.column("qq", width=140, stretch=False)
        self._result_tree.column("status", width=180, stretch=False)
        self._result_tree.column("reason", width=620, stretch=True)
        footer = ttk.Frame(outer)
        footer.grid(row=10, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(footer, textvariable=self._status, wraplength=850).pack(
            side="left", fill="x", expand=True
        )
        self._retry_exit = ttk.Button(footer, text="重试退出", command=self._close)

    @staticmethod
    def _tree(
        parent: ttk.Frame, row: int, columns: tuple[str, ...], labels: tuple[str, ...]
    ) -> ttk.Treeview:
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="extended", height=8
        )
        for column, label in zip(columns, labels, strict=True):
            tree.heading(column, text=label)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        return tree

    def _search_changed(self, *_: str) -> None:
        if self._search_timer is not None:
            self.root.after_cancel(self._search_timer)
        self._search_timer = self.root.after(150, self._render_groups)

    def _render_groups(self) -> None:
        self._search_timer = None
        query = self._search.get().strip().casefold()
        self._group_tree.delete(*self._group_tree.get_children())
        for group in self._groups.values():
            if query and query not in f"{group.name} {group.group_id}".casefold():
                continue
            self._group_tree.insert(
                "",
                "end",
                iid=group.group_id,
                values=(group.name, group.group_id, group.member_count),
            )
            if group.group_id in self._selected:
                self._group_tree.selection_add(group.group_id)
        self._selected_label.set(f"已选择 {len(self._selected)} 个群 / 共 {len(self._groups)} 个群")
        self._controls()

    def _selection_changed(self, _: tk.Event[tk.Misc]) -> None:
        self._selected.difference_update(self._group_tree.get_children())
        self._selected.update(self._group_tree.selection())
        self._selected_label.set(f"已选择 {len(self._selected)} 个群 / 共 {len(self._groups)} 个群")
        self._controls()

    def _select_all(self) -> None:
        self._group_tree.selection_set(self._group_tree.get_children())

    def _clear(self) -> None:
        self._selected.clear()
        self._group_tree.selection_remove(self._group_tree.selection())
        self._selected_label.set(f"已选择 0 个群 / 共 {len(self._groups)} 个群")
        self._controls()

    def _controls(self) -> None:
        available = not self._busy and not self._closing and not self._shutdown_failed
        for button in (
            self._reload,
            self._browser,
            self._confirm,
            self._resume_button,
            self._select_visible,
            self._clear_selection,
            self._choose_export_button,
        ):
            button.configure(state="normal" if available else "disabled")
        self._search_entry.configure(state="normal" if available else "disabled")
        for widget in self._setting_widgets:
            widget.configure(
                state=("readonly" if isinstance(widget, ttk.Combobox) else "normal")
                if available
                else "disabled"
            )
        self._defer_button.configure(
            state="normal" if available and self._logged_in and self._defer_qq else "disabled"
        )
        self._start_button.configure(
            state="normal" if available and self._selected and self._logged_in else "disabled"
        )
        self._continue_button.configure(
            state="normal"
            if available and self._folder is not None and self._logged_in
            else "disabled"
        )
        self._export_button.configure(
            state="normal" if available and self._folder is not None else "disabled"
        )
        self._pause_button.configure(
            state="normal"
            if self._scanning and not self._closing and not self._stop.is_set()
            else "disabled"
        )
        self._retry_exit.configure(
            state="normal" if self._shutdown_failed and not self._closing else "disabled"
        )

    def _submit(self, kind: str, payload: object = None) -> None:
        if self._busy or self._closing or self._shutdown_failed:
            return
        if kind in {"resume", "viewer", "browser"}:
            self._defer_qq = ""
        if kind in {"create", "scan"}:
            try:
                options = ScanOptions(
                    continuous=self._automatic.get(),
                    delay_seconds=int(self._delay.get()),
                    batch_size=int(self._batch_size.get()),
                    batch_pause_seconds=int(self._batch_pause.get()),
                    reuse_hours=24 if self._reuse.get() else 0,
                    lightweight=self._lightweight.get(),
                    defer_qq=str(payload) if kind == "scan" and payload else "",
                )
                options.validate()
            except (ValueError, InspectionError):
                self._status.set("请检查巡检设置：间隔、每批人数及休息时间必须在支持范围内。")
                return
            payload = {"groups": payload, "options": options} if kind == "create" else options
            self._defer_qq = ""
        self._busy = True
        self._scanning = kind in {"create", "scan"}
        if self._scanning:
            self._stop.clear()
        self._status.set("正在检查，请等待……" if self._scanning else "正在处理，请等待……")
        self._controls()
        self._commands.put((kind, payload))

    def _start(self) -> None:
        if self._folder is not None and not messagebox.askyesno(
            "开始新任务",
            "要为所选群创建新任务吗？当前任务会保留，可通过任务目录重新载入。",
            parent=self.root,
        ):
            return
        self._submit(
            "create", [group_id for group_id in self._groups if group_id in self._selected]
        )

    def _resume(self) -> None:
        if self._history_dialog is not None:
            self._history_dialog.lift()
            return
        self._submit("history")

    @staticmethod
    def _history_date(value: object) -> str:
        if not isinstance(value, str):
            return "时间未知"
        try:
            created = datetime.fromisoformat(value)
            if created.tzinfo is None:
                return "时间未知"
            return created.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OverflowError, OSError):
            return "时间未知"

    def _dismiss_history(self) -> None:
        dialog, self._history_dialog = self._history_dialog, None
        if dialog is not None:
            dialog.destroy()

    def _other_task(self) -> None:
        if self._closing or self._shutdown_failed or self._busy:
            return
        chosen = filedialog.askdirectory(
            title="选择已有巡检任务目录",
            parent=self._history_dialog or self.root,
            initialdir=str(data_root() / "tasks"),
            mustexist=True,
        )
        if chosen and not self._closing and not self._shutdown_failed and not self._busy:
            self._dismiss_history()
            self._submit("resume", Path(chosen))

    def _show_history(self, tasks: object, error_message: str = "") -> None:
        if self._closing or self._shutdown_failed:
            return
        self._dismiss_history()
        dialog = tk.Toplevel(self.root)
        self._history_dialog = dialog
        dialog.title("载入已有巡检任务")
        dialog.geometry("900x440")
        dialog.minsize(720, 320)
        dialog.transient(self.root)
        dialog.protocol("WM_DELETE_WINDOW", self._dismiss_history)
        outer = ttk.Frame(dialog, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)
        ttk.Label(
            outer,
            text="最近 50 个任务（本机时间）。载入后可查看或导出，点击“继续检查”才会开始巡检。",
            wraplength=840,
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))
        tree = self._tree(
            outer,
            1,
            ("created", "groups", "checked", "total", "restricted"),
            ("创建时间", "群名称", "已检查", "总人数", "观察到限制"),
        )
        tree.configure(selectmode="browse")
        tree.column("created", width=155, stretch=False)
        tree.column("groups", width=340, stretch=True)
        tree.column("checked", width=75, stretch=False)
        tree.column("total", width=75, stretch=False)
        tree.column("restricted", width=100, stretch=False)
        folders: dict[str, Path] = {}
        if isinstance(tasks, list):
            for task in tasks[:50]:
                if not isinstance(task, dict) or not isinstance(task.get("folder"), Path):
                    continue
                folder = task["folder"]
                row_id = tree.insert(
                    "",
                    "end",
                    values=(
                        self._history_date(task.get("created_at")),
                        str(task.get("group_names", "")),
                        task.get("checked", 0),
                        task.get("total", 0),
                        task.get("restricted", 0),
                    ),
                )
                folders[row_id] = folder

        def load_selected() -> None:
            if self._closing or self._shutdown_failed or self._busy:
                return
            selection = tree.selection()
            folder = folders.get(selection[0]) if selection else None
            if folder is not None:
                self._dismiss_history()
                self._submit("resume", folder)

        buttons = ttk.Frame(outer)
        buttons.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        load_button = ttk.Button(buttons, text="载入所选", command=load_selected)
        load_button.pack(side="left")
        ttk.Button(buttons, text="其他位置", command=self._other_task).pack(side="left", padx=8)
        ttk.Button(buttons, text="取消", command=self._dismiss_history).pack(side="right")
        if folders:
            first = next(iter(folders))
            tree.selection_set(first)
            tree.focus(first)
        else:
            load_button.configure(state="disabled")
            ttk.Label(
                outer,
                text=error_message or "没有找到已保存的任务；可点击“其他位置”选择任务文件夹。",
                wraplength=840,
            ).grid(row=2, column=0, sticky="w", pady=(8, 0))
        dialog.grab_set()
        tree.focus_set()

    def _pause(self) -> None:
        self._stop.set()
        self._status.set("正在暂停，等待当前检查保存完成……")
        self._controls()

    def _close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._dismiss_history()
        self._stop.set()
        self._status.set("正在保存任务并关闭巡检浏览器，请稍候……")
        self._controls()
        self._commands.put(("close", None))

    def _show_summary(self, summary: object) -> None:
        if not isinstance(summary, dict):
            return
        if isinstance(summary.get("task_label"), str):
            self._task_text.set(summary["task_label"])
        labels = [
            ("total", "成员数"),
            ("checked", "已有观察"),
            ("reused", "其中历史复用"),
            ("restricted", "观察到限制"),
            ("unconfirmed", "待确认"),
            ("pending", "未完成（含访问受阻）"),
        ]
        self._summary.set(
            "  |  ".join(f"{label}：{summary.get(key, '—')}" for key, label in labels)
        )

    def _show_snapshot(self, payload: dict[str, object]) -> None:
        folder = payload.get("folder")
        if isinstance(folder, Path):
            self._folder = folder
            self._folder_text.set(str(folder))
        elif "folder" in payload:
            self._folder = None
            self._task_text.set("尚未创建或载入任务")
            self._folder_text.set("")
            self._export_text.set("")
        self._show_summary(payload.get("summary"))
        rows = payload.get("rows")
        if isinstance(rows, list):
            self._result_tree.delete(*self._result_tree.get_children())
            for row in rows[:200]:
                if isinstance(row, dict):
                    status = str(row.get("status", ""))
                    evidence = row.get("evidence", {})
                    reused = isinstance(evidence, dict) and "reuse" in evidence
                    self._result_tree.insert(
                        "",
                        "end",
                        values=(
                            str(row.get("qq", "")),
                            ("历史：" if reused else "") + LABELS.get(status, "待确认"),
                            REASONS.get(str(row.get("reason", "")), "尚未检查")
                            + (f"；原观察 {row.get('checked_at', '')}" if reused else ""),
                        ),
                    )

    def _poll(self) -> None:
        for _ in range(100):
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "closed":
                self._worker.join(timeout=0)
                self.root.destroy()
                return
            if kind == "closing_failed":
                self._closing = False
                self._shutdown_failed = True
                self._busy = False
                self._scanning = False
                self._status.set(str(payload.get("message", "请手动关闭专用浏览器后重试退出。")))
                self._retry_exit.pack(side="right", padx=(8, 0))
                self._controls()
                continue
            if kind == "progress":
                self._show_summary(payload)
                phase = str(payload.get("phase", "检查中"))
                detail = f"{phase}；本轮访问 {payload.get('run_live', 0)} 人，历史复用 {payload.get('run_reused', 0)} 人"
                if "wait_seconds" in payload:
                    detail += f"；等待 {payload['wait_seconds']} 秒，可随时暂停"
                elif isinstance(payload.get("eta_seconds"), int):
                    detail += f"；粗估剩余 {max(1, int(str(payload['eta_seconds'])) // 60)} 分钟（受页面和休息影响）"
                self._status.set(str(payload.get("warning", detail)))
                continue
            if kind == "created":
                self._show_snapshot(payload)
                self._export_text.set("")
                continue
            self._busy = False
            self._scanning = False
            if "export_root" in payload:
                self._export_root_text.set(str(payload["export_root"]))
            if kind == "ready":
                groups = payload.get("groups")
                if isinstance(groups, list):
                    self._groups = {
                        group.group_id: group for group in groups if isinstance(group, Group)
                    }
                    self._selected.intersection_update(self._groups)
                    self._render_groups()
                self._source.set(f"群目录机器人账号：{payload.get('source_id', '—')}")
                self._status.set(
                    str(payload.get("export_settings_error"))
                    if payload.get("export_settings_error")
                    else "选择需要检查的群，然后打开空间登录并确认账号。"
                )
            elif kind == "export_location_saved":
                self._status.set(
                    "导出位置已保存，重开工具后继续使用。后续导出使用新目录，已有文件保留原位。"
                )
            elif kind == "browser_opened":
                self._logged_in = False
                self._viewer.set("空间访问账号：请在独立浏览器中登录，再点击“确认已登录”")
                self._status.set("巡检浏览器已打开。请完成 QQ 空间正常登录。")
            elif kind == "viewer":
                viewer = payload.get("viewer")
                self._logged_in = isinstance(viewer, str) and bool(viewer)
                self._viewer.set(f"空间访问账号：{viewer or '尚未确认登录'}")
                self._status.set(
                    "登录已确认，可以开始检查。"
                    if self._logged_in
                    else "尚未确认登录，请完成登录后重试。"
                )
            elif kind in {"loaded", "scanned"}:
                self._show_snapshot(payload)
                if kind == "loaded":
                    self._export_text.set("")
                    self._status.set("已有任务已载入；确认空间登录后可继续检查，也可直接导出。")
                else:
                    summary = payload.get("summary")
                    pending = summary.get("pending") if isinstance(summary, dict) else None
                    deferred = summary.get("deferred", 0) if isinstance(summary, dict) else 0
                    self._status.set(
                        f"本轮已停止并保存；留待复查 {deferred} 人。点“继续检查”可检查剩余成员并复查。"
                        if pending != 0
                        else "快照成员已检查并保存；未观察到限制提示仍为待确认。"
                    )
            elif kind == "history":
                self._show_history(payload.get("tasks"))
                self._status.set("选择已有任务载入；载入不会自动开始巡检。")
            elif kind == "exported":
                path = str(payload.get("path", ""))
                self._export_text.set(path)
                self._status.set(
                    "结果已导出。点“打开本次结果”，按群名和群号查看 CSV；总表与核验记录也保留。"
                )
            elif kind == "error":
                self._show_snapshot(payload)
                self._defer_qq = str(payload.get("defer_qq", ""))
                if payload.get("operation") == "history":
                    self._show_history([], str(payload.get("message", "历史任务读取未完成。")))
                if payload.get("operation") in {"viewer", "browser"}:
                    self._logged_in = False
                    self._viewer.set("空间访问账号：尚未确认登录")
                if payload.get("requires_browser_confirmation") is True:
                    self._logged_in = False
                    self._viewer.set("空间访问或登录状态需确认；处理专用浏览器后再点“确认已登录”")
                self._status.set(str(payload.get("message", "操作未完成。")))
            if self._closing:
                self._status.set("正在保存任务并关闭巡检浏览器，请稍候……")
            self._controls()
        self.root.after(100, self._poll)

    def _choose_export_location(self) -> None:
        if self._busy or self._closing or self._shutdown_failed:
            return
        current = self._export_root_text.get()
        chosen = filedialog.askdirectory(
            parent=self.root,
            title="选择导出总目录（仅影响后续导出）",
            initialdir=current if current and Path(current).is_dir() else str(Path.home()),
            mustexist=True,
        )
        if chosen:
            self._submit("set_export_root", Path(chosen))

    def _open_export(self, *, all_results: bool = False) -> None:
        target = (
            self._export_root_text.get()
            if all_results or not self._export_text.get()
            else self._export_text.get()
        )
        if target and Path(target).is_dir() and sys.platform == "win32":
            try:
                os.startfile(target)
            except OSError:
                self._status.set("无法打开文件夹，请复制窗口中的导出路径自行打开。")
        else:
            self._status.set(
                "导出目录尚不存在。请先载入任务并点“导出结果”；旧导出仍保留在旧任务数据中。"
            )

    def _open_task_folder(self) -> None:
        target = self._folder or data_root() / "tasks"
        if target.is_dir() and sys.platform == "win32":
            try:
                os.startfile(target)
            except OSError:
                self._status.set("无法打开任务目录；可用“载入已有任务”查看已保存任务。")
        else:
            self._status.set("尚未保存巡检任务；创建任务后可打开任务目录。")


def main() -> None:
    root = tk.Tk()
    Window(root)
    root.mainloop()


if __name__ == "__main__":
    main()
