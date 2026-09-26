"""Small ADB adapter limited to reading pages and known navigation gestures."""

from __future__ import annotations

import hashlib
import re
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from .pages import PACKAGE, InspectionError, Page, Rect, parse_page

SUPPORTED_QQ = "9.3.60"
ACTIVITIES = {
    "com.tencent.mobileqq.activity.TroopMemberListActivity": {"members"},
    "com.tencent.mobileqq.profilecard.activity.FriendProfileCardActivity": {
        "profile",
        "profile_cover",
        "warning",
    },
    "com.tencent.mobileqq.activity.QPublicFragmentActivity": {"group"},
}


class Paused(InspectionError):
    pass


@dataclass(frozen=True)
class DeviceIdentity:
    device_hash: str
    android_user: int
    qq_version: str


class Phone:
    def __init__(self, adb: Path, stop: threading.Event) -> None:
        self.adb = adb.resolve()
        self.stop = stop
        self.serial = ""
        self.identity: DeviceIdentity | None = None
        if not self.adb.is_file() or self.adb.name.lower() not in {"adb", "adb.exe"}:
            raise InspectionError("请选择 Android Platform-Tools 中的 adb.exe。")

    def _run(self, *args: str, timeout: float = 20) -> bytes:
        if self.stop.is_set():
            raise Paused("已暂停；已有结果已保存。")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            result = subprocess.run(
                [str(self.adb), *args],
                capture_output=True,
                timeout=timeout,
                creationflags=flags,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InspectionError("手机连接或读取超时，请检查数据线和 USB 调试。") from exc
        if result.returncode != 0:
            raise InspectionError("手机未授权、已断开或读取失败，请检查连接后继续。")
        if len(result.stdout) > 8_000_000:
            raise InspectionError("手机返回的数据过大，已暂停。")
        return result.stdout

    def adb_call(self, *args: str) -> bytes:
        return self._run("-s", self.serial, *args)

    def connect(self) -> DeviceIdentity:
        if self._run("-d", "get-state").strip() != b"device":
            raise InspectionError("请连接一部已授权的 USB 手机。")
        self.serial = self._run("-d", "get-serialno").decode("ascii").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", self.serial):
            raise InspectionError("设备标识无法确认。")
        user, _ = self.foreground()
        package = self.adb_call("shell", "dumpsys", "package", PACKAGE).decode("utf-8", "replace")
        versions = set(re.findall(r"\bversionName=([^\s]+)", package))
        if versions != {SUPPORTED_QQ}:
            raise InspectionError(f"当前适配 QQ {SUPPORTED_QQ}；检测到其他版本，请先验证适配。")
        self.identity = DeviceIdentity(
            hashlib.sha256(self.serial.encode()).hexdigest(), user, SUPPORTED_QQ
        )
        return self.identity

    def foreground(self) -> tuple[int, str]:
        raw = self.adb_call("shell", "dumpsys", "window").decode("utf-8", "replace")
        matches = re.findall(r"mCurrentFocus=Window\{[^}\r\n]*\bu(\d+)\s+([^\s/]+)/([^\s}]+)", raw)
        if len(matches) != 1 or matches[0][1] != PACKAGE:
            raise InspectionError("QQ 不在前台；请解锁手机并回到当前群。")
        user, activity = int(matches[0][0]), matches[0][2]
        if activity not in ACTIVITIES:
            raise InspectionError("当前不是已适配的群信息、成员列表或资料卡页面。")
        if self.identity is not None and user != self.identity.android_user:
            raise InspectionError("QQ 分身发生变化，已暂停。")
        power = self.adb_call("shell", "dumpsys", "power")
        policy = self.adb_call("shell", "dumpsys", "window", "policy")
        if (
            b"mWakefulness=Awake" not in power
            or re.search(rb"\bshowing=true\b", policy)
            or b"mShowingLockscreen=true" in raw.encode()
        ):
            raise InspectionError("手机已锁屏或屏幕未唤醒，已暂停。")
        return user, activity

    def snapshot(self) -> Page:
        before = self.foreground()
        temporary = "/data/local/tmp/qqphone-" + uuid.uuid4().hex + ".xml"
        raw: bytes | None = None
        try:
            self.adb_call("shell", "uiautomator", "dump", temporary)
            raw = self.adb_call("exec-out", "cat", temporary)
        finally:
            # Only this invocation's UUID file, never a recursive or computed parent deletion.
            if raw is not None:
                # Cleanup also works after the pause flag is set; no UI interaction here.
                subprocess.run(
                    [str(self.adb), "-s", self.serial, "shell", "rm", "--", temporary],
                    capture_output=True,
                    timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    check=False,
                )
        if before != self.foreground():
            raise InspectionError("采集期间页面发生切换，已暂停。")
        page = parse_page(raw)
        if page.kind != "unknown" and page.kind not in ACTIVITIES[before[1]]:
            raise InspectionError("页面内容与当前窗口不一致，已暂停。")
        return page

    def delay(self, seconds: float = 1.0) -> None:
        if self.stop.wait(seconds):
            raise Paused("已暂停；已有结果已保存。")

    def tap(self, expected: Page, target: Rect) -> None:
        fresh = self.snapshot()
        if (
            fresh.kind != expected.kind
            or fresh.qq != expected.qq
            or fresh.group_id != expected.group_id
            or fresh.fingerprint != expected.fingerprint
        ):
            raise InspectionError("点击前页面已变化，未执行点击。")
        allowed = [r.target for r in fresh.rows] + [fresh.back, fresh.confirm, fresh.members_button]
        if target not in allowed:
            raise InspectionError("目标不是允许的查看或返回控件。")
        self.foreground()
        x, y = target.center
        self.adb_call("shell", "input", "tap", str(x), str(y))
        self.delay()

    def scroll(self, expected: Page, *, toward_bottom: bool) -> None:
        fresh = self.snapshot()
        if (
            fresh.kind != "members"
            or fresh.fingerprint != expected.fingerprint
            or not fresh.viewport
        ):
            raise InspectionError("滚动前成员列表已变化，已暂停。")
        box = fresh.viewport
        x = box.left + (box.right - box.left) // 3
        upper, lower = box.top + box.height // 3, box.bottom - 100
        if lower - upper < 250:
            raise InspectionError("成员列表显示区域过小。")
        start, end = (lower, upper) if toward_bottom else (upper, lower)
        self.foreground()
        self.adb_call("shell", "input", "swipe", str(x), str(start), str(x), str(end), "500")
        self.delay()

    def screenshot(self) -> bytes:
        before = self.foreground()
        image = self.adb_call("exec-out", "screencap", "-p")
        if before != self.foreground() or not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise InspectionError("截图期间窗口变化或截图无效。")
        return image

    def reveal_profile(self, expected: Page) -> None:
        fresh = self.snapshot()
        if (
            expected.kind != "profile_cover"
            or fresh.kind != "profile_cover"
            or fresh.fingerprint != expected.fingerprint
            or not fresh.viewport
        ):
            raise InspectionError("资料卡封面发生变化，未执行滑动。")
        box = fresh.viewport
        x = box.left + (box.right - box.left) // 3
        upper, lower = box.top + box.height // 3, box.bottom - 200
        if lower - upper < 250:
            raise InspectionError("资料卡封面可滑动区域不足。")
        self.foreground()
        self.adb_call("shell", "input", "swipe", str(x), str(lower), str(x), str(upper), "500")
        self.delay()
