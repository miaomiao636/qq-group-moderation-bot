"""A persisted one-shot wall binding survives later source-image writes."""

import json
import uuid

import pytest
from app.db import SessionLocal
from app.runtime.models import ShadowDecision
from app.runtime.pipeline import upsert_shadow_decision
from sqlalchemy import select, update


@pytest.mark.parametrize("old_detail", ["{}", "{broken", "null"])
async def test_shadow_update_handles_missing_or_malformed_previous_detail(old_detail):
    key = "synthetic-shadow-" + uuid.uuid4().hex
    async with SessionLocal() as session:
        await upsert_shadow_decision(
            session,
            message_id=key,
            group_openid="synthetic",
            member_openid="synthetic",
            verdict="record_only",
            detail_json=old_detail,
        )
        record = await upsert_shadow_decision(
            session,
            message_id=key,
            detail_json='{"processing":false}',
            verdict="allow",
        )
        assert json.loads(record.detail_json) == {"processing": False}
        assert record.verdict == "allow"


async def test_source_update_preserves_binding_written_after_orm_record_was_loaded():
    key = "synthetic-shadow-" + uuid.uuid4().hex
    async with SessionLocal() as writer:
        await upsert_shadow_decision(
            writer,
            message_id=key,
            group_openid="synthetic",
            member_openid="synthetic",
            verdict="allow",
            detail_json='{"sent_at":"2026-09-14T00:00:00Z"}',
        )
    async with SessionLocal() as stale, SessionLocal() as pairing:
        held_record = await stale.scalar(
            select(ShadowDecision).where(ShadowDecision.message_id == key)
        )
        assert held_record is not None
        await pairing.execute(
            update(ShadowDecision)
            .where(ShadowDecision.message_id == key)
            .values(
                detail_json=json.dumps(
                    {"wall_paired": True, "wall_paired_message_id": "synthetic-text"}
                )
            )
        )
        await pairing.commit()
        updated = await upsert_shadow_decision(
            stale,
            message_id=key,
            detail_json='{"ai_results":[],"action_intents":[]}',
        )
        detail = json.loads(updated.detail_json)
        assert detail["wall_paired"] is True
        assert detail["wall_paired_message_id"] == "synthetic-text"
        assert detail["action_intents"] == []
