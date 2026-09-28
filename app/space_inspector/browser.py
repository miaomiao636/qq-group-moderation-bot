"""A dedicated browser owned by the desktop worker; no borrowed browser credentials."""

from __future__ import annotations

import importlib
import re
import threading
import time
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

ASSET_PATTERNS = [
    f"{scheme}://qzonestyle.gtimg.cn/{folder}/*.{extension}{suffix}"
    for scheme in ("http", "https")
    for folder in ("qzone_v6", "aoi")
    for extension in ("png", "jpg", "jpeg", "gif", "webp", "woff", "woff2", "ttf")
    for suffix in ("", "?*")
] + [f"{scheme}://qlogo3.store.qq.com/qzone/*" for scheme in ("http", "https")]


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
    viewers = raw.get("viewers")
    if viewers == [viewer_qq]:
        evidence["viewer_qq"] = viewer_qq
    elif isinstance(viewers, list) and viewers:
        return result(BLOCKED, "viewer_missing_or_changed")
    if ready != "complete":
        return result(BLOCKED, "page_incomplete")
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
    const profile_markers = {
        title: !!document.querySelector('#top_head_title'),
        logout: !!document.querySelector('#tb_logout')
    };
    return {
        url: location.href, ready: document.readyState, viewers,
        profile_markers,
        normal_profile: profile_markers.title && profile_markers.logout,
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
                not isinstance(viewers, list)
                or len(viewers) != 1
                or not isinstance(viewers[0], str)
                or not re.fullmatch(r"[1-9][0-9]{4,11}", viewers[0])
            ):
                raise ValueError
            parsed = urlsplit(raw["url"])
            if parsed.scheme != "https" or parsed.netloc != "user.qzone.qq.com":
                raise ValueError
            if raw.get("ready") != "complete":
                target = re.fullmatch(r"/([1-9][0-9]{4,11})(?:/main|/)?", parsed.path)
                if (
                    raw.get("ready") != "interactive"
                    or not target
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError
                generation = 0

                def navigated(frame: Any) -> None:
                    nonlocal generation
                    if frame == self._page.main_frame:
                        generation += 1

                self._page.on("framenavigated", navigated)
                try:
                    observation = self._read_after_navigation(
                        target[1], viewers[0], False, lambda: generation
                    )
                finally:
                    self._page.remove_listener("framenavigated", navigated)
                if observation.reason == "platform_access_blocked":
                    raise PlatformAccessBlocked(REASONS["platform_access_blocked"])
                if observation.status == BLOCKED or observation.evidence["viewer_qq"] != viewers[0]:
                    raise ValueError
            return viewers[0]
        except PlatformAccessBlocked:
            raise
        except Exception:
            raise InspectionError(
                "尚未确认登录。请在专用浏览器完成登录，打开自己的空间后重试。"
            ) from None

    def bind_stop(self, stop: threading.Event) -> None:
        """Share cancellation before login confirmation, not just during scanning."""
        self._stop = stop

    def configure_scan(self, stop: threading.Event, lightweight: bool) -> None:
        self.bind_stop(stop)
        if self._context is None:
            raise InspectionError("请先打开专用浏览器。")
        self.finish_scan()
        self.loading_warning = ""
        if lightweight:
            try:
                self._scan_session = self._context.new_cdp_session(self._page)
                self._scan_session.send("Network.enable")
                # Unlike Playwright route(), this does not disable the HTTP cache.
                # Limit filtering to observed static/portrait namespaces, not arbitrary
                # URLs whose API query parameters happen to end in an image extension.
                self._scan_session.send("Network.setBlockedURLs", {"urls": ASSET_PATTERNS})
            except Exception:
                self.finish_scan()
                self.loading_warning = "此浏览器不支持轻量加载，已使用普通加载。"

    def finish_scan(self) -> None:
        session = getattr(self, "_scan_session", None)
        if session is not None:
            cleared = False
            try:
                session.send("Network.setBlockedURLs", {"urls": []})
                cleared = True
            except Exception:
                pass
            try:
                session.detach()
            except Exception:
                if not cleared:
                    raise InspectionError("浏览器资源设置未能恢复，请关闭并重开巡检。") from None
            self._scan_session = None

    def inspect(self, qq: str, viewer_qq: str) -> Observation:
        qq = numeric_id(qq)
        if self._page is None:
            raise InspectionError("专用浏览器未打开。")
        committed = 0

        def navigated(frame: Any) -> None:
            nonlocal committed
            if frame == self._page.main_frame:
                committed += 1

        self._page.on("framenavigated", navigated)
        try:
            navigation_failed = False
            try:
                self._page.goto(f"https://user.qzone.qq.com/{qq}", wait_until="domcontentloaded")
            except Exception:
                navigation_failed = True
            return self._read_after_navigation(qq, viewer_qq, navigation_failed, lambda: committed)
        finally:
            self._page.remove_listener("framenavigated", navigated)

    def _read_after_navigation(
        self, qq: str, viewer_qq: str, navigation_failed: bool, committed: Any
    ) -> Observation:
        # A load timeout can leave a readable WAF, login or completed member page.
        # Inspect that same document; never automatically navigate again after failure.
        started = time.monotonic()
        deadline = started + 30
        signal = getattr(self, "_stop", threading.Event())
        last: Observation | None = None
        stable_since: float | None = None
        document = committed()
        reads = context_retries = 0
        markers: dict[str, bool] = {"title": False, "logout": False}

        def finish(observation: Observation) -> Observation:
            now = time.monotonic()
            observation.evidence["load_diagnostics"] = {
                "reads": reads,
                "elapsed_ms": max(0, int((now - started) * 1000)),
                "context_retries": context_retries,
                "profile_title": markers["title"],
                "profile_logout": markers["logout"],
                "stable_profile_ms": 0
                if stable_since is None
                else max(0, int((now - stable_since) * 1000)),
            }
            return observation

        while True:
            try:
                reads += 1
                raw = self._page.evaluate(SNAPSHOT_SCRIPT)
                last = classify_page(raw, qq, viewer_qq)
            except Exception as exc:
                # Context replacement can interrupt evaluate during a legitimate
                # redirect. No old snapshot survives it; closed pages never retry.
                stable_since = None
                last = None
                markers = {"title": False, "logout": False}
                message = str(exc)
                if (
                    message.startswith("Page.evaluate: Execution context was destroyed")
                    and context_retries < 2
                    and not signal.is_set()
                    and time.monotonic() < deadline
                ):
                    context_retries += 1
                    if not signal.wait(0.25):
                        continue
                break
            if committed() != document:
                document = committed()
                stable_since = None
            raw_markers = raw.get("profile_markers") if isinstance(raw, dict) else None
            markers = {
                key: isinstance(raw_markers, dict) and raw_markers.get(key) is True
                for key in ("title", "logout")
            }
            if navigation_failed and not committed():
                if last.reason in {"platform_access_blocked", "login_required"}:
                    return finish(last)
                # A pre-existing same-account page is not a fresh observation.
                last = None
                break
            candidate = (
                last.reason == "page_incomplete"
                and last.evidence["ready_state"] == "interactive"
                and last.evidence["viewer_qq"] == viewer_qq
                and last.evidence["page_url"] == f"https://user.qzone.qq.com/{qq}"
                and isinstance(raw, dict)
                and raw.get("normal_profile") is True
                and all(markers.values())
                and raw.get("panels") == []
                and raw.get("permission_panels") == []
            )
            if candidate:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= 0.5 and not signal.is_set():
                    # The parsed document's profile is stable while images/frames
                    # may still load. This is not an account-health declaration.
                    last.evidence["notice_source"] = "qzone_profile_stable"
                    return finish(
                        Observation(
                            qq,
                            UNCONFIRMED,
                            "no_restriction_notice_observed",
                            last.checked_at,
                            last.evidence,
                        )
                    )
            else:
                stable_since = None
            # Qzone can finish the document before rendering its profile/system
            # template. Re-read this identified page within the same deadline;
            # never navigate again or turn an unknown template into a success.
            pending_structure = (
                last.reason == "unrecognized_page"
                and last.evidence.get("viewer_qq") == viewer_qq
                and last.evidence.get("ready_state") == "complete"
                and last.evidence.get("page_url") == f"https://user.qzone.qq.com/{qq}"
            )
            if last.reason != "page_incomplete" and not pending_structure:
                return finish(last)
            if signal.is_set() or time.monotonic() >= deadline or signal.wait(0.25):
                return finish(last)
        result = last or classify_page({}, qq, viewer_qq)
        if navigation_failed or last is None:
            result.evidence["notice_source"] = "navigation_failure"
            return finish(
                Observation(qq, BLOCKED, "navigation_failed", result.checked_at, result.evidence)
            )
        return finish(result)

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
