"""T-104 测试：两次违规状态机、30天窗口、白名单保护、误判撤销、案件双出口互斥。"""

from __future__ import annotations

import uuid

import pytest
from app.adapters.qq_official.contract import Sender, StandardMessage
from app.cases.case_sm import IllegalTransitionError, validate_transition
from app.cases.service import (
    ViolationOutcome,
    count_active_violations,
    record_violation,
    revoke_violation,
    transition_case,
)
from app.moderation.decision import ModerationDecision


def _ids() -> tuple[str, str]:
    """每个用例独立的群/成员标识，避免共享测试库导致窗口计数互相污染。"""
    suffix = uuid.uuid4().hex[:8]
    return f"GROUP_{suffix}", f"MEMBER_{suffix}"


def high_decision(
    message_id: str, group: str, member: str, role: str = "member"
) -> ModerationDecision:
    return ModerationDecision(
        message_id=message_id,
        group_openid=group,
        sender_member_openid=member,
        sender_role=role,
        verdict="violation_high",
        category="ad",
        confidence=0.95,
        reason="测试决策",
        is_protected_sender=role in ("owner", "admin"),
    )


def make_message(message_id: str, group: str, member: str, role: str = "member") -> StandardMessage:
    return StandardMessage(
        message_id=message_id,
        group_openid=group,
        sender=Sender(member_openid=member, role=role),  # type: ignore[arg-type]
        text="测试违规内容",
    )


@pytest.mark.asyncio
async def test_first_strike_plan() -> None:
    from app.db import SessionLocal

    group, member = _ids()
    async with SessionLocal() as session:
        outcome = await record_violation(
            session,
            make_message("CASE_MSG_1", group, member),
            high_decision("CASE_MSG_1", group, member),
        )
        assert isinstance(outcome, ViolationOutcome)
        assert outcome.strike_no == 1
        actions = {p.action for p in outcome.planned_actions}
        assert actions == {"recall", "mute", "warn"}
        assert outcome.case is None
        mute = next(p for p in outcome.planned_actions if p.action == "mute")
        assert mute.params["seconds"] == 3600


@pytest.mark.asyncio
async def test_second_strike_creates_case_and_no_kick() -> None:
    from app.db import SessionLocal

    group, member = _ids()
    async with SessionLocal() as session:
        await record_violation(
            session,
            make_message("CASE_MSG_S2_1", group, member),
            high_decision("CASE_MSG_S2_1", group, member),
        )
        outcome = await record_violation(
            session,
            make_message("CASE_MSG_S2_2", group, member),
            high_decision("CASE_MSG_S2_2", group, member),
        )
        assert outcome.strike_no == 2
        actions = {p.action for p in outcome.planned_actions}
        assert "warn" not in actions  # 第二次不警告
        assert "kick" not in actions  # 永不自动踢人
        assert outcome.case is not None
        assert outcome.case.status == "PENDING_REVIEW"
        assert outcome.case.case_no.startswith("R")


@pytest.mark.asyncio
async def test_window_excludes_old_violations() -> None:
    from datetime import UTC, datetime, timedelta

    from app.cases.models import ViolationRecord
    from app.db import SessionLocal

    group, member = _ids()
    async with SessionLocal() as session:
        session.add(
            ViolationRecord(
                group_openid=group,
                member_openid=member,
                message_id="OLD_MSG",
                category="ad",
                confidence=0.95,
                created_at=datetime.now(UTC) - timedelta(days=31),
            )
        )
        await session.commit()
        outcome = await record_violation(
            session,
            make_message("CASE_MSG_W1", group, member),
            high_decision("CASE_MSG_W1", group, member),
        )
        assert outcome.strike_no == 1  # 窗口外违规不计数


@pytest.mark.asyncio
async def test_revoked_violation_excluded_from_window() -> None:
    from app.db import SessionLocal

    group, member = _ids()
    async with SessionLocal() as session:
        first = await record_violation(
            session,
            make_message("CASE_MSG_R1", group, member),
            high_decision("CASE_MSG_R1", group, member),
        )
        await revoke_violation(session, first.violation.id, "误判", operator="admin_test")
        assert await count_active_violations(session, group, member) == 0
        outcome = await record_violation(
            session,
            make_message("CASE_MSG_R2", group, member),
            high_decision("CASE_MSG_R2", group, member),
        )
        assert outcome.strike_no == 1  # 撤销后重新计为第1次


@pytest.mark.asyncio
async def test_strikes_are_isolated_by_transport_provider() -> None:
    """相同的外部群/成员字符串在不同通道中不是同一身份。"""
    from app.db import SessionLocal

    group, member = _ids()
    official = make_message("SAME_MSG", group, member)
    onebot = StandardMessage(
        message_id="SAME_MSG",
        provider="onebot",
        external_group_id=group,
        external_user_id=member,
        sender=Sender(member_openid=member),
        text="测试违规内容",
    )
    onebot_decision = ModerationDecision(
        message_id=onebot.message_id,
        provider="onebot",
        external_group_id=group,
        external_user_id=member,
        verdict="violation_high",
        category="ad",
        confidence=0.95,
    )
    async with SessionLocal() as session:
        first_official = await record_violation(
            session, official, high_decision("SAME_MSG", group, member)
        )
        first_onebot = await record_violation(session, onebot, onebot_decision)

    assert first_official.strike_no == 1
    assert first_onebot.strike_no == 1


@pytest.mark.asyncio
async def test_revoke_all_case_violations_closes_case() -> None:
    from app.cases.models import Case
    from app.db import SessionLocal

    group, member = _ids()
    async with SessionLocal() as session:
        o1 = await record_violation(
            session,
            make_message("CASE_MSG_C1", group, member),
            high_decision("CASE_MSG_C1", group, member),
        )
        o2 = await record_violation(
            session,
            make_message("CASE_MSG_C2", group, member),
            high_decision("CASE_MSG_C2", group, member),
        )
        assert o2.case is not None
        case_id = o2.case.id
        await revoke_violation(session, o1.violation.id, "误判A", operator="admin_test")
        await revoke_violation(session, o2.violation.id, "误判B", operator="admin_test")
        case = await session.get(Case, case_id)
        assert case is not None
        assert case.status == "CLOSED"


@pytest.mark.asyncio
async def test_protected_sender_rejected() -> None:
    from app.db import SessionLocal

    group, member = _ids()
    async with SessionLocal() as session:
        for role in ("owner", "admin"):
            with pytest.raises(ValueError, match="保护角色"):
                await record_violation(
                    session,
                    make_message(f"CASE_MSG_P_{role}", group, member, role),
                    high_decision(f"CASE_MSG_P_{role}", group, member, role),
                )


@pytest.mark.asyncio
async def test_only_high_verdict_allowed() -> None:
    from app.db import SessionLocal

    group, member = _ids()
    decision = ModerationDecision(
        message_id="M",
        group_openid=group,
        sender_member_openid=member,
        verdict="record_only",
        confidence=0.4,
    )
    async with SessionLocal() as session:
        with pytest.raises(ValueError, match="violation_high"):
            await record_violation(session, make_message("M", group, member), decision)


# ---------- 案件状态机 ----------


def test_state_machine_happy_paths() -> None:
    # 人工出口
    validate_transition("PENDING_REVIEW", "APPROVED_MANUAL")
    validate_transition("APPROVED_MANUAL", "MANUAL_PENDING")
    validate_transition("MANUAL_PENDING", "KICKED")
    validate_transition("KICKED", "CLOSED")
    # NapCat 出口
    validate_transition("PENDING_REVIEW", "APPROVED_NAPCAT")
    validate_transition("APPROVED_NAPCAT", "CONFIRM_PENDING")
    validate_transition("CONFIRM_PENDING", "EXECUTING")
    validate_transition("EXECUTING", "KICKED")
    # 保留 / 误判
    validate_transition("PENDING_REVIEW", "KEEP")
    validate_transition("PENDING_REVIEW", "FALSE_POSITIVE")
    validate_transition("FAILED", "MANUAL_PENDING")


def test_state_machine_rejects_invalid() -> None:
    with pytest.raises(IllegalTransitionError):
        validate_transition("PENDING_REVIEW", "KICKED")  # 不能跳过审批直接踢
    with pytest.raises(IllegalTransitionError):
        validate_transition("PENDING_REVIEW", "EXECUTING")
    with pytest.raises(IllegalTransitionError):
        validate_transition("EXECUTING", "APPROVED_MANUAL")  # 双出口互斥
    with pytest.raises(IllegalTransitionError):
        validate_transition("MANUAL_PENDING", "APPROVED_NAPCAT")  # 双出口互斥
    with pytest.raises(IllegalTransitionError):
        validate_transition("FAILED", "EXECUTING")  # NapCat失败不可重试NapCat
    with pytest.raises(IllegalTransitionError):
        validate_transition("CLOSED", "PENDING_REVIEW")  # 终态不可回退


@pytest.mark.asyncio
async def test_case_transition_via_service_audited() -> None:
    from app.cases.models import Case
    from app.db import SessionLocal

    group, member = _ids()
    async with SessionLocal() as session:
        await record_violation(
            session,
            make_message("CASE_MSG_T1", group, member),
            high_decision("CASE_MSG_T1", group, member),
        )
        o2 = await record_violation(
            session,
            make_message("CASE_MSG_T2", group, member),
            high_decision("CASE_MSG_T2", group, member),
        )
        assert o2.case is not None
        case_id = o2.case.id
        case = await transition_case(session, case_id, "APPROVED_MANUAL", operator="admin_test")
        assert case.status == "APPROVED_MANUAL"
        case = await transition_case(session, case_id, "MANUAL_PENDING", operator="admin_test")
        assert case.status == "MANUAL_PENDING"
        with pytest.raises(IllegalTransitionError):
            await transition_case(session, case_id, "APPROVED_NAPCAT", operator="admin_test")
        fresh = await session.get(Case, case_id)
        assert fresh is not None and fresh.status == "MANUAL_PENDING"
