"""Lifecycle cleanup must not turn UI hiding into destructive evidence loss."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from app.cases.models import Case, ViolationRecord
from app.db import Base
from app.models import SystemSetting
from app.moderation.ai import AIUsageLog
from app.moderation.feedback import RuleCandidate
from app.reports import cleanup, maintenance
from app.runtime import pipeline
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

NOW = datetime(2031, 9, 14, 12, tzinfo=UTC)
OLD = NOW - timedelta(days=120)
RAW = "synthetic-original-content"


@pytest.fixture
async def lifecycle_session(
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


def case(number: int, *, status: str = "CLOSED", references: str = "[]") -> Case:
    return Case(
        id=number,
        case_no=f"R-LIFECYCLE-{number}",
        provider="onebot",
        group_openid="synthetic-group",
        member_openid="synthetic-member",
        external_group_id="synthetic-group",
        external_user_id="synthetic-member",
        status=status,
        archived=True,
        created_at=OLD,
        closed_at=OLD if status == "CLOSED" else None,
        archived_at=NOW - timedelta(days=91),
        violation_ids_json=references,
        audit_json=json.dumps(
            {
                "evidence_count": 1,
                "transitions": [
                    {
                        "from": "KEEP",
                        "to": "CLOSED",
                        "operator": "test-admin",
                        "at": OLD.isoformat(),
                        "extra": {"original_message": RAW},
                    }
                ],
                "other_original_copy": RAW,
            }
        ),
    )


def violation(number: int, *, created_at: datetime = OLD, case_id: int = 1) -> ViolationRecord:
    return ViolationRecord(
        id=number,
        provider="onebot",
        group_openid="synthetic-group",
        member_openid="synthetic-member",
        external_group_id="synthetic-group",
        external_user_id="synthetic-member",
        message_id=f"synthetic-message-{number}",
        category="ad",
        confidence=0.99,
        case_id=case_id,
        created_at=created_at,
        message_snapshot_json=json.dumps({"text": RAW}),
    )


async def test_closed_case_archive_starts_at_thirty_full_days(
    lifecycle_session: AsyncSession,
) -> None:
    session = lifecycle_session
    recent = case(1)
    recent.archived = False
    recent.archived_at = None
    recent.closed_at = NOW - timedelta(days=30) + timedelta(seconds=1)
    boundary = case(2)
    boundary.archived = False
    boundary.archived_at = None
    boundary.closed_at = NOW - timedelta(days=30)
    older = case(3)
    older.archived = False
    older.archived_at = None
    older.closed_at = NOW - timedelta(days=31)
    pending = case(4, status="PENDING_REVIEW")
    pending.archived = False
    pending.archived_at = None
    previously_archived = case(5)
    previously_archived.closed_at = NOW - timedelta(days=16)
    previously_archived.archived_at = NOW - timedelta(days=1)
    session.add_all([recent, boundary, older, pending, previously_archived])
    await session.commit()
    recent_id, boundary_id, older_id, pending_id, previously_archived_id = (
        recent.id,
        boundary.id,
        older.id,
        pending.id,
        previously_archived.id,
    )

    result = await cleanup.purge_expired(session, NOW)
    session.expire_all()
    assert result["cases_archived"] == 2
    assert (await session.get(Case, recent_id)).archived is False
    assert (await session.get(Case, boundary_id)).archived is True
    assert (await session.get(Case, older_id)).archived is True
    assert (await session.get(Case, pending_id)).archived is False
    assert (await session.get(Case, previously_archived_id)).archived is True


@pytest.mark.parametrize("status", ["PENDING_REVIEW", "MANUAL_PENDING", "EXECUTING", "FAILED"])
async def test_old_archive_does_not_authorize_clearing_unfinished_case(
    lifecycle_session: AsyncSession, status: str
) -> None:
    session = lifecycle_session
    session.add_all([case(1, status=status, references="[1]"), violation(1)])
    await session.commit()
    result = await cleanup.purge_expired(session, NOW)
    session.expire_all()
    stored = await session.get(Case, 1)
    assert stored.violation_ids_json == "[1]"
    assert json.loads(stored.audit_json)["transitions"]
    assert await session.get(ViolationRecord, 1) is not None
    assert result["cases_purged"] == 0


@pytest.mark.parametrize("reference_kind", ["json", "case_id", "malformed_other_json"])
async def test_case_cannot_purge_records_still_needed_by_other_case(
    lifecycle_session: AsyncSession, reference_kind: str
) -> None:
    session = lifecycle_session
    other = case(
        2,
        status="PENDING_REVIEW",
        references={"json": "[1]", "case_id": "[]", "malformed_other_json": "[1,"}[reference_kind],
    )
    other.archived = False
    record = violation(1, case_id=2 if reference_kind == "case_id" else 1)
    session.add_all([case(1, references="[1]"), other, record])
    await session.commit()
    result = await cleanup.purge_expired(session, NOW)
    session.expire_all()
    assert await session.get(ViolationRecord, 1) is not None
    assert (await session.get(Case, 1)).violation_ids_json == "[1]"
    assert result["cases_purged"] == 0


async def test_recent_violation_in_old_case_keeps_repeat_count_metadata(
    lifecycle_session: AsyncSession,
) -> None:
    session = lifecycle_session
    session.add_all([case(1, references="[1]"), violation(1, created_at=NOW - timedelta(days=1))])
    await session.commit()
    result = await cleanup.purge_expired(session, NOW)
    session.expire_all()
    stored = await session.get(ViolationRecord, 1)
    assert stored is not None
    assert stored.revoked is False
    assert json.loads(stored.message_snapshot_json)["text"] == RAW
    assert (await session.get(Case, 1)).violation_ids_json == "[1]"
    assert result["cases_purged"] == 0


@pytest.mark.parametrize("references", ["[true]", "[1.1]", '["1"]', '{"1": true}', "null", "["])
async def test_malformed_reference_document_never_selects_deletion_targets(
    lifecycle_session: AsyncSession, references: str
) -> None:
    session = lifecycle_session
    session.add_all([case(1, references=references), violation(1)])
    await session.commit()
    result = await cleanup.purge_expired(session, NOW)
    session.expire_all()
    assert await session.get(ViolationRecord, 1) is not None
    assert (await session.get(Case, 1)).violation_ids_json == references
    assert result["cases_purged"] == 0


async def test_logical_clear_preserves_audit_chain_ids_and_is_idempotent(
    lifecycle_session: AsyncSession,
) -> None:
    session = lifecycle_session
    session.add_all([case(1, references="[1]"), violation(1)])
    await session.commit()
    first = await cleanup.purge_expired(session, NOW)
    session.expire_all()
    stored = await session.get(Case, 1)
    audit = json.loads(stored.audit_json)
    assert audit["transitions"][0]["operator"] == "test-admin"
    assert audit["transitions"][0]["from"] == "KEEP"
    assert audit["transitions"][0]["to"] == "CLOSED"
    assert audit["lifecycle_purged_at"]
    assert RAW not in stored.audit_json
    # No row-ID reuse can attach a historical audit to an unrelated new strike.
    record = await session.get(ViolationRecord, 1)
    assert record is not None
    assert json.loads(record.message_snapshot_json)["purged"] is True
    assert first["cases_purged"] == 1
    assert first["violation_records_purged"] == 0
    second = await cleanup.purge_expired(session, NOW + timedelta(days=1))
    assert second["cases_purged"] == 0


async def test_logical_clear_keeps_prior_confirmed_case_reference(
    lifecycle_session: AsyncSession,
) -> None:
    session = lifecycle_session
    new_case = case(2)
    audit = json.loads(new_case.audit_json)
    audit["prior_case_id"] = 1
    new_case.audit_json = json.dumps(audit)
    session.add(new_case)
    await session.commit()

    await cleanup.purge_expired(session, NOW)
    await session.refresh(new_case)
    assert json.loads(new_case.audit_json)["prior_case_id"] == 1


async def test_candidate_expiry_preserves_scope_and_previous_replay_metadata(
    lifecycle_session: AsyncSession,
) -> None:
    session = lifecycle_session
    candidate = RuleCandidate(
        scope="group",
        scope_key="synthetic-group",
        item_type="keyword",
        pattern="synthetic-reviewed-rule",
        generated_by="test-admin",
        created_at=NOW - timedelta(days=31),
        replay_report_json=json.dumps({"provider": "onebot", "positive_count": 3}),
    )
    session.add(candidate)
    await session.commit()
    result = await cleanup.purge_expired(session, NOW)
    await session.refresh(candidate)
    assert candidate.status == "DISMISSED"
    report = json.loads(candidate.replay_report_json)
    assert report["provider"] == "onebot"
    assert report["positive_count"] == 3
    assert report["invalidation_reason"] == "expired_30d_unhandled"
    assert result["candidates_expired"] == 1


async def test_reverse_case_link_protects_recent_records_not_in_original_json_list(
    lifecycle_session: AsyncSession,
) -> None:
    session = lifecycle_session
    session.add_all([case(1), violation(1, created_at=NOW - timedelta(days=1))])
    await session.commit()
    result = await cleanup.purge_expired(session, NOW)
    session.expire_all()
    assert "lifecycle_purged_at" not in json.loads((await session.get(Case, 1)).audit_json)
    assert result["cases_cleanup_deferred"] == 1
    assert result["cases_purged"] == 0


@pytest.mark.parametrize("audit", ["null", "[1]", "{"])
async def test_broken_audit_is_reported_as_deferred_not_erased(
    lifecycle_session: AsyncSession, audit: str
) -> None:
    session = lifecycle_session
    damaged = case(1)
    damaged.audit_json = audit
    session.add(damaged)
    await session.commit()
    result = await cleanup.purge_expired(session, NOW)
    await session.refresh(damaged)
    assert damaged.audit_json == audit
    assert result["cases_cleanup_deferred"] == 1


@pytest.mark.parametrize("closed_days_ago", [None, 0, 15])
async def test_archive_timestamp_does_not_override_missing_or_recent_closure(
    lifecycle_session: AsyncSession, closed_days_ago: int | None
) -> None:
    session = lifecycle_session
    damaged = case(1)
    damaged.closed_at = None if closed_days_ago is None else NOW - timedelta(days=closed_days_ago)
    session.add(damaged)
    await session.commit()
    result = await cleanup.purge_expired(session, NOW)
    assert result["cases_cleanup_deferred"] == 1
    assert result["cases_purged"] == 0


async def test_archive_cannot_predate_actual_case_closure(
    lifecycle_session: AsyncSession,
) -> None:
    session = lifecycle_session
    damaged = case(1, references="[1]")
    damaged.closed_at = NOW - timedelta(days=20)
    session.add_all([damaged, violation(1, created_at=NOW - timedelta(days=31))])
    await session.commit()
    result = await cleanup.purge_expired(session, NOW)
    await session.refresh(damaged)
    assert damaged.violation_ids_json == "[1]"
    assert "lifecycle_purged_at" not in json.loads(damaged.audit_json)
    assert result["cases_cleanup_deferred"] == 1
    assert result["cases_purged"] == 0


async def test_valid_archive_timeline_still_allows_logical_clear(
    lifecycle_session: AsyncSession,
) -> None:
    session = lifecycle_session
    completed = case(1, references="[1]")
    completed.closed_at = NOW - timedelta(days=105)
    session.add_all([completed, violation(1)])
    await session.commit()
    result = await cleanup.purge_expired(session, NOW)
    await session.refresh(completed)
    assert json.loads(completed.audit_json)["lifecycle_purged_at"]
    assert result["cases_purged"] == 1
    assert result["cases_cleanup_deferred"] == 0


async def test_raw_ttl_longer_than_archive_age_prevents_premature_case_clear(
    lifecycle_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = lifecycle_session
    settings = cleanup.get_settings().model_copy(update={"raw_retention_days": 180})
    monkeypatch.setattr(cleanup, "get_settings", lambda: settings)
    session.add_all([case(1, references="[1]"), violation(1)])
    await session.commit()
    result = await cleanup.purge_expired(session, NOW)
    await session.refresh(await session.get(ViolationRecord, 1))
    assert result["cases_purged"] == 0
    assert result["cases_cleanup_deferred"] == 1
    assert json.loads((await session.get(ViolationRecord, 1)).message_snapshot_json)["text"] == RAW


async def test_maintenance_accepts_and_persists_deferred_case_count(
    lifecycle_session: AsyncSession,
) -> None:
    session = lifecycle_session
    session.add(SystemSetting(key="auto_cleanup_enabled", value="1"))
    await session.commit()
    result = await maintenance.run_cleanup(session)
    assert result["status"] == "succeeded"
    assert result["counts"]["cases_cleanup_deferred"] == 0
    stored = await session.get(SystemSetting, "last_cleanup_result")
    assert json.loads(stored.value)["cases_cleanup_deferred"] == 0


async def test_cleanup_uses_utc_instant_for_retention_boundary(
    lifecycle_session: AsyncSession,
) -> None:
    session = lifecycle_session
    boundary = AIUsageLog(
        message_id="synthetic-boundary",
        model_id="test-model",
        source="text",
        group_openid="synthetic-group",
        created_at=NOW - timedelta(days=90) + timedelta(hours=1),
    )
    session.add(boundary)
    await session.commit()
    boundary_id = boundary.id
    result = await cleanup.purge_expired(session, NOW.astimezone(timezone(timedelta(hours=8))))
    session.expire_all()
    assert await session.get(AIUsageLog, boundary_id) is not None
    assert result["ai_usage_logs_deleted"] == 0
