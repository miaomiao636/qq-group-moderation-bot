"""Bounded empty-profile recovery; synthetic browser and isolated task data."""

import json

import pytest
from app.space_inspector.cache import ObservationCache
from app.space_inspector.contracts import BLOCKED, UNCONFIRMED, InspectionError
from app.space_inspector.store import Store

from tests.test_space_inspector_stability import QQ, VIEWER, profile, reader, store_at


def empty_profile():
    return profile() | {
        "ready": "complete",
        "normal_profile": False,
        "profile_markers": {"title": False, "logout": True},
    }


def recovering_browser(monkeypatch, final=None, **options):
    def frames(clock, frame):
        return empty_profile() if clock.visits == 1 else final or empty_profile()

    return reader(monkeypatch, [frames], **options)


def test_empty_profile_gets_one_same_member_revisit(monkeypatch):
    browser, clock = recovering_browser(monkeypatch, profile())
    result = browser.inspect(QQ, VIEWER)
    assert result.status == UNCONFIRMED and clock.visits == 2
    assert 30.5 <= clock.now < 32
    assert result.evidence["ready_state"] == "interactive"
    assert result.evidence["load_diagnostics"]["empty_profile_reloads"] == 1
    assert result.evidence["load_diagnostics"]["initial_wait_ms"] >= 30000


def test_permanent_empty_profile_stops_after_second_budget(monkeypatch):
    browser, clock = recovering_browser(monkeypatch)
    result = browser.inspect(QQ, VIEWER)
    assert result.status == BLOCKED and result.reason == "unrecognized_page"
    assert clock.visits == 2 and clock.now == 60


def test_profile_ready_at_final_recheck_needs_no_revisit(monkeypatch):
    frames = [empty_profile()] * 121 + [profile() | {"ready": "complete"}]
    browser, clock = reader(monkeypatch, frames)
    result = browser.inspect(QQ, VIEWER)
    assert result.status == UNCONFIRMED and clock.visits == 1 and clock.now == 30


@pytest.mark.parametrize(
    "raw,reason",
    [
        (profile() | {"url": "https://waf.tencent.com/501page.html"}, "platform_access_blocked"),
        (profile() | {"url": "https://i.qq.com/"}, "login_required"),
        (profile() | {"viewers": ["88765432"]}, "viewer_missing_or_changed"),
        (profile() | {"url": "https://user.qzone.qq.com/88765432"}, "unexpected_location"),
    ],
)
@pytest.mark.parametrize("during_recheck", [True, False])
def test_barriers_never_trigger_another_navigation(monkeypatch, raw, reason, during_recheck):
    if during_recheck:
        browser, clock = reader(monkeypatch, [empty_profile()] * 121 + [raw])
    else:
        browser, clock = recovering_browser(monkeypatch, raw)
    result = browser.inspect(QQ, VIEWER)
    assert result.status == BLOCKED and result.reason == reason
    assert clock.visits == (1 if during_recheck else 2)


@pytest.mark.parametrize(
    "change",
    [
        {"panels": [{"paragraphs": ["需要验证"], "report_icons": 1}]},
        {"permission_panels": [{"tips": "需要验证"}]},
    ],
)
@pytest.mark.parametrize("when", ["earlier", "recheck"])
def test_unknown_panels_prevent_revisit_even_if_later_absent(monkeypatch, change, when):
    frames = (
        [empty_profile() | change, empty_profile()]
        if when == "earlier"
        else [empty_profile()] * 121 + [empty_profile() | change]
    )
    browser, clock = reader(monkeypatch, frames)
    assert browser.inspect(QQ, VIEWER).status == BLOCKED
    assert clock.visits == 1


def test_first_navigation_timeout_with_commit_does_not_enable_revisit(monkeypatch):
    browser, clock = recovering_browser(monkeypatch, profile())
    original = browser._page.goto

    def timeout(*args, **kwargs):
        original(*args, **kwargs)
        raise TimeoutError("synthetic committed timeout")

    browser._page.goto = timeout
    assert browser.inspect(QQ, VIEWER).status == BLOCKED
    assert clock.visits == 1


@pytest.mark.parametrize("during_recheck", [False, True])
def test_pause_cannot_start_revisit(monkeypatch, during_recheck):
    if during_recheck:

        def cancelled(clock, frame):
            browser._stop.is_set = lambda: True
            return empty_profile()

        browser, clock = reader(monkeypatch, [empty_profile()] * 121 + [cancelled])
    else:
        browser, clock = recovering_browser(monkeypatch, profile(), stop_at=30)
    assert browser.inspect(QQ, VIEWER).status == BLOCKED
    assert clock.visits == 1


@pytest.mark.parametrize("interrupt", ["pause", "navigation"])
def test_recheck_cannot_complete_after_pause_or_document_change(monkeypatch, interrupt):
    def changed(clock, frame):
        if interrupt == "pause":
            browser._stop.is_set = lambda: True
        else:
            clock.handlers["framenavigated"](frame)
        return profile() | {"ready": "complete"}

    browser, clock = reader(monkeypatch, [empty_profile()] * 121 + [changed])
    assert browser.inspect(QQ, VIEWER).status == BLOCKED
    assert clock.visits == 1


def test_failed_revisit_cannot_observe_the_old_document(monkeypatch):
    browser, clock = recovering_browser(monkeypatch, profile() | {"ready": "complete"})
    original = browser._page.goto

    def failed(*args, **kwargs):
        if clock.visits:
            clock.visits += 1
            raise TimeoutError("synthetic failure before new document")
        original(*args, **kwargs)

    browser._page.goto = failed
    result = browser.inspect(QQ, VIEWER)
    assert result.status == BLOCKED and result.reason == "navigation_failed"
    assert clock.visits == 2


def test_recheck_exception_does_not_revisit(monkeypatch):
    browser, clock = reader(
        monkeypatch, [empty_profile()] * 121 + [RuntimeError("synthetic closed page")]
    )
    assert browser.inspect(QQ, VIEWER).status == BLOCKED
    assert clock.visits == 1


def test_recovery_evidence_survives_save_cache_and_export(tmp_path, monkeypatch):
    browser, clock = recovering_browser(monkeypatch, profile())
    result = browser.inspect(QQ, VIEWER)
    assert result.status == UNCONFIRMED
    store = store_at(tmp_path / "20260929T010000Z-12345678")
    other = store_at(tmp_path / "20260929T010000Z-87654321")
    cache = ObservationCache(tmp_path / "cache.sqlite3")
    try:
        store.save(result)
        assert store.db.execute("SELECT COUNT(*) FROM visits").fetchone()[0] == 1
        store.close()
        store = Store(tmp_path / "20260929T010000Z-12345678")
        cache.remember(store)
        reused = cache.lookup(other, QQ, 24)
        assert reused is not None
        other.save(reused)
        report = json.loads((other.export() / "report.json").read_text(encoding="utf-8"))
        assert report["rows"][0]["evidence"]["load_diagnostics"]["empty_profile_reloads"] == 1
        assert clock.visits == 2
    finally:
        cache.close()
        other.close()
        store.close()


@pytest.mark.parametrize("missing", ["empty_profile_reloads", "initial_wait_ms"])
def test_recovery_diagnostics_require_both_fields(tmp_path, monkeypatch, missing):
    browser, _ = recovering_browser(monkeypatch, profile())
    result = browser.inspect(QQ, VIEWER)
    del result.evidence["load_diagnostics"][missing]
    store = store_at(tmp_path / "task")
    try:
        with pytest.raises(InspectionError):
            store.save(result)
    finally:
        store.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"empty_profile_reloads": 0},
        {"empty_profile_reloads": 2},
        {"empty_profile_reloads": True},
        {"initial_wait_ms": 29999},
        {"initial_wait_ms": "30000"},
        {"initial_wait_ms": 700000},
    ],
)
def test_recovery_diagnostics_are_strictly_bounded(tmp_path, monkeypatch, changes):
    browser, _ = recovering_browser(monkeypatch, profile())
    result = browser.inspect(QQ, VIEWER)
    result.evidence["load_diagnostics"].update(changes)
    store = store_at(tmp_path / "task")
    try:
        with pytest.raises(InspectionError):
            store.save(result)
    finally:
        store.close()
