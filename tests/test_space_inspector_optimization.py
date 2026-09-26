"""Internal optimization regressions; synthetic pages and isolated local stores only."""

from __future__ import annotations

import csv
import json
import queue
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from app.space_inspector.browser import Browser, classify_page
from app.space_inspector.cache import ObservationCache
from app.space_inspector.contracts import (
    BLOCKED,
    RESTRICTED,
    BrowserConfirmationRequired,
    Group,
    InspectionError,
    MemberPageUnrecognized,
    PlatformAccessBlocked,
)
from app.space_inspector.options import ScanOptions
from app.space_inspector.service import Service, run_scan
from app.space_inspector.store import Store
from app.space_inspector.worker import run_worker

QQ, VIEWER = "12345678", "98765432"


def raw_page(qq=QQ):
    from app.space_inspector.contracts import NOTICE

    return {
        "url": f"https://user.qzone.qq.com/{qq}",
        "ready": "complete",
        "viewers": [VIEWER],
        "normal_profile": False,
        "panels": [{"paragraphs": ["温馨提示:", NOTICE, "返回我的空间"], "report_icons": 1}],
    }


def task(folder):
    store = Store(folder, create=True)
    store.bind_source("23456789")
    store.bind_viewer(VIEWER)
    store.add_snapshot(Group("34567890", "合成群", 3), [QQ, "22345678", "32345678"])
    store.seal_snapshots()
    return store


def fake_page(goto, evaluate, *, commits=True):
    handlers = {}
    frame = object()

    def navigate(*args, **kwargs):
        if commits:
            handlers["framenavigated"](frame)
        return goto(*args, **kwargs)

    return SimpleNamespace(
        goto=navigate,
        evaluate=evaluate,
        main_frame=frame,
        on=lambda name, handler: handlers.update({name: handler}),
        remove_listener=lambda name, handler: handlers.pop(name),
    )


@pytest.mark.parametrize(
    "raw,reason",
    [
        (raw_page(), "qzone_restriction_notice_observed"),
        ({"url": "https://waf.tencent.com/501page.html"}, "platform_access_blocked"),
        ({"url": "https://i.qq.com/"}, "login_required"),
    ],
)
def test_navigation_timeout_reads_loaded_page_instead_of_hiding_reason(raw, reason):
    browser = Browser.__new__(Browser)
    navigations = []

    def goto(*args, **kwargs):
        navigations.append(args)
        raise TimeoutError("synthetic load timeout")

    browser._page = fake_page(goto, lambda _: raw)
    assert browser.inspect(QQ, VIEWER).reason == reason
    assert len(navigations) == 1


def test_continuous_batches_keep_boundary_delay_and_fixed_queue(tmp_path):
    store = task(tmp_path / "task")
    visits, waits, progress = [], [], []
    reader = SimpleNamespace(
        inspect=lambda qq, viewer: visits.append(qq) or classify_page(raw_page(qq), qq, viewer)
    )
    stop = SimpleNamespace(is_set=lambda: False, wait=lambda seconds: waits.append(seconds))
    run_scan(
        store,
        reader,
        VIEWER,
        stop,
        progress.append,
        delay=30,
        max_checks=2,
        continuous=True,
        batch_pause=60,
    )
    assert visits == [QQ, "22345678", "32345678"]
    assert waits == [30, 60]
    assert store.summary()["pending"] == 0
    store.close()


def test_pause_during_batch_rest_does_not_start_another_batch(tmp_path):
    store = task(tmp_path / "task")
    visits, waits = [], []
    reader = SimpleNamespace(
        inspect=lambda qq, viewer: visits.append(qq) or classify_page(raw_page(qq), qq, viewer)
    )

    def wait(seconds):
        waits.append(seconds)
        return seconds == 60

    signal = SimpleNamespace(is_set=lambda: False, wait=wait)
    run_scan(
        store,
        reader,
        VIEWER,
        signal,
        lambda _: None,
        delay=30,
        max_checks=2,
        continuous=True,
        batch_pause=60,
    )
    assert visits == [QQ, "22345678"]
    assert waits == [30, 60]
    assert store.summary()["pending"] == 1
    store.close()


@pytest.mark.parametrize(
    "url,error",
    [
        ("https://waf.tencent.com/501page.html", PlatformAccessBlocked),
        ("https://i.qq.com/", BrowserConfirmationRequired),
    ],
)
def test_automatic_mode_stops_before_next_member_on_access_failure(tmp_path, url, error):
    store = task(tmp_path / "task")
    visits = []
    reader = SimpleNamespace(
        inspect=lambda qq, viewer: visits.append(qq) or classify_page({"url": url}, qq, viewer)
    )
    with pytest.raises(error):
        run_scan(
            store,
            reader,
            VIEWER,
            threading.Event(),
            lambda _: None,
            delay=0,
            max_checks=1,
            continuous=True,
            batch_pause=0,
        )
    assert visits == [QQ]
    assert store.summary()["checked"] == 0
    assert store.summary()["pending"] == 3
    store.close()


def test_unrecognized_identified_page_needs_explicit_operator_defer(tmp_path):
    store = task(tmp_path / "task")
    visits = []

    def inspect(qq, viewer):
        visits.append(qq)
        raw = raw_page(qq)
        if qq == QQ:
            raw["panels"] = []
        return classify_page(raw, qq, viewer)

    reader = SimpleNamespace(inspect=inspect)
    with pytest.raises(MemberPageUnrecognized) as raised:
        run_scan(store, reader, VIEWER, threading.Event(), lambda _: None, delay=0, continuous=True)
    assert raised.value.qq == QQ
    assert visits == [QQ]
    run_scan(
        store,
        reader,
        VIEWER,
        threading.Event(),
        lambda _: None,
        delay=0,
        continuous=True,
        defer_qq=QQ,
    )
    assert visits == [QQ, "22345678", "32345678"]
    assert store.pending() == [QQ]
    assert store.summary()["checked"] == 2
    store.close()


def test_same_page_finishes_loading_without_second_navigation():
    browser = Browser.__new__(Browser)
    navigations = []
    loading = raw_page()
    loading["ready"] = "interactive"
    frames = iter([loading, raw_page()])
    browser._stop = SimpleNamespace(is_set=lambda: False, wait=lambda _: False)
    browser._page = fake_page(lambda *a, **k: navigations.append(k), lambda _: next(frames))
    assert browser.inspect(QQ, VIEWER).status == RESTRICTED
    assert len(navigations) == 1


def test_pause_interrupts_same_page_loading_without_marking_complete():
    browser = Browser.__new__(Browser)
    raw = raw_page()
    raw["ready"] = "interactive"
    browser._stop = threading.Event()
    browser._stop.set()
    browser._page = fake_page(lambda *a, **k: None, lambda _: raw)
    assert browser.inspect(QQ, VIEWER).status == BLOCKED


def test_failed_navigation_cannot_refresh_old_same_member_page():
    browser = Browser.__new__(Browser)

    def fail(*args, **kwargs):
        raise TimeoutError("no document was committed")

    browser._page = fake_page(fail, lambda _: raw_page(), commits=False)
    result = browser.inspect(QQ, VIEWER)
    assert result.status == BLOCKED
    assert result.reason == "navigation_failed"
    assert result.evidence["notice"] == ""


def test_browser_failed_cleanup_keeps_handle_for_retry():
    browser = Browser.__new__(Browser)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic protocol failure")

    session = SimpleNamespace(send=fail, detach=fail)
    browser._scan_session = session
    with pytest.raises(InspectionError):
        browser.finish_scan()
    assert browser._scan_session is session
    session.send = lambda *args: None
    browser.finish_scan()
    assert browser._scan_session is None


def synthetic_service(tmp_path, reader):
    service = Service.__new__(Service)
    service.root = tmp_path
    service._store = task(tmp_path / "20260922T120000Z-45678901")
    service._deferred = set()
    service.source_id = "23456789"
    service._directory = SimpleNamespace(verify_identity=lambda: None)
    service._browser = SimpleNamespace(
        inspect=reader,
        loading_warning="",
        configure_scan=lambda *args: None,
        finish_scan=lambda: None,
    )
    service.viewer = lambda: VIEWER
    return service


def test_two_operator_deferred_members_do_not_return_to_front_of_queue(tmp_path):
    visits = []

    def read(qq, viewer):
        visits.append(qq)
        raw = raw_page(qq)
        if qq != "32345678":
            raw["panels"] = []
        return classify_page(raw, qq, viewer)

    service = synthetic_service(tmp_path, read)
    stop = SimpleNamespace(is_set=lambda: False, wait=lambda _: False)
    options = ScanOptions(reuse_hours=0)
    with pytest.raises(MemberPageUnrecognized):
        service.scan(stop, lambda _: None, options)
    with pytest.raises(MemberPageUnrecognized):
        service.scan(stop, lambda _: None, replace(options, defer_qq=QQ))
    service.scan(stop, lambda _: None, replace(options, defer_qq="22345678"))
    assert visits == [QQ, "22345678", "32345678"]
    assert service.summary()["deferred"] == 2
    assert service._store.summary()["checked"] == 1
    # An ordinary Continue explicitly returns to the remaining/review queue.
    with pytest.raises(MemberPageUnrecognized):
        service.scan(stop, lambda _: None, options)
    assert visits[-1] == QQ
    service._store.close()


def test_cleanup_error_does_not_hide_platform_stop(tmp_path):
    service = synthetic_service(
        tmp_path,
        lambda qq, viewer: classify_page(
            {"url": "https://waf.tencent.com/501page.html"}, qq, viewer
        ),
    )

    def cleanup():
        raise RuntimeError("synthetic cleanup error")

    service._browser.finish_scan = cleanup
    with pytest.raises(PlatformAccessBlocked):
        service.scan(threading.Event(), lambda _: None, ScanOptions())
    assert service._store.summary()["checked"] == 0
    service._store.close()


def test_partial_snapshot_can_be_viewed_and_exported_but_cannot_scan(tmp_path):
    service = Service.__new__(Service)
    service.export_root = tmp_path / "exports"
    service._store = Store(tmp_path / "partial", create=True)
    service._deferred = set()
    service._store.bind_source("23456789")
    service._store.add_snapshot(Group("34567890", "合成群", 1), [QQ])
    assert service.summary()["deferred"] == 0
    assert service.rows()[0]["qq"] == QQ
    assert (service.export() / "report.csv").is_file()
    with pytest.raises(InspectionError):
        service._store.pending()
    service._store.close()


@pytest.mark.parametrize("failure", [None, "unknown", "login"])
def test_worker_passes_options_and_exposes_only_appropriate_recovery(failure):
    commands, events, received = queue.Queue(), queue.Queue(), []
    options = ScanOptions(delay_seconds=10, batch_size=100, defer_qq=QQ)

    def scan(stop, progress, actual_options):
        received.append(actual_options)
        if failure == "unknown":
            raise MemberPageUnrecognized("synthetic unknown page", QQ)
        if failure == "login":
            raise BrowserConfirmationRequired("synthetic login redirect")

    service = SimpleNamespace(
        current_folder=None,
        create=lambda groups: received.append(groups),
        summary=lambda: {},
        rows=lambda: [],
        scan=scan,
        close=lambda: None,
    )
    commands.put(("create", {"groups": ["23456789"], "options": options}))
    commands.put(("scan", options))
    commands.put(("close", None))
    run_worker(commands, events, threading.Event(), lambda: service)
    assert received == [["23456789"], options, options]
    errors = [payload for kind, payload in list(events.queue) if kind == "error"]
    if failure is None:
        assert errors == []
    elif failure == "unknown":
        assert len(errors) == 2
        assert all(error["defer_qq"] == QQ for error in errors)
        assert all("requires_browser_confirmation" not in error for error in errors)
    else:
        assert len(errors) == 2
        assert all(error["requires_browser_confirmation"] for error in errors)
        assert all("defer_qq" not in error for error in errors)


def cache_fixture(tmp_path):
    source = task(tmp_path / "20260922T100000Z-12345678")
    observation = classify_page(raw_page(), QQ, VIEWER)
    source.save(observation)
    cache = ObservationCache(tmp_path / "cache.sqlite3")
    cache.remember(source)
    target = task(tmp_path / "20260922T110000Z-23456789")
    return source, target, cache, observation


def test_cache_hit_skips_request_and_preserves_original_evidence_in_all_exports(tmp_path):
    source, target, cache, observation = cache_fixture(tmp_path)
    visits, waits = [], []
    reader = SimpleNamespace(
        inspect=lambda qq, viewer: visits.append(qq) or classify_page(raw_page(qq), qq, viewer)
    )
    signal = SimpleNamespace(is_set=lambda: False, wait=lambda seconds: waits.append(seconds))
    run_scan(
        target,
        reader,
        VIEWER,
        signal,
        lambda _: None,
        delay=30,
        continuous=True,
        cache_lookup=lambda qq: cache.lookup(target, qq, 24),
        on_saved=lambda o: cache.remember(target, o.qq),
    )
    assert visits == ["22345678", "32345678"]
    assert waits == [30]
    assert target.reused_count() == 1
    row = target.rows()[0]
    assert row["checked_at"] == observation.checked_at
    assert row["evidence"]["reuse"]["source_task"] == source.folder.name
    output = target.export()
    for name in ("restricted.csv", "report.csv"):
        with (output / name).open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert rows[0]["观察来源"] == "历史观察"
        assert rows[0]["时间"] == observation.checked_at
        assert rows[0]["来源任务"] == source.folder.name
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["rows"][0]["evidence"]["reuse"]["source_visit"] == 1
    # A reused row never rewrites the cache's original source or freshness.
    assert (
        cache.db.execute("SELECT source_task FROM cached WHERE qq=?", (QQ,)).fetchone()[0]
        == source.folder.name
    )
    source.close()
    target.close()
    cache.close()


@pytest.mark.parametrize(
    "age,hit", [(0, True), (24 * 3600, True), (24 * 3600 + 1, False), (-1, False)]
)
def test_cache_age_uses_original_time_and_rejects_future(tmp_path, age, hit):
    source, target, cache, observation = cache_fixture(tmp_path)
    now = datetime.fromisoformat(observation.checked_at) + timedelta(seconds=age)
    assert (cache.lookup(target, QQ, 24, now=now) is not None) is hit
    source.close()
    target.close()
    cache.close()


@pytest.mark.parametrize(
    "column,value",
    [
        ("viewer", "88765432"),
        ("contract", "old-contract"),
        ("status", BLOCKED),
        ("evidence", "{}"),
        ("source_task", "../../private"),
        ("source_visit", 0),
    ],
)
def test_invalid_or_other_context_cache_entries_do_not_replace_live_check(tmp_path, column, value):
    source, target, cache, _ = cache_fixture(tmp_path)
    cache.db.execute(f"UPDATE cached SET {column}=?", (value,))
    cache.db.commit()
    assert cache.lookup(target, QQ, 24) is None
    assert target.summary()["checked"] == 0
    source.close()
    target.close()
    cache.close()


def test_newer_blocked_visit_invalidates_previous_success(tmp_path):
    source, target, cache, _ = cache_fixture(tmp_path)
    assert cache.lookup(target, QQ, 24) is not None
    target.save(classify_page({}, QQ, VIEWER))
    cache.remember(target, QQ)
    later = task(tmp_path / "20260922T120000Z-34567890")
    assert cache.lookup(later, QQ, 24) is None
    source.close()
    target.close()
    later.close()
    cache.close()


def test_bad_cache_file_disables_reuse_without_touching_task(tmp_path):
    broken = tmp_path / "cache.sqlite3"
    broken.write_text("not a database", encoding="utf-8")
    store = task(tmp_path / "task")
    cache = ObservationCache(broken)
    assert cache.lookup(store, QQ, 24) is None
    assert cache.warning
    assert store.summary()["pending"] == 3
    assert broken.read_text() == "not a database"
    store.close()


@pytest.mark.parametrize(
    "patch",
    [
        {"delay_seconds": 0},
        {"delay_seconds": True},
        {"batch_size": 301},
        {"batch_pause_seconds": 0},
        {"reuse_hours": 25},
        {"continuous": 1},
    ],
)
def test_invalid_settings_rejected(patch):
    with pytest.raises(InspectionError):
        replace(ScanOptions(), **patch).validate()


def test_historical_observation_preserves_time_and_export_provenance(tmp_path):
    store = task(tmp_path / "task")
    result = classify_page(raw_page(), QQ, VIEWER)
    result.evidence["reuse"] = {
        "source_task": "20260922T100000Z-12345678",
        "source_visit": 7,
        "reused_at": datetime.now(UTC).isoformat(),
        "contract": "qzone-dom-v1",
    }
    store.save(result)
    folder = store.folder
    store.close()
    store = Store(folder)
    row = store.rows()[0]
    assert row["checked_at"] == result.checked_at
    assert row["evidence"]["reuse"]["source_visit"] == 7
    exported = store.export()
    assert "历史观察" in (exported / "restricted.csv").read_text(encoding="utf-8-sig")
    assert store.reused_count() == 1
    store.close()
