"""T-306 security and identity isolation regressions."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.core.contracts import Attachment, MessageParseError, Sender, StandardMessage
from app.db import SessionLocal
from app.moderation.image_engine import MediaAnalysis
from app.moderation.rules import TextRuleEngine
from app.runtime import onebot_wiring
from app.runtime.models import ShadowDecision
from app.runtime.pipeline import run_pipeline
from sqlalchemy import func, select

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "onebot"


def _event(name: str = "group_message_text.json") -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))["event"]


def test_dedup_key_rejects_missing_self_id() -> None:
    with pytest.raises(MessageParseError, match="self_id"):
        onebot_wiring.dedup_key_for({"message_id": "1"}, "1")


@pytest.mark.asyncio
async def test_duplicate_event_does_not_repeat_media_preparation(monkeypatch: Any) -> None:
    payload = _event("group_message_image.json")
    payload["message_id"] = f"PREP_{uuid.uuid4().hex[:8]}"
    calls = 0

    async def fake_download(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        args[0]["_downloaded"] = [""]

    monkeypatch.setattr(onebot_wiring, "download_onebot_media", fake_download)
    async with httpx.AsyncClient() as client, SessionLocal() as session:
        kwargs = {
            "text_engine": TextRuleEngine(),
            "image_engine": onebot_wiring.ImageModerationEngine(),
            "dl_client": client,
        }
        first = await onebot_wiring.process_onebot_event(payload, session, **kwargs)
        second = await onebot_wiring.process_onebot_event(payload, session, **kwargs)

    assert first is not None
    assert second is None
    assert calls == 1


@pytest.mark.asyncio
async def test_same_external_message_id_from_two_accounts_stays_isolated() -> None:
    raw_message_id = f"SHARED_{uuid.uuid4().hex[:8]}"
    event_a = _event()
    event_b = _event()
    event_a.update({"message_id": raw_message_id, "self_id": "10001"})
    event_b.update({"message_id": raw_message_id, "self_id": "10002"})

    async with httpx.AsyncClient() as client, SessionLocal() as session:
        kwargs = {
            "text_engine": TextRuleEngine(),
            "image_engine": onebot_wiring.ImageModerationEngine(),
            "dl_client": client,
        }
        first = await onebot_wiring.process_onebot_event(event_a, session, **kwargs)
        second = await onebot_wiring.process_onebot_event(event_b, session, **kwargs)
        count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ShadowDecision)
                    .where(ShadowDecision.external_message_id == raw_message_id)
                )
            ).scalar_one()
        )

    assert first is not None and second is not None
    assert first.message_id == f"onebot:10001:{raw_message_id}"
    assert second.message_id == f"onebot:10002:{raw_message_id}"
    assert count == 2


class _UnsafePathSource:
    provider = "onebot"

    def __init__(self, path: Path) -> None:
        self.path = path

    def parse_group_message(self, payload: Any, /) -> StandardMessage:
        return StandardMessage(
            message_id=str(payload["message_id"]),
            provider="onebot",
            external_group_id="30001",
            external_user_id="20001",
            sender=Sender(member_openid="20001"),
            kind="image",
            attachments=[Attachment(content_type="image/jpeg", filename=str(self.path))],
        )


class _RecordingImageEngine:
    def __init__(self) -> None:
        self.paths: list[Path] = []

    def analyze(self, path: Path) -> MediaAnalysis:
        self.paths.append(path)
        return MediaAnalysis("allow", 1.0, "test")


@pytest.mark.asyncio
async def test_pipeline_never_reads_media_outside_managed_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"not managed media")
    engine = _RecordingImageEngine()
    payload = {"message_id": f"PATH_{uuid.uuid4().hex[:8]}"}

    async with SessionLocal() as session:
        record = await run_pipeline(
            payload,
            session,
            message_source=_UnsafePathSource(outside),  # type: ignore[arg-type]
            image_engine=engine,  # type: ignore[arg-type]
        )

    assert record is not None
    assert record.verdict == "record_only"
    assert engine.paths == []
