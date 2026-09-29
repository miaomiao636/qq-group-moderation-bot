"""Internal regressions for stable profiles and slow resources; synthetic data only."""

import csv
import json
import queue
import threading
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from app.space_inspector import browser as module
from app.space_inspector.browser import Browser, classify_page
from app.space_inspector.cache import ObservationCache
from app.space_inspector.contracts import BLOCKED, UNCONFIRMED, Group, InspectionError
from app.space_inspector.store import Store

QQ, VIEWER = "12345678", "98765432"


def profile(**changes):
    return dict(
        url=f"https://user.qzone.qq.com/{QQ}",
        ready="interactive",
        viewers=[VIEWER],
        normal_profile=True,
        panels=[],
        permission_panels=[],
        profile_markers={"title": True, "logout": True},
        **changes,
    )


def reader(monkeypatch, frames, *, stop_at=None):
    clock = SimpleNamespace(now=0.0, reads=0, visits=0, handlers={}, generation=0)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    frame = object()

    def navigate(*args, **kwargs):
        clock.visits += 1
        clock.handlers["framenavigated"](frame)

    def evaluate(script):
        entry = frames[min(clock.reads, len(frames) - 1)]
        clock.reads += 1
        if callable(entry):
            entry = entry(clock, frame)
        if isinstance(entry, Exception):
            raise entry
        return deepcopy(entry)

    def wait(seconds):
        clock.now += seconds
        return stop_at is not None and clock.now >= stop_at

    browser = Browser.__new__(Browser)
    browser._stop = SimpleNamespace(
        is_set=lambda: stop_at is not None and clock.now >= stop_at, wait=wait
    )
    browser._page = SimpleNamespace(
        goto=navigate,
        evaluate=evaluate,
        main_frame=frame,
        on=lambda n, h: clock.handlers.update({n: h}),
        remove_listener=lambda n, h: clock.handlers.pop(n),
    )
    return browser, clock


def store_at(folder):
    store = Store(folder, create=True)
    store.bind_source("23456789")
    store.bind_viewer(VIEWER)
    store.add_snapshot(Group("34567890", "合成群", 1), [QQ])
    store.seal_snapshots()
    return store


def test_stable_profile_does_not_wait_for_unrelated_resources(monkeypatch):
    browser, clock = reader(monkeypatch, [profile()])
    result = browser.inspect(QQ, VIEWER)
    assert result.status == UNCONFIRMED
    assert result.evidence["ready_state"] == "interactive"
    assert result.evidence["notice_source"] == "qzone_profile_stable"
    assert result.evidence["load_diagnostics"]["stable_profile_ms"] >= 500
    assert 0.5 <= clock.now < 2 and clock.visits == 1


def test_late_complete_structure_after_old_deadline_is_read(monkeypatch):
    empty = profile() | {
        "ready": "complete",
        "normal_profile": False,
        "profile_markers": {"title": True, "logout": False},
    }
    browser, clock = reader(monkeypatch, [empty] * 40 + [profile() | {"ready": "complete"}])
    result = browser.inspect(QQ, VIEWER)
    assert result.status == UNCONFIRMED and clock.now == 10
    assert clock.visits == 1


def test_transient_context_loss_is_re_read_without_navigation(monkeypatch):
    error = RuntimeError(
        "Page.evaluate: Execution context was destroyed, most likely because of a navigation"
    )
    browser, clock = reader(monkeypatch, [profile(), error, profile()])
    result = browser.inspect(QQ, VIEWER)
    assert result.status == UNCONFIRMED and clock.visits == 1
    assert clock.now >= 0.75
    assert result.evidence["load_diagnostics"]["context_retries"] == 1


def test_new_document_resets_profile_stability(monkeypatch):
    def navigation(clock, frame):
        clock.handlers["framenavigated"](frame)
        return profile()

    browser, clock = reader(monkeypatch, [profile(), navigation, profile()])
    result = browser.inspect(QQ, VIEWER)
    assert result.status == UNCONFIRMED and clock.now >= 0.75


@pytest.mark.parametrize(
    "change",
    [
        {"ready": "loading"},
        {"normal_profile": False},
        {"panels": [{"paragraphs": ["需要安全验证"], "report_icons": 1}]},
        {"permission_panels": [{"tips": "需要安全验证"}]},
        {"profile_markers": {"title": True, "logout": False}},
    ],
)
def test_incomplete_or_unknown_templates_never_become_stable_profiles(monkeypatch, change):
    browser, clock = reader(monkeypatch, [profile() | change])
    result = browser.inspect(QQ, VIEWER)
    assert result.status == BLOCKED and clock.visits == 1 and clock.now <= 30


@pytest.mark.parametrize(
    "raw,reason",
    [
        (profile() | {"url": "https://waf.tencent.com/501page.html"}, "platform_access_blocked"),
        (profile() | {"url": "https://i.qq.com/"}, "login_required"),
        (profile() | {"viewers": ["88765432"]}, "viewer_missing_or_changed"),
        (profile() | {"url": "https://user.qzone.qq.com/88765432"}, "unexpected_location"),
    ],
)
def test_access_barrier_during_stability_wait_stops_immediately(monkeypatch, raw, reason):
    browser, clock = reader(monkeypatch, [profile(), raw])
    result = browser.inspect(QQ, VIEWER)
    assert result.status == BLOCKED and result.reason == reason
    assert clock.reads == 2 and clock.now == 0.25


def test_cancel_and_permanent_browser_failure_do_not_complete_profile(monkeypatch):
    browser, clock = reader(monkeypatch, [profile()], stop_at=0.25)
    assert browser.inspect(QQ, VIEWER).status == BLOCKED
    assert clock.now == 0.25
    browser, clock = reader(monkeypatch, [profile(), RuntimeError("Target page has been closed")])
    assert browser.inspect(QQ, VIEWER).reason == "navigation_failed"
    assert clock.reads == 2


def test_profile_result_survives_reload_cache_and_export(tmp_path, monkeypatch):
    browser, _ = reader(monkeypatch, [profile()])
    result = browser.inspect(QQ, VIEWER)
    folder = tmp_path / "20260929T010000Z-12345678"
    store = store_at(folder)
    cache = ObservationCache(tmp_path / "cache.sqlite3")
    try:
        store.save(result)
        cache.remember(store)
        store.close()
        store = Store(folder)
        assert store.summary()["checked"] == 1 and store.summary()["restricted"] == 0
        assert store.rows()[0]["evidence"]["ready_state"] == "interactive"
        exported = store.export()
        report = json.loads((exported / "report.json").read_text(encoding="utf-8"))
        assert report["rows"][0]["evidence"]["ready_state"] == "interactive"
        assert report["rows"][0]["evidence"]["notice_source"] == "qzone_profile_stable"
        with (exported / "restricted.csv").open(encoding="utf-8-sig", newline="") as handle:
            assert list(csv.DictReader(handle)) == []
        other = store_at(tmp_path / "20260929T010000Z-87654321")
        try:
            reused = cache.lookup(other, QQ, 24)
            assert reused is not None
            other.save(reused)
            assert other.summary()["checked"] == 1
        finally:
            other.close()
    finally:
        cache.close()
        store.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"ready_state": "complete"},
        {"notice_source": "qzone_profile"},
        {"panel_count": 1},
        {"load_diagnostics": {}},
        {
            "load_diagnostics": {
                "reads": 1,
                "elapsed_ms": 0,
                "context_retries": 0,
                "profile_title": True,
                "profile_logout": True,
                "stable_profile_ms": 0,
            }
        },
    ],
)
def test_stable_evidence_cannot_be_saved_with_missing_proof(tmp_path, monkeypatch, changes):
    browser, _ = reader(monkeypatch, [profile()])
    result = browser.inspect(QQ, VIEWER)
    assert result.status == UNCONFIRMED
    result.evidence.update(changes)
    store = store_at(tmp_path / "task")
    try:
        with pytest.raises(InspectionError):
            store.save(result)
        assert store.summary()["checked"] == 0
    finally:
        store.close()


def test_one_interactive_snapshot_is_still_insufficient():
    assert classify_page(profile(), QQ, VIEWER).status == BLOCKED


def test_confirm_viewer_can_read_a_stable_profile_with_resources_pending(monkeypatch):
    browser, _ = reader(monkeypatch, [profile()])
    assert browser.viewer() == VIEWER


def test_context_retry_budget_is_not_reset_after_a_successful_read(monkeypatch):
    error = RuntimeError(
        "Page.evaluate: Execution context was destroyed, most likely because of a navigation"
    )
    browser, clock = reader(monkeypatch, [profile(), error, profile(), error, profile(), error])
    result = browser.inspect(QQ, VIEWER)
    assert result.status == BLOCKED and result.reason == "navigation_failed"
    assert clock.reads == 6 and clock.visits == 1
    assert result.evidence["load_diagnostics"]["context_retries"] == 2


def test_lost_profile_marker_resets_stability(monkeypatch):
    incomplete = profile() | {
        "normal_profile": False,
        "profile_markers": {"title": True, "logout": False},
    }
    browser, clock = reader(monkeypatch, [profile(), incomplete, profile()])
    result = browser.inspect(QQ, VIEWER)
    assert result.status == UNCONFIRMED and clock.now >= 1


@pytest.mark.parametrize("change", ["status", "reason", "old_contract", "extra_diagnostic"])
def test_stable_source_has_strict_status_reason_and_version(tmp_path, monkeypatch, change):
    browser, _ = reader(monkeypatch, [profile()])
    result = browser.inspect(QQ, VIEWER)
    if change == "status":
        result = replace(result, status=BLOCKED)
    elif change == "reason":
        result = replace(result, reason="page_incomplete")
    elif change == "old_contract":
        result.evidence["reuse"] = {
            "source_task": "20260929T010000Z-12345678",
            "source_visit": 1,
            "reused_at": datetime.now(UTC).isoformat(),
            "contract": "qzone-dom-v1",
        }
    else:
        result.evidence["load_diagnostics"]["body_text"] = "UNTRUSTED_CONTENT"
    store = store_at(tmp_path / "task")
    try:
        with pytest.raises(InspectionError):
            store.save(result)
    finally:
        store.close()


def test_stable_result_can_be_backed_up_and_restored(tmp_path, monkeypatch):
    from app.space_inspector.backup import create_backup, restore_backup

    browser, _ = reader(monkeypatch, [profile()])
    root = tmp_path / "inspector"
    name = "20260929T010000Z-12345678"
    store = store_at(root / "tasks" / name)
    store.save(browser.inspect(QQ, VIEWER))
    exports = tmp_path / "exports"
    exports.mkdir()
    store.export(exports)
    store.close()
    create_backup(root, tmp_path / "backup", [exports], reserve=0)
    restore_backup(tmp_path / "backup", tmp_path / "restored", reserve=0)
    resumed = Store(tmp_path / "restored/inspector/tasks" / name)
    try:
        assert resumed.summary()["checked"] == 1
        assert resumed.rows()[0]["evidence"]["notice_source"] == "qzone_profile_stable"
    finally:
        resumed.close()


def test_worker_binds_stop_before_initial_viewer_wait(monkeypatch):
    from app.space_inspector.service import Service
    from app.space_inspector.worker import run_worker

    from tests.test_space_inspector_gui import FakeService, drain

    stop = threading.Event()

    def cancelling_read(clock, frame):
        if clock.reads >= 2:
            stop.set()
        return profile()

    browser, clock = reader(monkeypatch, [cancelling_read])

    class PendingService(FakeService):
        bind_stop = Service.bind_stop
        viewer = Service.viewer

    service = PendingService([])
    service._browser = browser
    commands, events = queue.Queue(), queue.Queue()
    commands.put(("viewer", None))
    commands.put(("close", None))
    run_worker(commands, events, stop, lambda: service)
    assert browser._stop is stop
    assert clock.reads == 2
    assert any(kind == "error" for kind, _ in drain(events))


def test_new_login_confirmation_resets_previous_pause_in_ui():
    pytest.importorskip("tkinter")
    from app.space_inspector.gui import Window

    window = Window.__new__(Window)
    window._busy = window._closing = window._shutdown_failed = False
    window._stop = threading.Event()
    window._stop.set()
    window._commands = queue.Queue()
    window._status = SimpleNamespace(set=lambda value: None)
    window._controls = lambda: None
    window._submit("viewer")
    assert not window._stop.is_set()
    assert window._commands.get_nowait() == ("viewer", None)
