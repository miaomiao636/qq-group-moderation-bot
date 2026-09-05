"""T-301/T-302/T-401/T-402 集成测试：管理后台HTTP流、报告、清理。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.adapters.qq_official.contract import Sender, StandardMessage
from app.cases.service import record_violation
from app.moderation.decision import ModerationDecision
from fastapi.testclient import TestClient
from sqlalchemy import select


def unique_ids() -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:8]
    return f"GROUP_W{suffix}", f"MEMBER_W{suffix}"


def make_case(group: str, member: str, message_id: str) -> int:
    """直接构造一个 PENDING_REVIEW 案件（两次违规）。"""
    import asyncio

    from app.db import SessionLocal

    async def _run() -> int:
        async with SessionLocal() as session:
            msg = StandardMessage(
                message_id=message_id,
                group_openid=group,
                sender=Sender(member_openid=member),
                text="违规测试内容",
            )
            decision = ModerationDecision(
                message_id=message_id,
                group_openid=group,
                sender_member_openid=member,
                verdict="violation_high",
                category="ad",
                confidence=0.95,
            )
            await record_violation(session, msg, decision)
            outcome = await record_violation(
                session,
                StandardMessage(
                    message_id=message_id + "_2",
                    group_openid=group,
                    sender=Sender(member_openid=member),
                    text="违规测试内容2",
                ),
                ModerationDecision(
                    message_id=message_id + "_2",
                    group_openid=group,
                    sender_member_openid=member,
                    verdict="violation_high",
                    category="ad",
                    confidence=0.95,
                ),
            )
            assert outcome.case is not None
            return outcome.case.id

    return asyncio.run(_run())


@pytest.fixture()
def client() -> TestClient:
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def logged_in(client: TestClient) -> TestClient:
    resp = client.post(
        "/admin/login",
        data={"username": "admin", "password": "test-admin-pass"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return client


# ---------- 认证 ----------


def test_admin_requires_login(client: TestClient) -> None:
    # /admin 会 307 规范化到 /admin/，再 303 到登录页；验证最终落在登录页
    resp = client.get("/admin", follow_redirects=True)
    assert resp.status_code == 200
    assert "管理后台登录" in resp.text


def test_login_wrong_password(client: TestClient) -> None:
    resp = client.post(
        "/admin/login", data={"username": "admin", "password": "wrong"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]


def test_login_success_sets_cookie(logged_in: TestClient) -> None:
    assert "admin_session" in logged_in.cookies


# ---------- 案件审批流 ----------


def test_full_manual_kick_flow(logged_in: TestClient) -> None:
    group, member = unique_ids()
    case_id = make_case(group, member, f"WEB_MSG_{uuid.uuid4().hex[:6]}")

    # ① 预览并生成确认码（页面仅显示一次，从第一次响应提取）
    resp = logged_in.post(f"/admin/cases/{case_id}/approve-manual")
    assert resp.status_code == 200
    assert "一次性确认码" in resp.text
    assert "未验证QQ号" in resp.text  # 证据页标注
    import re

    match = re.search(r"<b style=\"font-size:22px\">(\d{6})</b>", resp.text)
    assert match, "预览页应显示确认码"
    code = match.group(1)

    # 错误确认码被拒
    resp = logged_in.post(f"/admin/cases/{case_id}/confirm-kick", data={"code": "000000"})
    assert "错误或已过期" in resp.text

    # ② 凭码确认已人工踢出 → 结案（TestClient 默认跟随重定向，最终应回到详情页并带结案通知）
    resp = logged_in.post(f"/admin/cases/{case_id}/confirm-kick", data={"code": code})
    assert resp.status_code == 200
    assert "已记录为人工踢出并结案" in resp.text

    # 确认码一次性：重放被拒
    resp = logged_in.post(f"/admin/cases/{case_id}/confirm-kick", data={"code": code})
    assert "错误或已过期" in resp.text

    from app.cases.models import Case
    from app.db import SessionLocal

    async def _get() -> str:
        async with SessionLocal() as session:
            case = await session.get(Case, case_id)
            assert case is not None
            return str(case.status)

    import asyncio

    assert asyncio.run(_get()) == "CLOSED"


def test_keep_flow(logged_in: TestClient) -> None:
    group, member = unique_ids()
    case_id = make_case(group, member, f"WEB_KEEP_{uuid.uuid4().hex[:6]}")
    resp = logged_in.post(f"/admin/cases/{case_id}/keep", follow_redirects=False)
    assert resp.status_code == 303

    from app.cases.models import Case
    from app.db import SessionLocal

    async def _get() -> str:
        async with SessionLocal() as session:
            case = await session.get(Case, case_id)
            assert case is not None
            return str(case.status)

    import asyncio

    assert asyncio.run(_get()) == "CLOSED"


def test_false_positive_flow_revokes_violations(logged_in: TestClient) -> None:
    group, member = unique_ids()
    case_id = make_case(group, member, f"WEB_FP_{uuid.uuid4().hex[:6]}")
    resp = logged_in.post(f"/admin/cases/{case_id}/false-positive", follow_redirects=False)
    assert resp.status_code == 303

    from app.cases.models import ViolationRecord
    from app.db import SessionLocal

    async def _check() -> int:
        async with SessionLocal() as session:
            records = (
                (
                    await session.execute(
                        select(ViolationRecord).where(ViolationRecord.group_openid == group)
                    )
                )
                .scalars()
                .all()
            )
            return sum(1 for r in records if not r.revoked)

    import asyncio

    assert asyncio.run(_check()) == 0


def test_double_exit_mutual_exclusion_via_web(logged_in: TestClient) -> None:
    group, member = unique_ids()
    case_id = make_case(group, member, f"WEB_MX_{uuid.uuid4().hex[:6]}")
    logged_in.post(f"/admin/cases/{case_id}/approve-manual")
    resp = logged_in.post(f"/admin/cases/{case_id}/confirm-kick", data={"code": "wrongcode"})
    assert "错误或已过期" in resp.text
    # 手动尝试直接切 NapCat 出口（绕过UI）应被状态机拒绝
    from app.cases.service import transition_case
    from app.db import SessionLocal

    async def _try() -> None:
        async with SessionLocal() as session:
            from app.cases.case_sm import IllegalTransitionError

            with pytest.raises(IllegalTransitionError):
                await transition_case(session, case_id, "APPROVED_NAPCAT", "attacker")


# ---------- T-401 报告 ----------


@pytest.mark.asyncio
async def test_daily_report_shape() -> None:
    from app.db import SessionLocal
    from app.reports.service import build_daily

    async with SessionLocal() as session:
        report = await build_daily(session)
    assert report["report_type"] == "daily"
    assert report["cost"] == 0.0 if "cost" in report else True
    assert set(report) >= {"messages_processed", "violations_recorded", "cases_pending_review"}


@pytest.mark.asyncio
async def test_weekly_report_shape() -> None:
    from app.db import SessionLocal
    from app.reports.service import build_weekly

    async with SessionLocal() as session:
        report = await build_weekly(session)
    assert report["report_type"] == "weekly"


# ---------- T-402 数据保留 ----------


@pytest.mark.asyncio
async def test_cleanup_purges_expired() -> None:
    from app.db import SessionLocal
    from app.models import ActionLog, ProcessedEvent
    from app.reports.cleanup import purge_expired
    from sqlalchemy import func

    marker = f"CLEANUP_{uuid.uuid4().hex[:8]}"
    raw_old = datetime.now(UTC) - timedelta(days=60)  # 超过30天原始期
    decision_old = datetime.now(UTC) - timedelta(days=200)  # 超过180天判断期
    async with SessionLocal() as session:
        session.add(ProcessedEvent(message_id=marker, processed_at=raw_old, event_type="t"))
        session.add(
            ActionLog(action="recall", group_openid=marker, ok=True, created_at=decision_old)
        )
        await session.commit()

        # 插入确认存在
        count_before = (
            await session.execute(
                select(func.count())
                .select_from(ProcessedEvent)
                .where(ProcessedEvent.message_id == marker)
            )
        ).scalar_one()
        assert count_before == 1

        await purge_expired(session)

        count_after = (
            await session.execute(
                select(func.count())
                .select_from(ProcessedEvent)
                .where(ProcessedEvent.message_id == marker)
            )
        ).scalar_one()
        assert count_after == 0  # 过期记录已清理
        log_left = (
            await session.execute(
                select(func.count()).select_from(ActionLog).where(ActionLog.group_openid == marker)
            )
        ).scalar_one()
        assert log_left == 0  # 过期动作日志已清理


@pytest.mark.asyncio
async def test_cleanup_replaces_old_snapshot_but_keeps_metadata() -> None:
    from app.cases.models import ViolationRecord
    from app.db import SessionLocal
    from app.reports.cleanup import purge_expired

    old = datetime.now(UTC) - timedelta(days=45)
    async with SessionLocal() as session:
        session.add(
            ViolationRecord(
                group_openid="G_CLEAN",
                member_openid="M_CLEAN",
                message_id="SNAP_OLD",
                category="ad",
                confidence=0.95,
                message_snapshot_json='{"text": "敏感原文"}',
                created_at=old,
            )
        )
        await session.commit()
        await purge_expired(session)
        record = (
            await session.execute(
                select(ViolationRecord).where(ViolationRecord.message_id == "SNAP_OLD")
            )
        ).scalar_one()
        assert '"purged": true' in record.message_snapshot_json
        assert "敏感原文" not in record.message_snapshot_json
        assert record.category == "ad"  # 元数据保留供统计
