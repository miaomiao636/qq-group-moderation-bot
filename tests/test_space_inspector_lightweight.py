"""Isolated Edge/localhost check; no QQ request or real browser profile is used.

Can also run with the optional desktop runtime using ``python -m unittest``.
The normal suite skips this when inspection dependencies or Edge are absent.
"""

from __future__ import annotations

import contextlib
import importlib.util
import threading
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from app.space_inspector.browser import ASSET_PATTERNS, SNAPSHOT_SCRIPT, Browser, classify_page
from app.space_inspector.contracts import NOTICE, RESTRICTED, UNCONFIRMED


@unittest.skipUnless(importlib.util.find_spec("playwright"), "optional inspection runtime absent")
class LightweightBrowserTest(unittest.TestCase):
    def test_stable_profile_is_read_while_an_image_keeps_document_interactive(self):
        from playwright.sync_api import Error, sync_playwright

        release = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.path == "/pending.png":
                    release.wait(15)
                    body, kind = b"fixture", "image/png"
                else:
                    body = (
                        '<div class="page"><div class="page_top">'
                        '<a href="https://user.qzone.qq.com/98765432">合成查看账号</a>'
                        '</div><div class="page_main"><h1 id="top_head_title">合成资料</h1>'
                        '<a id="tb_logout">退出</a></div></div><img src="/pending.png">'
                    ).encode()
                    kind = "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                with contextlib.suppress(OSError):
                    self.wfile.write(body)

        with sync_playwright() as runtime:
            try:
                engine = runtime.chromium.launch(channel="msedge", headless=True)
            except Error:
                self.skipTest("local Edge not installed")
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            context = engine.new_context()
            page = context.new_page()
            try:
                origin = f"http://127.0.0.1:{server.server_port}"
                page.goto(origin, wait_until="domcontentloaded")

                class FixturePage:
                    def evaluate(self, script):
                        raw = page.evaluate(script)
                        raw["url"] = "https://user.qzone.qq.com/12345678"
                        return raw

                browser = Browser(Path("unused-synthetic-profile"))
                browser._page = FixturePage()
                browser._stop = threading.Event()
                result = browser._read_after_navigation("12345678", "98765432", False, lambda: 1)
                self.assertEqual(result.status, UNCONFIRMED)
                self.assertEqual(result.evidence["ready_state"], "interactive")
                self.assertEqual(result.evidence["notice_source"], "qzone_profile_stable")
                self.assertGreaterEqual(
                    result.evidence["load_diagnostics"]["stable_profile_ms"], 500
                )
                self.assertEqual(page.evaluate("document.readyState"), "interactive")
            finally:
                release.set()
                context.close()
                engine.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_complete_document_waits_for_asynchronously_inserted_profile(self):
        from playwright.sync_api import Error, sync_playwright

        with sync_playwright() as runtime:
            try:
                engine = runtime.chromium.launch(channel="msedge", headless=True)
            except Error:
                self.skipTest("local Edge not installed")
            context = engine.new_context()
            # Entirely synthetic about:blank document; no QQ/network request.
            context.route("**/*", lambda route: route.abort())
            page = context.new_page()
            try:
                page.set_content(
                    '<div class="page"><div class="page_top">'
                    '<a href="https://user.qzone.qq.com/98765432">合成查看账号</a>'
                    '</div><div class="page_main"></div></div>'
                )
                snapshots = []

                class FixturePage:
                    def evaluate(self, script):
                        raw = page.evaluate(script)
                        snapshots.append(raw)
                        # Map only the fixture URL, leaving the real DOM extraction intact.
                        raw["url"] = "https://user.qzone.qq.com/12345678"
                        if len(snapshots) == 1:
                            page.evaluate(
                                """() => setTimeout(() => {
                                    document.querySelector('.page_main').innerHTML =
                                        '<h1 id="top_head_title">合成空间</h1>' +
                                        '<a id="tb_logout">退出</a>';
                                }, 20)"""
                            )
                        return raw

                browser = Browser(Path("unused-synthetic-profile"))
                browser._page = FixturePage()
                browser._stop = threading.Event()
                observation = browser._read_after_navigation(
                    "12345678", "98765432", False, lambda: True
                )
                self.assertEqual(snapshots[0]["ready"], "complete")
                self.assertFalse(snapshots[0]["normal_profile"])
                self.assertTrue(snapshots[-1]["normal_profile"])
                self.assertEqual(observation.status, UNCONFIRMED)
                self.assertEqual(observation.reason, "no_restriction_notice_observed")
            finally:
                context.close()
                engine.close()

    def test_assets_blocked_but_styles_scripts_cached_and_evidence_unchanged(self):
        from playwright.sync_api import Error, sync_playwright

        counts: Counter[str] = Counter()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                counts[self.path] += 1
                if self.path == "/main.css":
                    body, kind = b".report_img {display:block;width:30px;height:30px}", "text/css"
                elif self.path == "/main.js":
                    body, kind = b"window.syntheticScriptLoaded = true", "text/javascript"
                elif self.path.startswith("/qzone_v6/image.png"):
                    body, kind = b"synthetic image", "image/png"
                else:
                    panel = (
                        '<div class="error_content"><i class="report_img"></i>'
                        f"<p>温馨提示:</p><p>{NOTICE}</p><p>返回我的空间</p></div>"
                        if self.path == "/restricted"
                        else '<div class="main_content main_login">'
                        '<p class="tips">主人设置了权限，您可通过以下方式访问</p>'
                        '<div class="access_option"><a data-cmd="apply_request">申请访问</a>'
                        "</div></div>"
                    )
                    body = (
                        '<link rel="stylesheet" href="/main.css"><script src="/main.js"></script>'
                        '<div class="page"><div class="page_top">'
                        '<a href="https://user.qzone.qq.com/98765432">合成查看账号</a></div>'
                        f'<div class="page_main">{panel}</div></div>'
                        '<img src="/qzone_v6/image.png"><img src="/qzone_v6/image.png?variant=2">'
                    ).encode()
                    kind = "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                self.wfile.write(body)

        with sync_playwright() as runtime:
            try:
                engine = runtime.chromium.launch(channel="msedge", headless=True)
            except Error:
                self.skipTest("local Edge not installed")
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            context = engine.new_context()
            browser = Browser(Path("unused-synthetic-profile"))
            browser._context = context
            browser._page = context.new_page()
            origin = f"http://127.0.0.1:{server.server_port}"
            try:
                fixture_patterns = [
                    pattern.replace("http://qzonestyle.gtimg.cn", origin)
                    for pattern in ASSET_PATTERNS
                ]
                with patch("app.space_inspector.browser.ASSET_PATTERNS", fixture_patterns):
                    browser.configure_scan(threading.Event(), True)
                self.assertEqual(browser.loading_warning, "")
                for path, status in (("/restricted", RESTRICTED), ("/permission", UNCONFIRMED)):
                    browser._page.goto(origin + path, wait_until="load")
                    raw = browser._page.evaluate(SNAPSHOT_SCRIPT)
                    # Map the fixture's localhost address to the synthetic target identity.
                    raw["url"] = "https://user.qzone.qq.com/12345678"
                    self.assertEqual(classify_page(raw, "12345678", "98765432").status, status)
                    self.assertTrue(browser._page.evaluate("window.syntheticScriptLoaded"))
                self.assertEqual(counts["/main.css"], 1)
                self.assertEqual(counts["/main.js"], 1)
                self.assertEqual(counts["/qzone_v6/image.png"], 0)
                self.assertEqual(counts["/qzone_v6/image.png?variant=2"], 0)
                self.assertEqual(
                    browser._page.evaluate("fetch('/api?avatar=x.png').then(r => r.status)"), 200
                )
                self.assertEqual(counts["/api?avatar=x.png"], 1)
                browser.finish_scan()
                browser._page.goto(origin + "/restored", wait_until="load")
                self.assertEqual(counts["/qzone_v6/image.png"], 1)
                self.assertEqual(counts["/qzone_v6/image.png?variant=2"], 1)
                self.assertEqual(counts["/main.css"], 1)
                self.assertEqual(counts["/main.js"], 1)
            finally:
                browser.finish_scan()
                context.close()
                engine.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
