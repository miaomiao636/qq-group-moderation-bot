"""A dedicated browser owned by the desktop worker; no borrowed browser credentials."""

from __future__ import annotations

import importlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .contracts import (
    BLOCKED,
    NONFRIEND_NOTICE,
    REASONS,
    RESTRICTED,
    RESTRICTION_TEMPLATES,
    UNCONFIRMED,
    InspectionError,
    Observation,
    PlatformAccessBlocked,
    numeric_id,
)
from .contracts import NOTICE as NOTICE


def _platform_block_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and parsed.netloc == "waf.tencent.com"
            and parsed.path == "/501page.html"
        )
    except ValueError:
        return False


def classify_page(raw: object, qq: str, viewer_qq: str) -> Observation:
    qq, viewer_qq = numeric_id(qq), numeric_id(viewer_qq)
    evidence: dict[str, object] = {
        "page_url": "",
        "viewer_qq": "",
        "notice": "",
        "notice_source": "unrecognized_page",
        "ready_state": "unknown",
        "panel_count": 0,
        "report_icon_count": 0,
    }

    def result(status: str, reason: str) -> Observation:
        return Observation(qq, status, reason, datetime.now(UTC).isoformat(), evidence)

    if not isinstance(raw, dict) or not isinstance(raw.get("url"), str):
        return result(BLOCKED, "unrecognized_page")
    if _platform_block_url(raw["url"]):
        evidence["notice_source"] = "platform_access_block"
        return result(BLOCKED, "platform_access_blocked")
    try:
        url = urlsplit(raw["url"])
        if url.scheme == "https" and url.netloc in ("i.qq.com", "qzone.qq.com"):
            evidence["notice_source"] = "login_redirect"
            return result(BLOCKED, "login_required")
        if (
            url.scheme != "https"
            or url.netloc != "user.qzone.qq.com"
            or url.path not in (f"/{qq}", f"/{qq}/", f"/{qq}/main")
            or url.query
            or url.fragment
        ):
            return result(BLOCKED, "unexpected_location")
    except ValueError:
        return result(BLOCKED, "unexpected_location")
    evidence["page_url"] = f"https://user.qzone.qq.com/{qq}"
    ready = raw.get("ready")
    if ready in ("complete", "interactive", "loading"):
        evidence["ready_state"] = ready
    if ready != "complete":
        return result(BLOCKED, "page_incomplete")
    viewers = raw.get("viewers")
    if viewers != [viewer_qq]:
        return result(BLOCKED, "viewer_missing_or_changed")
    evidence["viewer_qq"] = viewer_qq
    panels = raw.get("panels")
    if not isinstance(panels, list) or len(panels) > 10:
        return result(BLOCKED, "unrecognized_page")
    evidence["panel_count"] = len(panels)
    if not panels and raw.get("normal_profile") is True:
        evidence["notice_source"] = "qzone_profile"
        return result(UNCONFIRMED, "no_restriction_notice_observed")
    permission_panels = raw.get("permission_panels", [])
    if (
        not panels
        and raw.get("normal_profile") is False
        and permission_panels
        == [{"tips": "主人设置了权限，您可通过以下方式访问", "apply_link": True}]
    ):
        evidence["notice_source"] = "qzone_permission_page"
        evidence["panel_count"] = 1
        return result(UNCONFIRMED, "space_access_permission_required")
    if len(panels) == 1 and isinstance(panels[0], dict):
        panel = panels[0]
        icons = panel.get("report_icons")
        if type(icons) is int and 0 <= icons <= 10:
            evidence["report_icon_count"] = icons
        paragraphs = panel.get("paragraphs")
        if (
            raw.get("normal_profile") is False
            and type(icons) is int
            and icons == 1
            and paragraphs == ["温馨提示:", NONFRIEND_NOTICE, "返回我的空间"]
        ):
            evidence["notice"] = NONFRIEND_NOTICE
            evidence["notice_source"] = "qzone_nonfriend_page"
            return result(UNCONFIRMED, "space_nonfriend_access_unavailable")
        if (
            raw.get("normal_profile") is False
            and type(icons) is int
            and icons == 0
            and isinstance(paragraphs, list)
            and len(paragraphs) == 2
            and paragraphs[0] == "对方未开通空间"
            and isinstance(paragraphs[1], str)
            and paragraphs[1].split() == ["邀请开通", "返回我的空间"]
        ):
            evidence["notice_source"] = "qzone_unopened_page"
            return result(UNCONFIRMED, "space_not_opened")
        if (
            raw.get("normal_profile") is False
            and type(icons) is int
            and icons == 1
            and isinstance(paragraphs, list)
            and any(paragraphs == list(template) for template in RESTRICTION_TEMPLATES)
        ):
            evidence["notice"] = paragraphs[1]
            evidence["notice_source"] = "qzone_top_level_error"
            return result(RESTRICTED, "qzone_restriction_notice_observed")
    return result(BLOCKED, "unrecognized_page")


# Only inspect the verified top-level system template and account navigation.
# User posts, nicknames, signatures and embedded frames are not classification inputs.
SNAPSHOT_SCRIPT = r"""() => {
    const visible = e => !!(e.getClientRects().length) &&
        getComputedStyle(e).visibility !== 'hidden' && getComputedStyle(e).display !== 'none';
    const links = [...document.querySelectorAll('.page > .page_top a, a.user-home')]
        .filter(a => visible(a) && (!a.matches('a.user-home') ||
            a.querySelector('img[alt="您的头像"]')));
    const viewers = [...new Set(links.map(a => {
        try {
            const u = new URL(a.href);
            if (u.protocol !== 'https:' || u.host !== 'user.qzone.qq.com' || u.search || u.hash)
                return '';
            const m = u.pathname.match(/^\/([1-9][0-9]{4,11})(?:\/main|\/)?$/);
            return m ? m[1] : '';
        } catch { return ''; }
    }).filter(Boolean))];
    return {
        url: location.href, ready: document.readyState, viewers,
        normal_profile: !!document.querySelector('#top_head_title') &&
            !!document.querySelector('#tb_logout'),
        permission_panels: [...document.querySelectorAll('.page > .page_main > .main_content.main_login')]
            .filter(visible).slice(0, 2).map(panel => ({
                tips: panel.querySelector(':scope > p.tips')?.textContent.trim().slice(0, 500) || '',
                apply_link: [...panel.querySelectorAll('.access_option a[data-cmd="apply_request"]')]
                    .some(a => visible(a) && a.textContent.trim() === '申请访问')
            })),
        panels: [...document.querySelectorAll('.page > .page_main > .error_content')]
            .filter(visible).slice(0, 11).map(panel => ({
                paragraphs: [...panel.children].filter(e => e.tagName === 'P')
                    .map(e => e.textContent.trim().slice(0, 500)),
                report_icons: panel.querySelectorAll('.report_img').length
            }))
    };
}"""


class Browser:
    """All methods must run in the one desktop worker thread."""

    def __init__(self, profile: Path) -> None:
        self.profile = profile
        self._runtime: Any = None
        self._context: Any = None
        self._page: Any = None

    def open(self) -> None:
        if self._context is not None:
            return
        try:
            api = importlib.import_module("playwright.sync_api")
        except ImportError:
            raise InspectionError("巡检运行环境未安装，请使用桌面巡检入口。") from None
        try:
            self.profile.mkdir(parents=True, exist_ok=True)
            self._runtime = api.sync_playwright().start()
            self._context = self._runtime.chromium.launch_persistent_context(
                str(self.profile),
                channel="msedge",
                headless=False,
                chromium_sandbox=True,
                accept_downloads=False,
                timeout=20000,
            )
            self._context.set_default_timeout(15000)
            self._context.set_default_navigation_timeout(20000)
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self._page.goto("https://i.qq.com/", wait_until="domcontentloaded")
        except Exception:
            self.close()
            raise InspectionError(
                "专用浏览器未能打开。请确认已安装 Microsoft Edge，再重试。"
            ) from None

    def viewer(self) -> str:
        if self._page is None:
            raise InspectionError("请先打开专用浏览器并登录 QQ 空间。")
        try:
            raw = self._page.evaluate(SNAPSHOT_SCRIPT)
            if isinstance(raw, dict) and _platform_block_url(raw.get("url")):
                raise PlatformAccessBlocked(REASONS["platform_access_blocked"])
            viewers = raw.get("viewers")
            if (
                raw.get("ready") != "complete"
                or not isinstance(viewers, list)
                or len(viewers) != 1
                or not isinstance(viewers[0], str)
                or not re.fullmatch(r"[1-9][0-9]{4,11}", viewers[0])
            ):
                raise ValueError
            parsed = urlsplit(raw["url"])
            if parsed.scheme != "https" or parsed.netloc != "user.qzone.qq.com":
                raise ValueError
            return viewers[0]
        except PlatformAccessBlocked:
            raise
        except Exception:
            raise InspectionError(
                "尚未确认登录。请在专用浏览器完成登录，打开自己的空间后重试。"
            ) from None

    def inspect(self, qq: str, viewer_qq: str) -> Observation:
        qq = numeric_id(qq)
        if self._page is None:
            raise InspectionError("专用浏览器未打开。")
        try:
            self._page.goto(f"https://user.qzone.qq.com/{qq}", wait_until="load")
            return classify_page(self._page.evaluate(SNAPSHOT_SCRIPT), qq, viewer_qq)
        except Exception:
            result = classify_page({}, qq, viewer_qq)
            result.evidence["notice_source"] = "navigation_failure"
            return Observation(qq, BLOCKED, "navigation_failed", result.checked_at, result.evidence)

    def close(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
                self._context = self._page = None
            if self._runtime is not None:
                self._runtime.stop()
                self._runtime = None
        except Exception:
            raise InspectionError("专用浏览器尚未关闭，请手动关闭后重试退出。") from None
