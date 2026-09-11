"""Fifteen-day raw-content erasure keeps durable moderation metadata intact."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.cases.models import Case, ViolationRecord
from app.cases.service import count_active_violations
from app.core.contracts import Sender, StandardMessage
from app.db import Base
from app.models import SystemSetting
from app.moderation.ai import AICacheEntry, AIModerationResult, AIUsageLog, get_cached_ai_result
from app.moderation.dynamic_rules import (
    DynamicRuleEngine,
    load_active_snapshot,
    publish_rule_version,
)
from app.moderation.feedback import (
    FeedbackRecord,
    RuleCandidate,
    RuleCandidateExample,
    copy_candidate_to_draft,
    mine_rule_candidates,
    record_feedback,
)
from app.reports import cleanup, maintenance
from app.runtime import pipeline
from app.runtime.models import ShadowDecision
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

NOW = datetime(2031, 1, 20, 12, tzinfo=UTC)
OLD = NOW - timedelta(days=20)
RAW = "synthetic-private-original-content"


@pytest.fixture
async def retention_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncSession]:
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    settings = cleanup.get_settings().model_copy(
        update={"raw_retention_days": 15, "decision_retention_days": 15}
    )
    monkeypatch.setattr(cleanup, "get_settings", lambda: settings)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
    finally:
        await engine.dispose()


def shadow(mid: str, *, created_at: datetime = OLD) -> ShadowDecision:
    return ShadowDecision(
        message_id=f"onebot:self:{mid}",
        external_message_id=mid,
        provider="onebot",
        external_group_id="group",
        external_user_id="member",
        group_openid="group",
        member_openid="member",
        sender_name=RAW,
        kind="text",
        verdict="violation_high",
        category="ad",
        confidence=0.99,
        reason=RAW,
        created_at=created_at,
        detail_json=json.dumps(
            {
                "text_preview": RAW,
                "segments": [{"kind": "text", "text": RAW, "attachment_index": None}],
                "media_files": [{"name": "synthetic.jpg", "type": "image/jpeg"}],
                "parse_error": RAW,
                "unrecognized_original_copy": RAW,
                "rule_version_ids": [7],
                "recommended_actions": ["recall", "mute", "warn"],
                "is_protected_sender": False,
                "rule_hits": [
                    {
                        "rule_id": "AI_text",
                        "rule_name": "model-v1",
                        "category": "ad",
                        "confidence_delta": 0.99,
                        "evidence_masked": RAW,
                    }
                ],
                "ai_results": [
                    {
                        "model_id": "model-v1",
                        "prompt_version": "v7",
                        "evidence": RAW,
                        "category": "ad",
                        "confidence": 0.99,
                        "input_tokens": 20,
                        "output_tokens": 5,
                        "degraded_reason": RAW,
                    }
                ],
                "action_intents": [
                    {"id": 2, "action": "recall", "status": "SUCCEEDED", "reason": RAW}
                ],
            }
        ),
    )


def feedback(mid: str, *, created_at: datetime = OLD, provider: str = "onebot") -> FeedbackRecord:
    return FeedbackRecord(
        message_id=mid,
        group_openid="group",
        member_openid="member",
        provider=provider,
        external_group_id="group",
        external_user_id="member",
        label="confirmed_violation",
        category="ad",
        operator="synthetic-human",
        source="web",
        reason=RAW,
        sample_text_masked=RAW,
        created_at=created_at,
    )


async def test_old_shadow_content_is_redacted_but_audit_metadata_remains(
    retention_session: AsyncSession,
) -> None:
    session = retention_session
    old = shadow("old")
    fresh = shadow("fresh", created_at=NOW - timedelta(days=14))
    boundary = shadow("boundary", created_at=NOW - timedelta(days=15))
    malformed = shadow("malformed")
    malformed.detail_json = "not-json-" + RAW
    session.add_all([old, fresh, boundary, malformed])
    await session.commit()

    result = await cleanup.purge_expired(session, now=NOW)
    await session.refresh(old)
    await session.refresh(fresh)
    await session.refresh(malformed)

    assert RAW not in old.detail_json + old.reason + old.sender_name
    assert RAW not in malformed.detail_json + malformed.reason
    assert RAW in fresh.detail_json and fresh.reason == RAW
    await session.refresh(boundary)
    assert RAW in boundary.detail_json  # Existing retention contract uses strict < cutoff.
    assert old.external_message_id == "old" and old.external_user_id == "member"
    assert old.verdict == "violation_high" and old.confidence == 0.99
    detail = json.loads(old.detail_json)
    assert detail["purged"] is True
    assert detail["rule_version_ids"] == [7]
    assert detail["rule_hits"][0]["rule_id"] == "AI_text"
    assert detail["ai_results"][0]["model_id"] == "model-v1"
    assert detail["ai_results"][0]["input_tokens"] == 20
    assert detail["ai_results"][0]["degraded_reason"] == "raw_retention_expired_degraded"
    assert detail["action_intents"][0]["status"] == "SUCCEEDED"
    assert not detail.get("media_files")
    assert result["shadow_content_purged"] == 2

    again = await cleanup.purge_expired(session, now=NOW)
    assert again["shadow_content_purged"] == 0


async def test_feedback_copies_expire_with_source_without_cross_provider_redaction(
    retention_session: AsyncSession,
) -> None:
    session = retention_session
    original = shadow("feedback-source")
    late_copy = feedback(original.message_id, created_at=NOW - timedelta(days=1))
    other_provider = feedback(original.message_id, created_at=NOW, provider="qq_official")
    orphan = feedback("orphan")
    recent = feedback("recent", created_at=NOW - timedelta(days=14))
    session.add_all([original, late_copy, other_provider, orphan, recent])
    await session.commit()

    result = await cleanup.purge_expired(session, now=NOW)

    for row in (late_copy, orphan):
        await session.refresh(row)
        assert not row.sample_text_masked and RAW not in row.reason
        assert row.label == "confirmed_violation" and row.operator == "synthetic-human"
        assert row.external_group_id == "group" and row.external_user_id == "member"
    for row in (other_provider, recent):
        await session.refresh(row)
        assert row.sample_text_masked == RAW
    assert result["feedback_content_purged"] == 2


async def test_raw_erasure_preserves_strike_count_mapping_and_case_state(
    retention_session: AsyncSession,
) -> None:
    session = retention_session
    old = datetime.now(UTC) - timedelta(days=20)
    original = shadow("strike", created_at=old)
    violation = ViolationRecord(
        provider="onebot",
        external_group_id="group",
        external_user_id="member",
        group_openid="group",
        member_openid="member",
        message_id="strike",
        category="ad",
        confidence=0.99,
        created_at=old,
        message_snapshot_json=json.dumps({"text": RAW}),
        rule_hits_json=json.dumps(
            [{"rule_id": "R001", "rule_name": "explicit-rule", "evidence_masked": RAW}]
        ),
        revoke_reason=f"{RAW}（操作人: synthetic-human）",
    )
    case = Case(
        case_no="R-synthetic-retention",
        group_openid="group",
        member_openid="member",
        provider="onebot",
        external_group_id="group",
        external_user_id="member",
        status="PENDING_REVIEW",
        created_at=datetime.now(UTC),
        audit_json=json.dumps(
            {
                "evidence_count": 1,
                "revoked_by": "synthetic-human",
                "revoke_reason": RAW,
                "transitions": [
                    {
                        "operator": "synthetic-human",
                        "from": "PENDING_REVIEW",
                        "to": "KEEP",
                        "at": "synthetic-time",
                        "extra": {"reason": "new-independent-audit-note"},
                    }
                ],
            }
        ),
    )
    session.add_all([original, violation, case])
    await session.flush()
    violation.case_id = case.id
    case.violation_ids_json = json.dumps([violation.id])
    session.add(SystemSetting(key="auto_cleanup_enabled", value="1"))
    await session.commit()

    outcome = await maintenance.run_cleanup(session)
    assert outcome["status"] == "succeeded"  # New counters must be accepted by CLI metadata.
    for row in (original, violation, case):
        await session.refresh(row)
    assert (
        RAW
        not in violation.message_snapshot_json + violation.rule_hits_json + violation.revoke_reason
    )
    assert RAW not in case.audit_json
    assert case.status == "PENDING_REVIEW" and json.loads(case.violation_ids_json) == [violation.id]
    assert json.loads(case.audit_json)["transitions"][0]["operator"] == "synthetic-human"
    assert (
        json.loads(case.audit_json)["transitions"][0]["extra"]["reason"]
        == "new-independent-audit-note"
    )
    assert json.loads(violation.rule_hits_json)[0]["rule_id"] == "R001"
    assert await count_active_violations(session, "group", "member", provider="onebot") == 1

    await record_feedback(session, original.message_id, "false_positive", "ad", "new-human")
    await session.refresh(violation)
    assert violation.revoked
    assert await count_active_violations(session, "group", "member", provider="onebot") == 0

    late = await record_feedback(
        session,
        original.message_id,
        "confirmed_normal",
        "ad",
        "new-human",
        sample_text=RAW,
        reason=RAW,
    )
    await cleanup.purge_expired(session)
    await session.refresh(late)
    assert late.sample_text_masked == "" and RAW not in late.reason
    assert late.label == "confirmed_normal"  # A new copy cannot renew the old source's clock.


async def test_ai_cache_erases_expired_payloads_without_breaking_live_cache_or_usage(
    retention_session: AsyncSession,
) -> None:
    session = retention_session
    result = AIModerationResult(category="ad", confidence=0.99, model_id="model-v1", evidence=RAW)
    for key, created, expires in (
        ("expired", NOW - timedelta(days=2), NOW - timedelta(days=1)),
        ("over-raw-limit", OLD, NOW + timedelta(days=20)),
        ("fresh", NOW - timedelta(hours=1), NOW + timedelta(hours=23)),
    ):
        session.add(
            AICacheEntry(
                cache_key=key,
                model_id="model-v1",
                prompt_version="v7",
                result_json=result.model_dump_json(),
                created_at=created,
                expires_at=expires,
            )
        )
    usage = AIUsageLog(
        model_id="model-v1",
        group_openid="group",
        message_id="old-usage",
        created_at=OLD,
        cost_cents=5,
        source="text",
    )
    session.add(usage)
    await session.commit()

    counts = await cleanup.purge_expired(session, now=NOW)

    assert await session.get(AICacheEntry, "expired") is None
    assert await session.get(AICacheEntry, "over-raw-limit") is None
    cached = await get_cached_ai_result(session, "fresh", now=NOW)
    assert cached is not None and cached.cache_hit and cached.evidence == RAW
    assert await session.get(AIUsageLog, usage.id) is not None
    assert counts["ai_cache_deleted"] == 2


async def test_expired_feedback_stops_raw_sample_mining_but_published_rule_still_works(
    retention_session: AsyncSession,
) -> None:
    session = retention_session
    text = "合成测试推广请联系报名"
    for index in range(3):
        row = feedback(f"source-{index}")
        row.external_user_id = row.member_openid = f"member-{index}"
        row.sample_text_masked = text
        session.add(row)
    await session.commit()
    candidates = await mine_rule_candidates(session)
    assert candidates
    candidate = candidates[0]
    version_id = await copy_candidate_to_draft(session, candidate.id, operator="synthetic-human")
    await publish_rule_version(session, version_id, operator="synthetic-human")
    support_count = await session.scalar(select(func.count()).select_from(RuleCandidateExample))
    snapshot = await load_active_snapshot(session, "group")

    await cleanup.purge_expired(session, now=NOW)

    assert await mine_rule_candidates(session) == []
    retained = await session.get(RuleCandidate, candidate.id, populate_existing=True)
    assert retained is not None and retained.status == "COPIED_TO_DRAFT"
    assert retained.copied_version_id == version_id
    assert (
        await session.scalar(select(func.count()).select_from(RuleCandidateExample))
        == support_count
    )
    after = await load_active_snapshot(session, "group")
    assert after.version_ids == snapshot.version_ids
    result = DynamicRuleEngine(after).evaluate(
        StandardMessage(
            message_id="fresh-synthetic-message",
            provider="onebot",
            external_group_id="group",
            external_user_id="member-new",
            sender=Sender(member_openid="member-new"),
            text=text,
        )
    )
    assert result.verdict == "violation_high"


async def _mine_synthetic_candidate(session: AsyncSession, text: str) -> RuleCandidate:
    for index in range(3):
        row = feedback(f"candidate-source-{index}")
        row.external_user_id = row.member_openid = f"member-{index}"
        row.sample_text_masked = text
        session.add(row)
    await session.commit()
    candidates = await mine_rule_candidates(session)
    assert len(candidates) == 1
    candidate = candidates[0]
    # A recently mined copy must not renew the old source messages' retention.
    candidate.created_at = NOW - timedelta(days=1)
    await session.commit()
    return candidate


@pytest.mark.parametrize("status", ["PROPOSED", "DISMISSED"])
async def test_expired_automatic_candidate_pattern_is_erased_without_deleting_identity(
    retention_session: AsyncSession, status: str
) -> None:
    session = retention_session
    text = "合成隐私原句测试"
    candidate = await _mine_synthetic_candidate(session, text)
    candidate.status = status
    await session.commit()
    example_ids = list(
        await session.scalars(
            select(RuleCandidateExample.id).where(RuleCandidateExample.candidate_id == candidate.id)
        )
    )

    counts = await cleanup.purge_expired(session, now=NOW)
    await session.refresh(candidate)

    assert text not in candidate.pattern
    assert candidate.pattern == f"raw_retention_purged_candidate_{candidate.id}"
    assert candidate.status == "DISMISSED" and candidate.copied_version_id is None
    assert counts["candidate_patterns_purged"] == 1
    assert "candidate_patterns_purged" in maintenance._COUNT_KEYS
    assert (
        list(
            await session.scalars(
                select(RuleCandidateExample.id).where(
                    RuleCandidateExample.candidate_id == candidate.id
                )
            )
        )
        == example_ids
    )
    with pytest.raises(ValueError, match="已忽略"):
        await copy_candidate_to_draft(session, candidate.id, operator="synthetic-human")
    again = await cleanup.purge_expired(session, now=NOW)
    assert again["candidate_patterns_purged"] == 0

    # Fresh independent messages with the same words may form a NEW proposal.
    for index in range(3):
        row = feedback(f"new-source-{index}", created_at=NOW)
        row.external_user_id = row.member_openid = f"new-member-{index}"
        row.sample_text_masked = text
        session.add(row)
    await session.commit()
    renewed = await mine_rule_candidates(session)
    assert len(renewed) == 1 and renewed[0].id != candidate.id
    assert renewed[0].pattern == text and renewed[0].status == "PROPOSED"
    version_id = await copy_candidate_to_draft(session, renewed[0].id, operator="synthetic-human")
    assert version_id


@pytest.mark.parametrize("late_old_source", [False, True])
async def test_fresh_support_must_come_from_fresh_message_and_manual_candidate_is_preserved(
    retention_session: AsyncSession, late_old_source: bool
) -> None:
    session = retention_session
    text = "合成新旧共同规则"
    candidate = await _mine_synthetic_candidate(session, text)
    fresh = feedback("fresh-support", created_at=NOW)
    fresh.sample_text_masked = text
    if late_old_source:
        original = shadow("fresh-support")
        original.message_id = fresh.message_id
        session.add(original)
    manual = RuleCandidate(
        scope="group",
        scope_key="group",
        item_type="keyword",
        pattern="人工政策不可清理",
        category="ad",
        status="PROPOSED",
        generated_by="synthetic-human",
        created_at=OLD,
    )
    session.add_all([fresh, manual])
    await session.commit()

    counts = await cleanup.purge_expired(session, now=NOW)

    await session.refresh(candidate)
    await session.refresh(manual)
    if late_old_source:
        assert text not in candidate.pattern and candidate.status == "DISMISSED"
    else:
        assert candidate.pattern == text and candidate.status == "PROPOSED"
    assert manual.pattern == "人工政策不可清理" and manual.status == "PROPOSED"
    assert counts["candidate_patterns_purged"] == int(late_old_source)


@pytest.mark.parametrize("candidate_is_old", [True, False])
async def test_dismissed_candidate_without_current_support_index_uses_source_age(
    retention_session: AsyncSession, candidate_is_old: bool
) -> None:
    session = retention_session
    text = "合成已驳回原句"
    candidate = await _mine_synthetic_candidate(session, text)
    candidate.created_at = OLD if candidate_is_old else NOW - timedelta(days=1)
    for row in await session.scalars(select(FeedbackRecord)):
        row.label = "confirmed_normal"
    await session.commit()
    await mine_rule_candidates(session)  # Reconciliation removes its current support index.
    assert candidate.status == "DISMISSED"
    assert await session.scalar(select(func.count()).select_from(RuleCandidateExample)) == 0
    different_sources = []
    for changed in (
        {"provider": "qq_official"},
        {"scope_key": "other-group"},
        {"item_type": "domain"},
        {"category": "other"},
    ):
        row = RuleCandidate(
            scope="group",
            scope_key=changed.get("scope_key", "group"),
            item_type=changed.get("item_type", "keyword"),
            pattern=text,
            category=changed.get("category", "ad"),
            status="DISMISSED",
            created_at=NOW - timedelta(days=1),
            replay_report_json=json.dumps({"provider": changed.get("provider", "onebot")}),
        )
        session.add(row)
        different_sources.append(row)
    await session.commit()

    counts = await cleanup.purge_expired(session, now=NOW)

    await session.refresh(candidate)
    assert text not in candidate.pattern
    assert counts["candidate_patterns_purged"] == 1
    for row in different_sources:
        await session.refresh(row)
        assert row.pattern == text
