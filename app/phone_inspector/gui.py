"""Desktop window; all device work runs outside Tk's event loop."""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .pages import InspectionError
from .service import configured_adb, execute, export_task, remember_adb
from .storage import LABELS, data_root


class Window:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("QQ 手机辅助巡检")
        self.root.geometry("1080x720")
        self.root.minsize(820, 600)
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.stop = threading.Event()
        self.worker: threading.Thread | None = None
        self.closing = False
        self.adb = tk.StringVar(value=configured_adb())
        self.task = tk.StringVar()
        self.mode = tk.StringVar(value="试扫 10 次资料卡")
        self.status = tk.StringVar(value="连接手机，打开目标群的成员列表，然后新建任务。")
        self.counts = tk.StringVar(value="已核对 0 个账号　异常提示 0 个　未确认 0 次")
        self.group = tk.StringVar(value="尚未读取群信息")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(12, 8))
        style.configure("Treeview", rowheight=30, font=("Microsoft YaHei UI", 10))
        outer = ttk.Frame(root, padding=22)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="QQ 手机辅助巡检", font=("Microsoft YaHei UI", 21, "bold")).pack(
            anchor="w"
        )
        ttk.Label(outer, text="读取 QQ 的实际资料卡提示 · 保存账号与群信息 · 随时暂停和导出").pack(
            anchor="w", pady=(8, 18)
        )
        settings = ttk.LabelFrame(outer, text="连接与任务", padding=12)
        settings.pack(fill="x")
        settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="ADB 工具").grid(row=0, column=0, padx=(0, 12))
        ttk.Entry(settings, textvariable=self.adb).grid(row=0, column=1, sticky="ew")
        ttk.Button(settings, text="选择文件", command=self.pick_adb).grid(
            row=0, column=2, padx=(12, 0)
        )
        ttk.Label(settings, text="当前任务").grid(row=1, column=0, padx=(0, 12), pady=10)
        ttk.Entry(settings, textvariable=self.task, state="readonly").grid(
            row=1, column=1, sticky="ew"
        )
        ttk.Button(settings, text="打开已有任务", command=self.pick_task).grid(
            row=1, column=2, padx=(12, 0)
        )
        ttk.Label(settings, text="扫描范围").grid(row=2, column=0, padx=(0, 12))
        ttk.Combobox(
            settings,
            textvariable=self.mode,
            state="readonly",
            values=("试扫 10 次资料卡", "查看 50 次资料卡", "遍历完整列表"),
            width=24,
        ).grid(row=2, column=1, sticky="w")
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=14)
        self.start_button = ttk.Button(
            buttons, text="新建并开始", command=lambda: self.start(False)
        )
        self.start_button.pack(side="left")
        self.resume_button = ttk.Button(
            buttons, text="继续当前任务", command=lambda: self.start(True)
        )
        self.resume_button.pack(side="left", padx=8)
        self.pause_button = ttk.Button(buttons, text="暂停", command=self.pause, state="disabled")
        self.pause_button.pack(side="left")
        self.export_button = ttk.Button(buttons, text="导出结果", command=self.export)
        self.export_button.pack(side="right")
        ttk.Label(outer, textvariable=self.group, font=("Microsoft YaHei UI", 13, "bold")).pack(
            anchor="w"
        )
        ttk.Label(outer, textvariable=self.counts).pack(anchor="w", pady=8)
        frame = ttk.Frame(outer)
        frame.pack(fill="both", expand=True)
        self.table = ttk.Treeview(frame, columns=("qq", "result", "time"), show="headings")
        for name, label, width in (
            ("qq", "QQ 号", 150),
            ("result", "观察结果", 440),
            ("time", "时间（UTC）", 220),
        ):
            self.table.heading(name, text=label)
            self.table.column(name, width=width)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.table.pack(fill="both", expand=True)
        ttk.Label(outer, textvariable=self.status, wraplength=1000).pack(anchor="w", pady=(12, 6))
        ttk.Label(
            outer,
            text="扫描时请保持 QQ 前台、竖屏并暂停操作手机。继续任务会从顶部复核，合并去重；未出现提示不代表账号正常。",
            wraplength=1000,
            foreground="#586576",
        ).pack(anchor="w")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self.poll)

    def busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def pick_adb(self) -> None:
        if not self.busy():
            value = filedialog.askopenfilename(
                title="选择 adb.exe", filetypes=[("ADB", "adb.exe"), ("所有文件", "*")]
            )
            if value:
                self.adb.set(value)

    def pick_task(self) -> None:
        if not self.busy():
            value = filedialog.askdirectory(
                title="选择包含 inspection.sqlite3 的任务目录",
                initialdir=str(data_root() / "tasks"),
            )
            if value:
                self.task.set(value)

    def start(self, resume: bool) -> None:
        if self.busy():
            return
        if resume and not self.task.get():
            messagebox.showinfo("选择任务", "请先打开已有任务。")
            return
        adb, folder = self.adb.get(), self.task.get() if resume else None
        if not Path(adb).is_file():
            messagebox.showerror("连接工具", "请选择有效的 adb.exe 文件。")
            return
        limit = {"试扫 10 次资料卡": 10, "查看 50 次资料卡": 50, "遍历完整列表": 0}[self.mode.get()]
        self.stop.clear()
        self.status.set("正在读取群信息并定位列表顶部，请暂时不要操作手机……")
        for button in (self.start_button, self.resume_button, self.export_button):
            button.configure(state="disabled")
        self.pause_button.configure(state="normal")

        def work() -> None:
            try:
                remember_adb(adb)
                result = execute(
                    Path(adb),
                    resume=Path(folder) if folder else None,
                    limit=limit,
                    stop=self.stop,
                    notify=self.events.put,
                )
                self.events.put({"done": True, **result})
            except Exception as exc:
                reason = (
                    str(exc)
                    if isinstance(exc, InspectionError)
                    else f"运行失败（{type(exc).__name__}），已停止。请保留任务目录。"
                )
                self.events.put({"done": True, "error": reason})

        self.worker = threading.Thread(target=work, name="qq-phone-inspection", daemon=False)
        self.worker.start()

    def pause(self) -> None:
        self.stop.set()
        self.status.set("正在暂停；等待当前读取结束，已有结果会保留。")

    def export(self) -> None:
        if self.busy() or not self.task.get():
            messagebox.showinfo("导出", "请先暂停扫描并选择一个任务。")
            return
        try:
            folder = export_task(Path(self.task.get()))
            self.status.set("已导出 TXT、CSV 和完整 JSON 报告：" + str(folder))
            if os.name == "nt":
                os.startfile(folder)
        except Exception as exc:
            messagebox.showerror("导出未完成", str(exc))

    def poll(self) -> None:
        while not self.events.empty():
            event = self.events.get_nowait()
            if "task_folder" in event:
                self.task.set(event["task_folder"])
            if "group" in event:
                group = event["group"]
                self.group.set(f"{group.get('group_name', '')}　群号 {group.get('group_id', '')}")
            if "stats" in event:
                stats = event["stats"]
                self.counts.set(
                    f"已核对 {stats['checked']} 个账号　异常提示 {stats['warnings']} 个　未确认 {stats['unresolved']} 次"
                )
                latest = stats.get("latest_pass") or {}
                self.status.set(latest.get("reason") or "正在逐个查看成员资料卡……")
            if "rows" in event:
                self.table.delete(*self.table.get_children())
                for row in event["rows"]:
                    self.table.insert(
                        "", "end", values=(row["qq"], LABELS[row["result"]], row["finished"])
                    )
            if event.get("done"):
                for button in (self.start_button, self.resume_button, self.export_button):
                    button.configure(state="normal")
                self.pause_button.configure(state="disabled")
                if event.get("error"):
                    self.status.set(event["error"])
        if self.closing and not self.busy():
            self.root.destroy()
        else:
            self.root.after(100, self.poll)

    def close(self) -> None:
        self.closing = True
        self.pause()
        if not self.busy():
            self.root.destroy()


def main() -> None:
    root = tk.Tk()
    Window(root)
    root.mainloop()
