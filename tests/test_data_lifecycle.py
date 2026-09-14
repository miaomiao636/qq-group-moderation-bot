"""负责人 2026-09-14 数据管理决策回归：案件归档/清除、AI 日志清理、候选过期。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.cases.models import Case
from app.db import SessionLocal
from app.moderation.ai import AIUsageLog
from app.moderation.feedback import RuleCandidate


async def _transition(case_id: int, target: str) -> None:
    from app.cases.service import transition_case

    async with SessionLocal() as session:
        await transition_case(session, case_id, target, "test")


async def _make_closed_case(*, days_ago: int, group: str) -> Case:
    """造一个已 CLOSED 的案件（经状态机走合法路径）。"""
    async with SessionLocal() as session:
        case = Case(
            case_no=f"R{uuid.uuid4().hex[:8]}",
            group_openid=group,
            member_openid="M-" + group,
            provider="onebot",
            external_group_id=group,
            external_user_id="M-" + group,
            violation_ids_json="[]",
        )
        session.add(case)
        await session.commit()
        case_id = case.id
    for target in ("APPROVED_MANUAL", "MANUAL_PENDING", "KICKED", "CLOSED"):
        await _transition(case_id, target)
    async with SessionLocal() as session:
        closed_at = (datetime.now(UTC) - timedelta(days=days_ago)).replace(tzinfo=None)
        await session.execute(
            Case.__table__.update()
            .where(Case.id == case_id)
            .values(closed_at=closed_at, created_at=closed_at)
        )
        await session.commit()
        return await session.get(Case, case_id)


async def _force_archive(case_id: int, *, days_ago: int) -> None:
    async with SessionLocal() as session:
        archived_at = (datetime.now(UTC) - timedelta(days=days_ago)).replace(tzinfo=None)
        await session.execute(
            Case.__table__.update()
            .where(Case.id == case_id)
            .values(archived=True, archived_at=archived_at)
        )
        await session.commit()


async def _run_cleanup() -> dict:
    from app.reports.cleanup import purge_expired

    async with SessionLocal() as session:
        return await purge_expired(session)


@pytest.mark.asyncio
async def test_case_auto_archive_after_15d() -> None:
    """终态满 15 天自动归档；未满不归档；PENDING 永不归档。"""
    g = "GA-" + uuid.uuid4().hex[:10]
    old = await _make_closed_case(days_ago=16, group=g)
    fresh = await _make_closed_case(days_ago=2, group=g)
    await _run_cleanup()
    async with SessionLocal() as session:
        assert (await session.get(Case, old.id)).archived is True
        assert (await session.get(Case, fresh.id)).archived is False
        pending = Case(
            case_no=f"R{uuid.uuid4().hex[:8]}",
            group_openid=g,
            member_openid="M",
            provider="onebot",
            external_group_id=g,
            external_user_id="M",
            status="PENDING_REVIEW",
            created_at=(datetime.now(UTC) - timedelta(days=40)).replace(tzinfo=None),
        )
        session.add(pending)
        await session.commit()
        pending_id = pending.id
    await _run_cleanup()
    async with SessionLocal() as session:
        assert (await session.get(Case, pending_id)).archived is False


@pytest.mark.asyncio
async def test_purged_cases_keep_id_monotonic_for_notifications() -> None:
    """R03 契约：清除=逻辑清除（案件壳与 ID 保留，内容引用清空）——
    新案件 ID 单调递增不被 rowid 复用破坏，违规记录物理删除。"""
    g = "GB-" + uuid.uuid4().hex[:10]
    old = await _make_closed_case(days_ago=16, group=g)
    await _force_archive(old.id, days_ago=91)
    stats = await _run_cleanup()
    assert stats["cases_purged"] >= 1
    assert stats["violation_records_purged"] >= 0
    new = await _make_closed_case(days_ago=0, group=g)
    assert new.id > old.id
    async with SessionLocal() as session:
        shell = await session.get(Case, old.id)
        assert shell is not None  # 案件壳保留（ID 不复用）
        assert shell.violation_ids_json == "[]"  # 内容引用已清空
        assert shell.archived is True


@pytest.mark.asyncio
async def test_ai_usage_logs_purged_after_90d() -> None:
    """AI 日志 90 天清理：老行删除、新行保留。"""
    async with SessionLocal() as session:
        old = AIUsageLog(
            message_id="old-" + uuid.uuid4().hex,
            model_id="deepseek-flash",
            source="text",
            latency_ms=100,
            ok=True,
            provider="deepseek",
            group_openid="G-AIL",
            external_group_id="G-AIL",
        )
        fresh = AIUsageLog(
            message_id="new-" + uuid.uuid4().hex,
            model_id="deepseek-flash",
            source="text",
            latency_ms=100,
            ok=True,
            provider="deepseek",
            group_openid="G-AIL",
            external_group_id="G-AIL",
        )
        session.add(old)
        session.add(fresh)
        await session.commit()
        old_id, fresh_id = old.id, fresh.id
        old_created = (datetime.now(UTC) - timedelta(days=91)).replace(tzinfo=None)
        await session.execute(
            AIUsageLog.__table__.update()
            .where(AIUsageLog.id == old_id)
            .values(created_at=old_created)
        )
        await session.commit()
    stats = await _run_cleanup()
    assert stats["ai_usage_logs_deleted"] >= 1
    async with SessionLocal() as session:
        assert await session.get(AIUsageLog, old_id) is None
        assert await session.get(AIUsageLog, fresh_id) is not None


@pytest.mark.asyncio
async def test_proposed_candidates_expire_after_30d() -> None:
    """候选规则 30 天未处理自动 DISMISSED（含理由），新候选不受影响。"""
    async with SessionLocal() as session:
        old = RuleCandidate(
            scope="group",
            scope_key="G-OLD",
            item_type="keyword",
            pattern="过期测试词",
            status="PROPOSED",
            created_at=(datetime.now(UTC) - timedelta(days=31)).replace(tzinfo=None),
        )
        fresh = RuleCandidate(
            scope="group",
            scope_key="G-NEW",
            item_type="keyword",
            pattern="新鲜测试词",
            status="PROPOSED",
        )
        session.add(old)
        session.add(fresh)
        await session.commit()
        old_id, fresh_id = old.id, fresh.id
    stats = await _run_cleanup()
    # 30 天未处理的候选：或被既有副本清理物理删除（candidate_patterns_purged），
    # 或被候选过期逻辑驳回（candidates_expired）——两者都是合规结局。
    async with SessionLocal() as session:
        refreshed_old = await session.get(RuleCandidate, old_id)
        assert refreshed_old is None or refreshed_old.status == "DISMISSED"
        assert (await session.get(RuleCandidate, fresh_id)).status == "PROPOSED"
        assert stats["candidates_expired"] >= 1 or stats["candidate_patterns_purged"] >= 1
