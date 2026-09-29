# ruff: noqa: E402, I001, F401
# Reviewer probe pack (PR #45 / r132), promoted verbatim into the repo test suite.
# Isolation asserts intentionally run BEFORE application imports (E402 is by design).
# This header changes no assertion and no logic.

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

# Require the repository test fixture BEFORE importing app/database modules.
# Failing this guard must never cause fallback to any developer/production .env.
assert os.environ.get("APP_ENV") == "test", "Load tests/conftest.py with -p conftest"
assert os.environ.get("ACTION_MODE") == "SHADOW"
for _flag in (
    "AI_ENABLED",
    "ONEBOT_ACTIONS_ENABLED",
    "NOTIFICATIONS_ENABLED",
    "NOTIFICATION_QQ_ENABLED",
    "NOTIFICATION_EMAIL_ENABLED",
    "NOTIFICATION_HEARTBEAT_ENABLED",
):
    assert os.environ.get(_flag) == "false", _flag
_database_url = os.environ.get("DATABASE_URL", "")
assert _database_url.startswith("sqlite+aiosqlite:///")
_database_path = Path(_database_url.split(":///", 1)[1]).resolve()
assert _database_path.parent.name.startswith("qqbot-test-")
assert _database_path.is_relative_to(Path(tempfile.gettempdir()).resolve())

from app.db import SessionLocal
from app.runtime import pipeline
from app.runtime.inbox import enqueue_event
from tests.test_pairing_inflight import _event, _run, _synthetic_id


@pytest.mark.parametrize("offset", [0, 1], ids=["same-second", "one-second-before"])
@pytest.mark.parametrize("stage", ["queued", "downloading"])
async def test_pending_image_does_not_enable_text_punishment(monkeypatch, stage, offset):
    now = datetime.now(UTC).replace(microsecond=0)
    group, user = _synthetic_id(), _synthetic_id()
    image = _event(group=group, user=user, when=now - timedelta(seconds=offset), kind="image")
    text = _event(group=group, user=user, when=now, kind="text")
    observed = {}

    async def no_external_actions(session, msg, decision, **kwargs):
        observed[msg.message_id] = decision
        return []

    monkeypatch.setattr(pipeline, "orchestrate_actions", no_external_actions)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def prepare(payload):
        entered.set()
        await release.wait()

    task = None
    try:
        if stage == "queued":
            async with SessionLocal() as session:
                await enqueue_event(session, image, max_pending=100_000)
        else:
            task = asyncio.create_task(_run(image, prepare_payload=prepare))
            await asyncio.wait_for(entered.wait(), timeout=5)
        result = await asyncio.wait_for(_run(text), timeout=5)
    finally:
        release.set()
        if task is not None:
            await asyncio.wait_for(task, timeout=5)
    assert result is not None
    actual = observed[str(text["message_id"])]
    assert result.verdict == "record_only", (result.verdict, actual.recommended_actions)
    assert actual.recommended_actions == []
    assert "未完成" in result.reason
    assert "未授予" in result.reason
