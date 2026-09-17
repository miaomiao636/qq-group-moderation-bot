"""Shadow decision upserts stay idempotent; 口径 C no longer preserves bindings."""

import json
import uuid

import pytest
from app.db import SessionLocal
from app.runtime.pipeline import upsert_shadow_decision


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


async def test_source_update_overwrites_detail_without_binding_semantics():
    """口径 C（2026-09-17）：豁免为无状态重算——重试直接覆盖 detail，不保留旧绑定字段。"""
    key = "synthetic-shadow-" + uuid.uuid4().hex
    async with SessionLocal() as writer:
        await upsert_shadow_decision(
            writer,
            message_id=key,
            group_openid="synthetic",
            member_openid="synthetic",
            verdict="allow",
            detail_json=json.dumps(
                {"wall_paired": True, "wall_paired_message_id": "synthetic-text"}
            ),
        )
    async with SessionLocal() as session:
        updated = await upsert_shadow_decision(
            session,
            message_id=key,
            detail_json='{"ai_results":[],"action_intents":[]}',
        )
    detail = json.loads(updated.detail_json)
    assert "wall_paired" not in detail
    assert detail["action_intents"] == []
