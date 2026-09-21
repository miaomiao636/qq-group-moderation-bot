"""Internal orchestration regressions; no real account or browser requests."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from app.space_inspector.browser import classify_page
from app.space_inspector.contracts import (
    BLOCKED,
    UNCONFIRMED,
    InspectionError,
    Observation,
    PlatformAccessBlocked,
)
from app.space_inspector.service import Service, run_scan


class MemoryStore:
    def __init__(self) -> None:
        self.saved: list[Observation] = []

    def pending(self) -> list[str]:
        return ["12345678", "22345678"]

    def save(self, observation: Observation) -> None:
        self.saved.append(observation)

    def summary(self) -> dict[str, object]:
        return {"checked": len(self.saved)}


class Reader:
    def __init__(self, status: str = UNCONFIRMED) -> None:
        self.status = status
        self.calls: list[str] = []

    def inspect(self, qq: str, viewer_qq: str) -> Observation:
        self.calls.append(qq)
        result = classify_page({}, qq, viewer_qq)
        return Observation(qq, self.status, result.reason, result.checked_at, result.evidence)


def test_scan_saves_before_progress_and_finishes_snapshot() -> None:
    store, reader, seen = MemoryStore(), Reader(), []

    def progress(summary: dict[str, object]) -> None:
        seen.append(len(store.saved))

    run_scan(store, reader, "98765432", threading.Event(), progress, delay=0)
    assert reader.calls == ["12345678", "22345678"]
    assert seen == [1, 2]


def test_blocked_visit_is_saved_then_job_stops() -> None:
    store, reader = MemoryStore(), Reader(BLOCKED)
    with pytest.raises(InspectionError, match="QQ 12345678.*页面格式无法确认"):
        run_scan(store, reader, "98765432", threading.Event(), lambda _: None, delay=0)
    assert len(store.saved) == 1
    assert reader.calls == ["12345678"]


def test_unopened_space_is_saved_and_scan_continues_to_next_member() -> None:
    class UnopenedReader:
        def inspect(self, qq: str, viewer_qq: str) -> Observation:
            return classify_page(
                {
                    "url": f"https://user.qzone.qq.com/{qq}",
                    "ready": "complete",
                    "viewers": [viewer_qq],
                    "normal_profile": False,
                    "panels": [
                        {
                            "paragraphs": ["对方未开通空间", "邀请开通\u00a0\u00a0返回我的空间"],
                            "report_icons": 0,
                        }
                    ],
                },
                qq,
                viewer_qq,
            )

    store = MemoryStore()
    run_scan(store, UnopenedReader(), "98765432", threading.Event(), lambda _: None, delay=0)
    assert [item.qq for item in store.saved] == ["12345678", "22345678"]
    assert all(
        item.status == UNCONFIRMED and item.reason == "space_not_opened" for item in store.saved
    )


def test_pause_before_scan_makes_no_requests() -> None:
    stop = threading.Event()
    stop.set()
    reader = Reader()
    run_scan(MemoryStore(), reader, "98765432", stop, lambda _: None, delay=0)
    assert not reader.calls


def test_platform_block_saves_incomplete_result_and_never_visits_next_member():
    class BlockedReader(Reader):
        def inspect(self, qq: str, viewer_qq: str) -> Observation:
            self.calls.append(qq)
            return classify_page(
                {"url": "https://waf.tencent.com/501page.html?id=SECRET"}, qq, viewer_qq
            )

    store, reader = MemoryStore(), BlockedReader()
    with pytest.raises(PlatformAccessBlocked, match="停止重试") as raised:
        run_scan(store, reader, "98765432", threading.Event(), lambda _: None, delay=0)
    assert reader.calls == ["12345678"]
    assert len(store.saved) == 1
    assert store.saved[0].status == BLOCKED
    assert store.saved[0].reason == "platform_access_blocked"
    assert "SECRET" not in str(raised.value)


def test_pause_after_visit_preserves_result_without_next_request() -> None:
    store, reader, stop = MemoryStore(), Reader(), threading.Event()
    run_scan(store, reader, "98765432", stop, lambda _: stop.set(), delay=10)
    assert len(store.saved) == 1
    assert reader.calls == ["12345678"]


def test_storage_failure_does_not_advance_to_next_member() -> None:
    class BrokenStore(MemoryStore):
        def save(self, observation: Observation) -> None:
            raise OSError("disk full")

    reader = Reader()
    with pytest.raises(OSError):
        run_scan(BrokenStore(), reader, "98765432", threading.Event(), lambda _: None, delay=0)
    assert reader.calls == ["12345678"]


def test_close_attempts_all_resources_and_retains_lock_on_failure() -> None:
    calls = []
    service = Service.__new__(Service)

    def fail_storage():
        calls.append("storage")
        raise OSError("disk error")

    service._release_task = fail_storage
    service._browser = SimpleNamespace(close=lambda: calls.append("browser"))
    service._directory = SimpleNamespace(close=lambda: calls.append("directory"))
    service._lock = SimpleNamespace(__exit__=lambda *args: calls.append("unlock"))
    with pytest.raises(InspectionError):
        service.close()
    assert calls == ["storage", "browser", "directory"]
    service._release_task = lambda: calls.append("storage_ok")
    service.close()
    assert calls[-4:] == ["storage_ok", "browser", "directory", "unlock"]
