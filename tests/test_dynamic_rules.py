"""T-105 dynamic rule system tests."""

from __future__ import annotations

import json
import uuid

import pytest
from app.adapters.qq_official.contract import Sender, ShareCardInfo, StandardMessage
from app.db import SessionLocal
from app.moderation.dynamic_rules import (
    DynamicRuleEngine,
    add_rule_item,
    create_rule_draft,
    load_active_snapshot,
    publish_rule_version,
    rollback_to_version,
)
from app.runtime.pipeline import run_pipeline


def make_text(text: str, *, group: str = "G_DYN") -> StandardMessage:
    return StandardMessage(
        message_id=f"DYN_{uuid.uuid4().hex[:8]}",
        group_openid=group,
        sender=Sender(member_openid="M_DYN"),
        text=text,
    )


@pytest.mark.asyncio
async def test_draft_rule_does_not_affect_runtime() -> None:
    word = f"草稿违规词{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        draft = await create_rule_draft(session, scope="global", scope_key="*", name="draft")
        await add_rule_item(
            session,
            draft.id,
            item_type="keyword",
            pattern=word,
            category="ad",
            weight=0.95,
        )
        snapshot = await load_active_snapshot(session, None)

    decision = DynamicRuleEngine(snapshot).evaluate(make_text(word))
    assert decision.verdict != "violation_high"


@pytest.mark.asyncio
async def test_published_rule_affects_runtime_without_restart() -> None:
    word = f"发布违规词{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        before = await load_active_snapshot(session, None)
        draft = await create_rule_draft(session, scope="global", scope_key="*", name="publish")
        await add_rule_item(
            session,
            draft.id,
            item_type="keyword",
            pattern=word,
            category="ad",
            weight=0.95,
        )
        await publish_rule_version(session, draft.id, operator="test")
        after = await load_active_snapshot(session, None)

    assert before.version_ids != after.version_ids
    decision = DynamicRuleEngine(after).evaluate(make_text(word))
    assert decision.verdict == "violation_high"


@pytest.mark.asyncio
async def test_rollback_restores_previous_active_version() -> None:
    word = f"回滚违规词{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        original = await load_active_snapshot(session, None)
        draft = await create_rule_draft(session, scope="global", scope_key="*", name="rollback")
        await add_rule_item(
            session,
            draft.id,
            item_type="keyword",
            pattern=word,
            category="ad",
            weight=0.95,
        )
        await publish_rule_version(session, draft.id, operator="test")
        await rollback_to_version(session, original.version_id, operator="test")
        restored = await load_active_snapshot(session, None)

    decision = DynamicRuleEngine(restored).evaluate(make_text(word))
    assert restored.version_id == original.version_id
    assert decision.verdict != "violation_high"


@pytest.mark.asyncio
async def test_rule_validation_rejects_code_sql_and_unbounded_regex() -> None:
    async with SessionLocal() as session:
        draft = await create_rule_draft(session, scope="global", scope_key="*", name="safe")
        for pattern in ("__import__('os')", "select * from users", "坏词.*"):
            with pytest.raises(ValueError):
                await add_rule_item(
                    session,
                    draft.id,
                    item_type="keyword",
                    pattern=pattern,
                    category="ad",
                    weight=0.95,
                )


@pytest.mark.asyncio
async def test_allow_and_block_conflict_becomes_record_only() -> None:
    group = f"G_CONFLICT_{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        draft = await create_rule_draft(session, scope="group", scope_key=group, name="conflict")
        await add_rule_item(
            session,
            draft.id,
            item_type="share_source",
            pattern="万能校园墙",
            category="allow",
            weight=0.0,
        )
        await add_rule_item(
            session,
            draft.id,
            item_type="keyword",
            pattern="冲突违规词",
            category="ad",
            weight=0.95,
        )
        await publish_rule_version(session, draft.id, operator="test")
        snapshot = await load_active_snapshot(session, group)

    msg = StandardMessage(
        message_id=f"DYN_CARD_{uuid.uuid4().hex[:8]}",
        group_openid=group,
        sender=Sender(member_openid="M_DYN"),
        kind="share_card",
        text="[卡片消息] 小程序\nsource: 万能校园墙\ntitle: 冲突违规词",
        share_card=ShareCardInfo(source="万能校园墙", title="冲突违规词"),
    )
    decision = DynamicRuleEngine(snapshot).evaluate(msg)

    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []


@pytest.mark.asyncio
async def test_pipeline_loads_published_dynamic_rules_without_restart() -> None:
    group = f"G_PIPE_DYN_{uuid.uuid4().hex[:6]}"
    word = f"流水线违规词{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        draft = await create_rule_draft(session, scope="group", scope_key=group, name="pipeline")
        await add_rule_item(
            session,
            draft.id,
            item_type="keyword",
            pattern=word,
            category="ad",
            weight=0.95,
        )
        await publish_rule_version(session, draft.id, operator="test")
        payload = {
            "id": f"DYN_PIPE_{uuid.uuid4().hex[:8]}",
            "group_openid": group,
            "group_id": group,
            "author": {
                "member_openid": "M_PIPE_DYN",
                "member_role": "member",
                "bot": False,
                "username": "tester",
            },
            "content": word,
            "attachments": [],
            "timestamp": "2026-09-06T10:00:00+08:00",
        }
        record = await run_pipeline(payload, session)

    assert record is not None
    assert record.verdict == "violation_high"
    detail = json.loads(record.detail_json)
    assert draft.id in detail["rule_version_ids"]
    assert any(hit["rule_id"].startswith("DR_") for hit in detail["rule_hits"])
