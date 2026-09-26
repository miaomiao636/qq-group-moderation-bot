"""Isolated Edge/localhost check; no QQ request or real browser profile is used.

Can also run with the optional desktop runtime using ``python -m unittest``.
The normal suite skips this when inspection dependencies or Edge are absent.
"""

from __future__ import annotations

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
