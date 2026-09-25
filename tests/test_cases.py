"""T-104 测试：仅自动撤回、再犯待审、违规历史与案件审计。"""

from __future__ import annotations

import json
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
        actions = [p.action for p in outcome.planned_actions]
        assert actions == ["recall"]
        assert outcome.case is None


@pytest.mark.asyncio
async def test_second_strike_only_recalls_and_creates_review_case() -> None:
    from app.cases.models import Case
    from app.db import SessionLocal
    from sqlalchemy import func, select

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
        actions = [p.action for p in outcome.planned_actions]
        assert actions == ["recall"]
        assert outcome.case is not None
        assert outcome.case.status == "PENDING_REVIEW"
        assert outcome.violation.case_id == outcome.case.id
        assert (
            await session.execute(
                select(func.count()).select_from(Case).where(Case.group_openid == group)
            )
        ).scalar_one() == 1


@pytest.mark.asyncio
async def test_after_manual_close_next_violation_gets_new_case_with_only_new_evidence() -> None:
    from app.db import SessionLocal

    group, member = _ids()
    async with SessionLocal() as session:
        first = await record_violation(
            session,
            make_message("CASE_REOPEN_1", group, member),
            high_decision("CASE_REOPEN_1", group, member),
        )
        second = await record_violation(
            session,
            make_message("CASE_REOPEN_2", group, member),
            high_decision("CASE_REOPEN_2", group, member),
        )
        assert second.case is not None
        old_case_id = second.case.id
        await transition_case(session, old_case_id, "KEEP", operator="admin_test")
        await transition_case(session, old_case_id, "CLOSED", operator="admin_test")

        later = await record_violation(
            session,
            make_message("CASE_REOPEN_3", group, member),
            high_decision("CASE_REOPEN_3", group, member),
        )
        assert later.case is not None and later.case.id != old_case_id
        assert later.case.status == "PENDING_REVIEW"
        assert later.planned_actions[0].action == "recall"
        assert len(later.planned_actions) == 1
        assert json.loads(later.case.violation_ids_json) == [later.violation.id]
        assert later.violation.case_id == later.case.id
        assert first.violation.case_id == old_case_id
        old = await session.get(type(later.case), old_case_id)
        assert old is not None and old.status == "CLOSED"


@pytest.mark.asyncio
async def test_closed_case_reoffense_after_window_or_archive_still_creates_case() -> None:
    from datetime import UTC, datetime, timedelta

    from app.cases.models import Case
    from app.db import SessionLocal

    group, member = _ids()
    async with SessionLocal() as session:
        session.add(
            Case(
                case_no=f"OLD-{uuid.uuid4().hex[:12]}",
                group_openid=group,
                member_openid=member,
                status="CLOSED",
                archived=True,
                closed_at=datetime.now(UTC) - timedelta(days=45),
                audit_json=json.dumps({"transitions": [{"to": "KEEP"}]}),
            )
        )
        await session.commit()
        outcome = await record_violation(
            session,
            make_message("CASE_ARCHIVED_REOFFEND", group, member),
            high_decision("CASE_ARCHIVED_REOFFEND", group, member),
        )
        assert outcome.strike_no == 1
        assert outcome.case is not None and outcome.case.status == "PENDING_REVIEW"
        assert json.loads(outcome.case.violation_ids_json) == [outcome.violation.id]


@pytest.mark.asyncio
async def test_false_positive_closed_case_does_not_trigger_first_new_violation() -> None:
    from app.cases.models import Case
    from app.db import SessionLocal

    group, member = _ids()
    async with SessionLocal() as session:
        session.add(
            Case(
                case_no=f"FALSE-{uuid.uuid4().hex[:12]}",
                group_openid=group,
                member_openid=member,
                status="CLOSED",
                audit_json=json.dumps({"revoked_by": "admin_test"}),
            )
        )
        await session.commit()
        outcome = await record_violation(
            session,
            make_message("CASE_FALSE_REOFFEND", group, member),
            high_decision("CASE_FALSE_REOFFEND", group, member),
        )
        assert outcome.strike_no == 1 and outcome.case is None


@pytest.mark.asyncio
async def test_reoffense_ignores_damaged_closed_audit_and_finds_earlier_confirmation() -> None:
    from datetime import UTC, datetime, timedelta

    from app.cases.models import Case
    from app.db import SessionLocal

    group, member = _ids()
    async with SessionLocal() as session:
        session.add_all(
            [
                Case(
                    case_no=f"CONFIRMED-{uuid.uuid4().hex[:12]}",
                    group_openid=group,
                    member_openid=member,
                    status="CLOSED",
                    closed_at=datetime.now(UTC) - timedelta(days=50),
                    audit_json=json.dumps({"transitions": [{"to": "KEEP"}]}),
                ),
                Case(
                    case_no=f"DAMAGED-{uuid.uuid4().hex[:12]}",
                    group_openid=group,
                    member_openid=member,
                    status="CLOSED",
                    closed_at=datetime.now(UTC) - timedelta(days=40),
                    audit_json="{",
                ),
            ]
        )
        await session.commit()
        outcome = await record_violation(
            session,
            make_message("CASE_DAMAGED_AUDIT", group, member),
            high_decision("CASE_DAMAGED_AUDIT", group, member),
        )
        assert outcome.case is not None
        assert outcome.case.status == "PENDING_REVIEW"


@pytest.mark.asyncio
async def test_revoking_one_of_multiple_case_violations_keeps_case_pending() -> None:
    from app.db import SessionLocal

    group, member = _ids()
    async with SessionLocal() as session:
        first = await record_violation(
            session,
            make_message("CASE_PARTIAL_REVOKE_1", group, member),
            high_decision("CASE_PARTIAL_REVOKE_1", group, member),
        )
        second = await record_violation(
            session,
            make_message("CASE_PARTIAL_REVOKE_2", group, member),
            high_decision("CASE_PARTIAL_REVOKE_2", group, member),
        )
        assert second.case is not None
        await revoke_violation(session, first.violation.id, "误判一条", "admin_test")
        case = await session.get(type(second.case), second.case.id)
        assert case is not None and case.status == "PENDING_REVIEW"
        await revoke_violation(session, second.violation.id, "全部误判", "admin_test")
        await session.refresh(case)
        assert case.status == "CLOSED"


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
        # Existing cases remain readable and correctable after automatic case creation stops.
        case = Case(
            case_no=f"LEGACY-{uuid.uuid4().hex[:12]}",
            group_openid=group,
            member_openid=member,
            violation_ids_json=json.dumps([o1.violation.id, o2.violation.id]),
        )
        session.add(case)
        await session.flush()
        o1.violation.case_id = case.id
        o2.violation.case_id = case.id
        await session.commit()
        case_id = case.id
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
        historical = Case(
            case_no=f"LEGACY-{uuid.uuid4().hex[:12]}",
            group_openid=group,
            member_openid=member,
        )
        session.add(historical)
        await session.commit()
        case_id = historical.id
        case = await transition_case(session, case_id, "APPROVED_MANUAL", operator="admin_test")
        assert case.status == "APPROVED_MANUAL"
        case = await transition_case(session, case_id, "MANUAL_PENDING", operator="admin_test")
        assert case.status == "MANUAL_PENDING"
        with pytest.raises(IllegalTransitionError):
            await transition_case(session, case_id, "APPROVED_NAPCAT", operator="admin_test")
        fresh = await session.get(Case, case_id)
        assert fresh is not None and fresh.status == "MANUAL_PENDING"
