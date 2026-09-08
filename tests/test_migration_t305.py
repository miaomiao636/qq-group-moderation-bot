"""T-305 迁移与混合版本兼容测试：回填幂等、混合行兼容读取、双写。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.adapters.qq_official.contract import Sender, StandardMessage
from app.cases.models import Case
from app.cases.service import count_active_violations, record_violation
from app.core.identity_backfill import NEUTRAL_BACKFILL_STATEMENTS
from app.db import SessionLocal
from app.moderation.decision import ModerationDecision
from sqlalchemy import text


async def _execute_backfill(session) -> None:
    for statement in NEUTRAL_BACKFILL_STATEMENTS:
        await session.execute(text(statement))
    await session.commit()


@pytest.mark.asyncio
async def test_backfill_fills_legacy_only_rows() -> None:
    """迁移回填：仅含旧镜像列的行（模拟混合版本期间旧代码写入）被补齐。"""
    marker = f"LEGACY_{uuid.uuid4().hex[:8]}"
    group = f"G_BACKFILL_{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        # 直接以旧版本代码的写入形态落库（不写中立列）
        await session.execute(
            text(
                "INSERT INTO shadow_decisions "
                "(message_id, group_openid, member_openid, sender_name, kind, verdict, "
                " category, confidence, reason, detail_json, created_at) "
                "VALUES (:mid, :gid, :uid, '', 'text', 'record_only', '', 0, '', '{}', "
                " CURRENT_TIMESTAMP)"
            ),
            {"mid": marker, "gid": group, "uid": "U_LEGACY"},
        )
        await _execute_backfill(session)
        row = (
            await session.execute(
                text(
                    "SELECT provider, external_group_id, external_user_id "
                    "FROM shadow_decisions WHERE message_id = :mid"
                ),
                {"mid": marker},
            )
        ).one()
    assert tuple(row) == ("qq_official", group, "U_LEGACY")


@pytest.mark.asyncio
async def test_backfill_is_idempotent_and_preserves_neutral_values() -> None:
    """回填幂等：已写入的中立值（含 onebot 数据）不被覆盖。"""
    marker = f"ONEBOT_KEEP_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO shadow_decisions "
                "(message_id, group_openid, member_openid, provider, external_group_id, "
                " external_user_id, sender_name, kind, verdict, category, confidence, "
                " reason, detail_json, created_at) "
                "VALUES (:mid, '300000001', '200000001', 'onebot', '300000001', "
                " '200000001', '', 'text', 'record_only', '', 0, '', '{}', CURRENT_TIMESTAMP)"
            ),
            {"mid": marker},
        )
        await _execute_backfill(session)
        await _execute_backfill(session)  # 第二次执行不改变任何值
        row = (
            await session.execute(
                text("SELECT provider FROM shadow_decisions WHERE message_id = :mid"),
                {"mid": marker},
            )
        ).one()
    assert row.provider == "onebot"


@pytest.mark.asyncio
async def test_hybrid_version_rows_are_visible_to_neutral_queries() -> None:
    """混合版本兼容：仅有旧镜像列的历史违规行仍被中立口径查询命中。"""
    group = f"G_HYBRID_{uuid.uuid4().hex[:6]}"
    marker = f"HYBRID_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO violation_records "
                "(group_openid, member_openid, message_id, category, confidence, "
                " rule_hits_json, message_snapshot_json, action_result_json, "
                " revoked, revoke_reason, created_at) "
                "VALUES (:gid, 'U_HYBRID', :mid, 'ad', 0.95, '[]', '{}', '[]', "
                " 0, '', :created)"
            ),
            {
                "gid": group,
                "mid": marker,
                "created": datetime.now(UTC) - timedelta(days=1),
            },
        )
        await session.commit()
        await _execute_backfill(session)
        count = await count_active_violations(session, group, "U_HYBRID")
    assert count == 1


@pytest.mark.asyncio
async def test_new_writes_are_dual_written() -> None:
    """新代码写入：中立列与旧镜像列同时落库（expand 阶段读写兼容）。"""
    group = f"G_DUAL_{uuid.uuid4().hex[:6]}"
    msg = StandardMessage(
        message_id=f"DUAL_{uuid.uuid4().hex[:8]}",
        provider="onebot",
        external_group_id=group,
        external_user_id="200000007",
        group_openid=group,
        sender=Sender(member_openid="200000007", role="member"),
        text="测试双写",
    )
    decision = ModerationDecision(
        message_id=msg.message_id,
        provider="onebot",
        external_group_id=group,
        external_user_id="200000007",
        sender_member_openid=msg.sender.member_openid,
        sender_role="member",
        verdict="violation_high",
        category="ad",
        confidence=0.95,
        recommended_actions=["recall"],
    )
    async with SessionLocal() as session:
        outcome = await record_violation(session, msg, decision)
        violation = outcome.violation
        assert violation.provider == "onebot"
        assert violation.external_group_id == group
        assert violation.external_user_id == "200000007"
        assert violation.group_openid == group
        assert violation.member_openid == "200000007"
        if outcome.case is not None:
            case: Case = outcome.case
            assert case.provider == "onebot"
            assert case.external_group_id == group
        # 镜像口径仍可查询（web后台/报告依赖）
        legacy = (
            await session.execute(
                text(
                    "SELECT count(*) FROM violation_records "
                    "WHERE group_openid = :gid AND member_openid = '200000007'"
                ),
                {"gid": group},
            )
        ).scalar_one()
    assert legacy == 1
