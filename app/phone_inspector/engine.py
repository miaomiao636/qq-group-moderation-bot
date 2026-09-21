"""Bounded, deterministic navigation with append-only observation evidence."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import asdict
from typing import Any, Protocol

from .device import DeviceIdentity, Paused
from .pages import WARNING_TEXT, InspectionError, Page, Rect
from .storage import Store, now


class PhoneInterface(Protocol):
    identity: DeviceIdentity | None
    stop: threading.Event

    def snapshot(self) -> Page: ...
    def tap(self, expected: Page, target: Rect) -> None: ...
    def scroll(self, expected: Page, *, toward_bottom: bool) -> None: ...
    def delay(self, seconds: float = 1.0) -> None: ...
    def screenshot(self) -> bytes: ...


class Scanner:
    def __init__(
        self,
        phone: PhoneInterface,
        store: Store,
        progress: Callable[[dict[str, Any]], None] | None = None,
        stamp: dict[str, Any] | None = None,
    ) -> None:
        self.phone = phone
        self.store = store
        self.progress = progress
        self.stamp = stamp or {}

    def check_stop(self) -> None:
        if self.phone.stop.is_set():
            raise Paused("已暂停；继续任务时从顶部重新核对，已有结果保留。")

    def members(self) -> Page:
        page = self.phone.snapshot()
        if page.kind != "members" or not page.rows:
            raise InspectionError("当前成员列表不可读取，已暂停。")
        return page

    def group(self) -> Page:
        page = self.phone.snapshot()
        if page.kind == "warning" and page.confirm:
            self.phone.tap(page, page.confirm)
            page = self.phone.snapshot()
        if page.kind == "profile" and page.back:
            self.phone.tap(page, page.back)
            page = self.phone.snapshot()
        if page.kind == "members" and page.back:
            self.phone.tap(page, page.back)
            page = self.phone.snapshot()
        if page.kind != "group" or not page.members_button:
            raise InspectionError("请打开目标群的聊天信息或成员列表后继续。")
        return page

    def enter_members(self, group: Page) -> Page:
        assert group.members_button is not None
        self.phone.tap(group, group.members_button)
        return self.members()

    def top(self, page: Page) -> Page:
        unchanged = 0
        for _ in range(500):
            self.check_stop()
            if page.at_top:
                return page
            self.phone.scroll(page, toward_bottom=False)
            following = self.members()
            unchanged = unchanged + 1 if following.fingerprint == page.fingerprint else 0
            if unchanged >= 2:
                raise InspectionError("无法确认成员列表顶部，请手动滑到顶部再继续。")
            page = following
        raise InspectionError("定位列表顶部超过限制，已暂停。")

    def loaded_profile(self) -> Page:
        for _ in range(3):
            page = self.phone.snapshot()
            if page.kind in {"warning", "profile"}:
                return page
            self.phone.delay()
        raise InspectionError("资料卡未加载或出现未知页面，已暂停；没有判定账号状态。")

    def inspect(self, page: Page, row_index: int, pass_id: int, page_index: int) -> str:
        visit_id = self.store.start_visit(pass_id, page_index, row_index)
        observation_started = now()
        try:
            self.phone.tap(page, page.rows[row_index].target)
            profile = self.loaded_profile()
            first_qq = ""
            if profile.kind == "profile":
                first_qq = profile.qq
                # Give a delayed warning a chance to arrive; never call this "normal".
                self.phone.delay(2.0)
                profile = self.loaded_profile()
                if profile.kind == "profile" and profile.qq != first_qq:
                    raise InspectionError("资料卡账号在读取期间变化，已暂停。")
            warning = profile.kind == "warning"
            warning_evidence = ""
            if warning:
                image = self.phone.screenshot()
                warning_evidence = self.store.evidence(
                    visit_id + "-warning",
                    {
                        "warning.xml": profile.raw,
                        "warning.png": image,
                    },
                )
                if profile.confirm is None:
                    raise InspectionError("没有识别到异常提示的确认按钮。")
                self.phone.tap(profile, profile.confirm)
                profile = self.loaded_profile()
            if profile.kind != "profile" or not profile.qq or not profile.back:
                raise InspectionError("无法把提示对应到明确 QQ 号，已暂停。")
            if first_qq and profile.qq != first_qq:
                raise InspectionError("异常提示前后的 QQ 号不一致，已暂停。")
            result = "WARNING_OBSERVED" if warning else "WARNING_NOT_OBSERVED_THIS_VISIT"
            observation = {
                "started": observation_started,
                "observed_at": now(),
                "qq": profile.qq,
                "result": result,
                "warning_text": WARNING_TEXT if warning else "",
                "group": self.store.metadata(),
                "execution": self.stamp,
                "page_index": page_index,
                "row_index": row_index,
                "member_page_fingerprint": page.fingerprint,
                "group_link_basis": "guarded_continuous_ui_navigation",
                "profile_xml_sha256": hashlib.sha256(profile.raw).hexdigest(),
                "profile_raw_retained": warning,
                "warning_evidence": warning_evidence,
                "no_warning_limit": "bounded observations of this visit only; not proof of a normal account",
            }
            files = {
                "observation.json": json.dumps(observation, ensure_ascii=False, indent=2).encode()
            }
            if warning:
                files["restricted-profile.xml"] = profile.raw
            evidence = self.store.evidence(visit_id, files)
            self.store.finish_visit(visit_id, profile.qq, result, evidence)
        except InspectionError as exc:
            self.store.finish_visit(visit_id, None, "INCONCLUSIVE", reason=str(exc))
            raise
        self.phone.tap(profile, profile.back)
        returned = self.members()
        if returned.fingerprint != page.fingerprint:
            raise InspectionError(
                "返回后成员列表位置或内容变化，已有观察已保存；请继续任务重新核对。"
            )
        return profile.qq

    def run(self, limit: int = 0) -> dict[str, Any]:
        if type(limit) is not int or not 0 <= limit <= 20_000:
            raise ValueError("Invalid visit limit")
        self.check_stop()
        if self.phone.identity is None:
            raise InspectionError("请先连接手机。")
        group = self.group()
        self.store.bind(
            {
                **self.stamp,
                **asdict(self.phone.identity),
                "group_id": group.group_id,
                "group_name": group.group_name,
                "created_at": now(),
            }
        )
        pass_id = self.store.begin_pass(
            group.member_count,
            {
                **self.stamp,
                **asdict(self.phone.identity),
                "group_id": group.group_id,
                "group_name": group.group_name,
                "member_count_before": group.member_count,
            },
        )
        try:
            page = self.top(self.enter_members(group))
            previous: tuple[str, tuple[str, ...]] | None = None
            repeated = 0
            visits = 0
            for page_index in range(2_000):
                identities = []
                for row_index in range(len(page.rows)):
                    self.check_stop()
                    if visits >= (limit or 20_000):
                        raise Paused("已达到本次查看上限，结果已保存；可继续任务或导出。")
                    identities.append(self.inspect(page, row_index, pass_id, page_index))
                    visits += 1
                    if self.progress:
                        self.progress(
                            {
                                "stats": self.store.stats(),
                                "rows": self.store.observations(),
                                "group": self.store.metadata(),
                            }
                        )
                current = (page.fingerprint, tuple(identities))
                repeated = repeated + 1 if current == previous else 0
                # Both visible layout and actual QQ sequence must remain unchanged
                # through two scroll attempts. Identical nicknames alone cannot end a scan.
                if repeated >= 2:
                    after = self.group()
                    if after.group_id != group.group_id:
                        raise InspectionError("结束核对时群号变化，不能声明到达原群列表末尾。")
                    unique = len(self.store.pass_qqs(pass_id))
                    reconciled = group.member_count == after.member_count == unique
                    reason = (
                        "已到列表末尾；本轮不同 QQ 数与前后群人数一致。"
                        if reconciled
                        else "已到列表末尾，但本轮不同 QQ 数与群人数尚未完全核对。"
                    )
                    self.enter_members(after)
                    self.store.end_pass(pass_id, "END_REACHED", reason, after.member_count)
                    return self.store.stats()
                previous = current
                self.phone.scroll(page, toward_bottom=True)
                page = self.members()
            raise Paused("列表遍历达到保护上限，已保存结果。")
        except InspectionError as exc:
            self.store.end_pass(pass_id, "PAUSED", str(exc))
            return self.store.stats()
