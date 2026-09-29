"""Long-term runtime regressions; synthetic inputs, isolated DB, no network.

Internal regression coverage, separate from the external r132 review probes.
"""

from __future__ import annotations

import asyncio
import gc
import json
import threading
import weakref

import httpx
import pytest
from app.adapters.onebot.parser import OneBotMessageSource
from app.db import Base
from app.moderation import image_engine as image_module
from app.moderation.decision import ModerationDecision
from app.moderation.image_engine import ImageModerationEngine, MediaAnalysis
from app.runtime import pipeline, runner
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture(autouse=True)
def reject_network(monkeypatch):
    async def reject_async(*args, **kwargs):
        pytest.fail("runtime regression attempted an external HTTP request")

    def reject_sync(*args, **kwargs):
        pytest.fail("runtime regression attempted an external HTTP request")

    monkeypatch.setattr(httpx.AsyncClient, "send", reject_async)
    monkeypatch.setattr(httpx.Client, "send", reject_sync)


@pytest.fixture
async def isolated_sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("kind", ["video", "file", "image"])
async def test_media_analysis_allows_other_event_loop_tasks(
    monkeypatch, tmp_path, isolated_sessions, kind
):
    started = asyncio.Event()
    release = threading.Event()
    observed = []
    loop = asyncio.get_running_loop()
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / "synthetic.bin").write_bytes(b"synthetic media seam")
    monkeypatch.setattr(pipeline, "MEDIA_DIR", media_dir)

    def slow_analysis(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        observed.append(release.wait(timeout=0.5))
        if kind == "image":
            return MediaAnalysis("record_only", 0.1, reason="synthetic incomplete evidence")
        return ModerationDecision(
            message_id="99118001",
            verdict="record_only",
            confidence=0.1,
            reason="synthetic incomplete evidence",
        )

    if kind == "image":
        monkeypatch.setattr(ImageModerationEngine, "analyze", slow_analysis)
    else:
        monkeypatch.setattr(pipeline, f"evaluate_{kind}", slow_analysis)
    payload = {
        "time": 1789000400,
        "self_id": 10000001,
        "message_id": 99118001,
        "group_id": 300000001,
        "user_id": 200000001,
        "post_type": "message",
        "message_type": "group",
        "sender": {"user_id": 200000001, "role": "member"},
        "message": [{"type": kind, "data": {"file": "synthetic.bin"}}],
        "_downloaded": ["synthetic.bin"],
    }
    async with isolated_sessions() as session:
        task = asyncio.create_task(
            pipeline.run_pipeline(
                payload,
                session,
                message_source=OneBotMessageSource(),
                dedup_key=f"onebot:10000001:99118001:{kind}",
            )
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            release.set()
            result = await asyncio.wait_for(task, timeout=2)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert observed == [True], "the event loop could not release analysis before its deadline"
    assert result is not None and result.verdict == "record_only"


@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_blocking_call_cancellation_waits_for_real_thread_exit(cancel_count):
    from app.core.async_utils import blocking_call

    started = threading.Event()
    release = threading.Event()
    exited = threading.Event()

    def work():
        started.set()
        try:
            assert release.wait(timeout=2)
            return "finished"
        finally:
            exited.set()

    task = asyncio.create_task(blocking_call(work))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        for _ in range(cancel_count):
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done(), "cancellation released the caller while its thread still runs"
        assert not exited.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert exited.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_blocking_call_preserves_values_and_exceptions():
    from app.core.async_utils import blocking_call

    def add(a, *, b):
        return a + b

    def fail():
        raise ValueError("synthetic analysis error")

    assert await blocking_call(add, 4, b=7) == 11
    with pytest.raises(ValueError, match="synthetic analysis error"):
        await blocking_call(fail)


async def test_blocking_call_keeps_serial_capacity_during_running_and_queued_cancellation():
    from app.core.async_utils import blocking_call

    started = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    queued_started = threading.Event()
    next_started = threading.Event()

    def first():
        started.set()
        try:
            assert release.wait(timeout=3)
        finally:
            exited.set()

    def following():
        next_started.set()
        assert exited.is_set(), "a second media calculation overlapped the first"
        return "next"

    first_task = asyncio.create_task(blocking_call(first))
    tasks = [first_task]
    try:
        assert await asyncio.to_thread(started.wait, 1)
        queued = asyncio.create_task(blocking_call(queued_started.set))
        next_task = asyncio.create_task(blocking_call(following))
        tasks.extend([queued, next_task])
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        first_task.cancel()
        await asyncio.sleep(0)
        first_task.cancel()
        await asyncio.sleep(0)
        assert not first_task.done()
        assert not next_started.is_set()
        assert not queued_started.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first_task
        assert await next_task == "next"
        assert not queued_started.is_set(), "a cancelled queued call started its thread"
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_blocking_gate_does_not_retain_closed_event_loops():
    from app.core.async_utils import blocking_call

    loop_refs = []

    async def compete():
        loop = asyncio.get_running_loop()
        loop_refs.append(weakref.ref(loop))
        started = asyncio.Event()
        release = threading.Event()

        def first():
            loop.call_soon_threadsafe(started.set)
            assert release.wait(timeout=2)
            return 1

        first_task = asyncio.create_task(blocking_call(first))
        tasks = [first_task]
        try:
            await asyncio.wait_for(started.wait(), 1)
            second_task = asyncio.create_task(blocking_call(lambda: 2))
            tasks.append(second_task)
            await asyncio.sleep(0)  # Make the second call wait and bind its semaphore.
            release.set()
            assert await asyncio.gather(*tasks) == [1, 2]
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    for _ in range(3):
        asyncio.run(compete())
    gc.collect()
    assert all(loop_ref() is None for loop_ref in loop_refs)


class _OfficialSocket:
    def __init__(self, mode="busy", *, fail_heartbeat=False):
        self.mode = mode
        self.fail_heartbeat = fail_heartbeat
        self.closed = False
        self.received = 0
        self.heartbeats = []
        self.heartbeat_seen = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def recv(self):
        self.received += 1
        if self.received == 1:
            return json.dumps({"op": 10, "d": {"heartbeat_interval": 10}})
        if self.mode == "busy":
            await asyncio.sleep(0.001)
            return json.dumps({"op": 0, "t": "SYNTHETIC_OTHER", "s": self.received, "d": {}})
        if self.mode == "slow" and self.received == 2:
            return json.dumps(
                {"op": 0, "t": "GROUP_MESSAGE_CREATE", "s": 37, "d": {"id": "synthetic"}}
            )
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(self, frame):
        assert not self.closed, "a heartbeat task used an exited WebSocket"
        payload = json.loads(frame)
        if payload["op"] == 1:
            if self.fail_heartbeat:
                raise RuntimeError("synthetic heartbeat failure")
            self.heartbeats.append(payload)
            # Startup heartbeats may precede the first dispatch sequence.
            # Observe a heartbeat after input, without assuming timer ordering.
            if payload["d"] > 0:
                self.heartbeat_seen.set()


class _NoNetworkClient:
    def __init__(self, *args, **kwargs):
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


class _NoDatabaseSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def _wire_official(monkeypatch, socket):
    async def token(*args):
        return "synthetic-token"

    async def download(*args):
        pass

    monkeypatch.setattr(runner, "connect", lambda *args, **kwargs: socket)
    monkeypatch.setattr(runner.httpx, "AsyncClient", _NoNetworkClient)
    monkeypatch.setattr(runner, "_get_token", token)
    monkeypatch.setattr(runner, "_download_attachments", download)
    monkeypatch.setattr(runner, "_seed_image_engine", lambda: ImageModerationEngine())
    monkeypatch.setattr(runner, "SessionLocal", _NoDatabaseSession)


async def _stop_listener(task, stop):
    stop.set()
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_official_busy_frames_still_send_current_sequence_heartbeat(monkeypatch):
    socket = _OfficialSocket()
    _wire_official(monkeypatch, socket)
    stop = asyncio.Event()
    before = set(asyncio.all_tasks())
    task = asyncio.create_task(runner._listen_once("synthetic-app", "synthetic-secret", stop))
    try:
        await asyncio.wait_for(socket.heartbeat_seen.wait(), timeout=0.5)
        assert socket.received > 2
        assert socket.heartbeats[-1]["d"] > 0
    finally:
        await _stop_listener(task, stop)
    assert socket.closed
    assert not (set(asyncio.all_tasks()) - before), "listener left background tasks alive"


async def test_official_slow_pipeline_does_not_starve_heartbeat(monkeypatch):
    socket = _OfficialSocket("slow")
    _wire_official(monkeypatch, socket)
    processing = asyncio.Event()
    release = asyncio.Event()

    async def slow_pipeline(*args, **kwargs):
        processing.set()
        await release.wait()
        return None

    monkeypatch.setattr(runner, "run_pipeline", slow_pipeline)
    stop = asyncio.Event()
    before = set(asyncio.all_tasks())
    task = asyncio.create_task(runner._listen_once("synthetic-app", "synthetic-secret", stop))
    try:
        await asyncio.wait_for(processing.wait(), timeout=0.5)
        await asyncio.wait_for(socket.heartbeat_seen.wait(), timeout=0.5)
        assert not release.is_set()
        assert socket.heartbeats[-1]["d"] == 37
    finally:
        release.set()
        await _stop_listener(task, stop)
    assert socket.closed
    assert not (set(asyncio.all_tasks()) - before), "listener left background tasks alive"


async def test_official_heartbeat_failure_terminates_idle_connection(monkeypatch):
    socket = _OfficialSocket("idle", fail_heartbeat=True)
    _wire_official(monkeypatch, socket)
    stop = asyncio.Event()
    before = set(asyncio.all_tasks())
    task = asyncio.create_task(runner._listen_once("synthetic-app", "synthetic-secret", stop))
    try:
        with pytest.raises(Exception) as failure:
            await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
        errors = [failure.value]
        while any(isinstance(error, BaseExceptionGroup) for error in errors):
            errors = [
                leaf
                for error in errors
                for leaf in (error.exceptions if isinstance(error, BaseExceptionGroup) else [error])
            ]
        assert any(
            isinstance(error, RuntimeError) and str(error) == "synthetic heartbeat failure"
            for error in errors
        ), "heartbeat failure must propagate; a timeout is not an observed transport failure"
    finally:
        await _stop_listener(task, stop)
    assert socket.closed
    assert not (set(asyncio.all_tasks()) - before), "failed heartbeat left the receiver alive"


@pytest.mark.parametrize("verdict", ["allow", "violation_high", "record_only"])
def test_image_cache_lru_bounds_preserve_verdict_on_recomputation(monkeypatch, verdict):
    # Distinct hashes are separated enough that similarity never aliases our seam.
    hashes = {"a": "0000000000000000", "b": "ffffffffffffffff", "c": "aaaaaaaaaaaaaaaa"}
    monkeypatch.setattr(image_module, "image_frames_from_source", lambda source: [source])
    monkeypatch.setattr(image_module, "dhash", lambda frame: hashes[frame])
    monkeypatch.setattr(image_module, "decode_qr_codes", lambda frame: [])
    engine = ImageModerationEngine(
        cache_capacity=2,
        allowed_hashes={hashes["a"]} if verdict == "allow" else None,
        violation_hashes={hashes["a"]} if verdict == "violation_high" else None,
    )
    first = engine.analyze("a")
    engine.analyze("b")
    assert engine.analyze("a").cache_hit  # a becomes most recently used
    engine.analyze("c")
    assert len(engine._cache) == 2
    assert engine.analyze("a").cache_hit
    assert not engine.analyze("b").cache_hit  # b was evicted, not recent a
    engine.analyze("c")  # evict a without changing the policy
    recomputed = engine.analyze("a")
    assert not recomputed.cache_hit
    assert recomputed == first
    assert recomputed.verdict == verdict
    assert len(engine._cache) == 2
