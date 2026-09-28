"""Internal delayed-DOM regressions; no QQ requests or real browser session."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from app.space_inspector import browser as module
from app.space_inspector.browser import Browser
from app.space_inspector.contracts import (
    BLOCKED,
    NOTICE,
    RESTRICTED,
    UNCONFIRMED,
    Group,
    MemberPageUnrecognized,
)
from app.space_inspector.service import run_scan
from app.space_inspector.store import Store

QQ, VIEWER = "12345678", "98765432"


def pending_page():
    return {
        "url": f"https://user.qzone.qq.com/{QQ}",
        "ready": "complete",
        "viewers": [VIEWER],
        "normal_profile": False,
        "panels": [],
    }


def reader(monkeypatch, frames, *, stop_at=None, failure=False, committed=True):
    clock = SimpleNamespace(now=0.0, reads=0, visits=0, waits=[])
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    handlers, frame = {}, object()

    def navigate(*args, **kwargs):
        clock.visits += 1
        if committed:
            handlers["framenavigated"](frame)
        if failure:
            raise TimeoutError("synthetic navigation timeout")

    def evaluate(script):
        raw = frames[min(clock.reads, len(frames) - 1)]
        clock.reads += 1
        return raw

    def stopped():
        return stop_at is not None and clock.now >= stop_at

    def wait(seconds):
        clock.waits.append(seconds)
        clock.now += seconds
        return stopped()

    browser = Browser.__new__(Browser)
    browser._stop = SimpleNamespace(is_set=stopped, wait=wait)
    browser._page = SimpleNamespace(
        goto=navigate,
        evaluate=evaluate,
        main_frame=frame,
        on=lambda name, handler: handlers.update({name: handler}),
        remove_listener=lambda name, handler: handlers.pop(name),
    )
    return browser, clock


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("kind", ["profile", "restriction", "permission"])
def test_complete_document_waits_for_late_page_structure(monkeypatch, kind, failure):
    ready = pending_page()
    if kind == "profile":
        ready["normal_profile"] = True
    elif kind == "restriction":
        ready["panels"] = [{"paragraphs": ["温馨提示:", NOTICE, "返回我的空间"], "report_icons": 1}]
    else:
        ready["permission_panels"] = [
            {"tips": "主人设置了权限，您可通过以下方式访问", "apply_link": True}
        ]
    browser, clock = reader(monkeypatch, [pending_page(), ready], failure=failure)
    observation = browser.inspect(QQ, VIEWER)
    assert observation.status == (RESTRICTED if kind == "restriction" else UNCONFIRMED)
    assert observation.evidence["page_url"] == ready["url"]
    assert observation.evidence["viewer_qq"] == VIEWER
    assert clock.reads == 2 and clock.visits == 1
    assert clock.waits == [0.25]


def test_loading_and_unknown_structure_share_one_deadline(monkeypatch):
    loading = pending_page() | {"ready": "interactive"}
    browser, clock = reader(monkeypatch, [loading] * 20 + [pending_page()])
    observation = browser.inspect(QQ, VIEWER)
    assert observation.status == BLOCKED and observation.reason == "unrecognized_page"
    # SPACE-STABLE-20260929 raises the fixed read budget to 30 seconds;
    # loading -> complete must still not restart it.
    assert clock.now == 30 and clock.reads == 121 and clock.visits == 1
    assert observation.evidence["notice"] == ""


@pytest.mark.parametrize("stop_at", [0, 0.25])
def test_pause_interrupts_unknown_structure_wait(monkeypatch, stop_at):
    browser, clock = reader(monkeypatch, [pending_page()], stop_at=stop_at)
    observation = browser.inspect(QQ, VIEWER)
    assert observation.status == BLOCKED and observation.reason == "unrecognized_page"
    assert clock.now == stop_at and clock.reads == 1 and clock.visits == 1


@pytest.mark.parametrize(
    "raw,reason",
    [
        ({"url": "https://waf.tencent.com/501page.html"}, "platform_access_blocked"),
        ({"url": "https://i.qq.com/"}, "login_required"),
        (pending_page() | {"viewers": ["88765432"]}, "viewer_missing_or_changed"),
        (pending_page() | {"viewers": []}, "viewer_missing_or_changed"),
        (pending_page() | {"url": "https://user.qzone.qq.com/88765432"}, "unexpected_location"),
    ],
)
@pytest.mark.parametrize("after_pending", [False, True])
def test_access_barriers_stop_without_waiting_for_another_snapshot(
    monkeypatch, raw, reason, after_pending
):
    frames = ([pending_page()] if after_pending else []) + [raw]
    browser, clock = reader(monkeypatch, frames)
    observation = browser.inspect(QQ, VIEWER)
    assert observation.status == BLOCKED and observation.reason == reason
    assert clock.reads == len(frames) and clock.visits == 1
    assert clock.now == (0.25 if after_pending else 0)


def test_invalid_snapshot_does_not_wait_as_identified_member_page(monkeypatch):
    browser, clock = reader(monkeypatch, [{}])
    observation = browser.inspect(QQ, VIEWER)
    assert observation.status == BLOCKED and observation.reason == "unrecognized_page"
    assert clock.reads == 1 and clock.waits == []


def test_failed_navigation_does_not_wait_on_old_same_member_document(monkeypatch):
    browser, clock = reader(monkeypatch, [pending_page()], failure=True, committed=False)
    observation = browser.inspect(QQ, VIEWER)
    assert observation.status == BLOCKED and observation.reason == "navigation_failed"
    assert clock.reads == 1 and clock.waits == [] and clock.visits == 1


@pytest.mark.parametrize("becomes_ready", [True, False])
def test_scan_saves_only_final_observation_and_retains_unknown_member(
    tmp_path, monkeypatch, becomes_ready
):
    frames = [pending_page()]
    if becomes_ready:
        frames.append(pending_page() | {"normal_profile": True})
    browser, clock = reader(monkeypatch, frames)
    store = Store(tmp_path / "task", create=True)
    try:
        store.bind_source("23456789")
        store.bind_viewer(VIEWER)
        store.add_snapshot(Group("34567890", "合成群", 1), [QQ])
        store.seal_snapshots()
        if becomes_ready:
            run_scan(store, browser, VIEWER, threading.Event(), lambda _: None, delay=0)
            assert store.pending() == [] and store.summary()["checked"] == 1
        else:
            with pytest.raises(MemberPageUnrecognized):
                run_scan(store, browser, VIEWER, threading.Event(), lambda _: None, delay=0)
            assert store.pending() == [QQ] and store.summary()["checked"] == 0
        assert store.summary()["restricted"] == 0
        assert store.db.execute("SELECT COUNT(*) FROM visits").fetchone()[0] == 1
        assert clock.visits == 1
    finally:
        store.close()
