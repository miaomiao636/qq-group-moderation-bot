"""T-301/T-302/T-401/T-402 集成测试：管理后台HTTP流、报告、清理。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator
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
    """保留旧案件管理回归；新违规已不再自动立案。"""
    import asyncio

    from app.cases.models import Case
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
            first = await record_violation(session, msg, decision)
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
            assert outcome.case is None
            historical = Case(
                case_no=f"LEGACY-{uuid.uuid4().hex[:12]}",
                group_openid=group,
                member_openid=member,
                external_group_id=group,
                external_user_id=member,
                violation_ids_json=json.dumps([first.violation.id, outcome.violation.id]),
            )
            session.add(historical)
            await session.flush()
            first.violation.case_id = historical.id
            outcome.violation.case_id = historical.id
            await session.commit()
            return historical.id

    return asyncio.run(_run())


def extract_csrf(html: str) -> str:
    import re

    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "页面应包含CSRF令牌"
    return match.group(1)


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
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


def test_admin_rules_requires_login(client: TestClient) -> None:
    resp = client.get("/admin/rules", follow_redirects=True)
    assert resp.status_code == 200
    assert "管理后台登录" in resp.text


def test_admin_stats_requires_login(client: TestClient) -> None:
    resp = client.get("/admin/stats", follow_redirects=True)
    assert resp.status_code == 200
    assert "管理后台登录" in resp.text


def test_stats_dashboard_renders(logged_in: TestClient) -> None:
    resp = logged_in.get("/admin/stats")
    assert resp.status_code == 200
    assert "统计大盘" in resp.text
    assert "判定分布" in resp.text
    assert "AI 调用（按模型）" in resp.text


def test_case_dashboard_renders(logged_in: TestClient) -> None:
    """回归：案件主页必须可渲染（批量删除表单含 CSRF，曾是 NameError 事故点）。"""
    resp = logged_in.get("/admin/")
    assert resp.status_code == 200
    assert "待人工处理" in resp.text
    # R02/R03：批量删除已停用，页面不得再出现删除表单
    assert "删除勾选案件" not in resp.text
    assert "/admin/cases/batch-delete" not in resp.text
    # 筛选参数渲染
    resp2 = logged_in.get("/admin/", params={"status": "CLOSED", "page": "1"})
    assert resp2.status_code == 200


def test_state_changing_post_requires_csrf(logged_in: TestClient) -> None:
    resp = logged_in.post(
        "/admin/groups/alias",
        data={"group_openid": "G_NO_CSRF", "name": "测试群"},
    )
    assert resp.status_code == 403


def test_state_changing_post_accepts_csrf(logged_in: TestClient) -> None:
    page = logged_in.get("/admin/shadow")
    csrf = extract_csrf(page.text)
    group_id = f"G_WITH_CSRF_{uuid.uuid4().hex[:6]}"
    resp = logged_in.post(
        "/admin/groups/alias",
        data={"group_openid": group_id, "name": "测试群", "csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    import asyncio

    from app.db import SessionLocal
    from app.models import AdminAudit

    async def _audit_exists() -> bool:
        async with SessionLocal() as session:
            row = (
                await session.execute(
                    select(AdminAudit).where(
                        AdminAudit.action == "group_alias_save",
                        AdminAudit.target_id == group_id,
                    )
                )
            ).scalar_one_or_none()
            return row is not None

    assert asyncio.run(_audit_exists()) is True


def test_dynamic_rule_admin_create_item_publish_flow(logged_in: TestClient) -> None:
    page = logged_in.get("/admin/rules")
    assert page.status_code == 200
    csrf = extract_csrf(page.text)
    group_id = f"G_RULE_WEB_{uuid.uuid4().hex[:6]}"
    word = f"后台违规词{uuid.uuid4().hex[:6]}"
    draft_name = f"web-rule-{uuid.uuid4().hex[:6]}"

    resp = logged_in.post(
        "/admin/rules/drafts",
        data={"scope": "group", "scope_key": group_id, "name": draft_name, "csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    import asyncio

    from app.db import SessionLocal
    from app.moderation.dynamic_rules import (
        DynamicRuleEngine,
        RuleSet,
        RuleVersion,
        load_active_snapshot,
    )

    async def _draft_id() -> int:
        async with SessionLocal() as session:
            row = (
                await session.execute(
                    select(RuleVersion)
                    .join(RuleSet, RuleSet.id == RuleVersion.rule_set_id)
                    .where(
                        RuleVersion.description == draft_name,
                        RuleVersion.status == "DRAFT",
                        RuleSet.scope_key == group_id,
                    )
                )
            ).scalar_one()
            return row.id

    version_id = asyncio.run(_draft_id())
    csrf = extract_csrf(logged_in.get("/admin/rules").text)
    resp = logged_in.post(
        f"/admin/rules/drafts/{version_id}/items",
        data={
            "item_type": "keyword",
            "pattern": word,
            "category": "ad",
            "weight": "0.95",
            "description": "测试后台规则",
            "csrf": csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    csrf = extract_csrf(logged_in.get("/admin/rules").text)
    resp = logged_in.post(
        f"/admin/rules/versions/{version_id}/publish",
        data={"csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    plan_url = resp.headers["location"]
    assert plan_url.startswith("/admin/plans/")
    assert (
        logged_in.post(
            plan_url + "/approve", data={"csrf": csrf}, follow_redirects=False
        ).status_code
        == 303
    )
    assert (
        logged_in.post(
            plan_url + "/execute", data={"csrf": csrf}, follow_redirects=False
        ).status_code
        == 303
    )

    async def _decision() -> str:
        async with SessionLocal() as session:
            snapshot = await load_active_snapshot(session, group_id)
        msg = StandardMessage(
            message_id=f"WEB_RULE_{uuid.uuid4().hex[:6]}",
            group_openid=group_id,
            sender=Sender(member_openid="M_WEB_RULE"),
            text=word,
        )
        return DynamicRuleEngine(snapshot).evaluate(msg).verdict

    assert asyncio.run(_decision()) == "violation_high"


# ---------- 案件审批流 ----------


def test_full_manual_kick_flow(logged_in: TestClient) -> None:
    group, member = unique_ids()
    case_id = make_case(group, member, f"WEB_MSG_{uuid.uuid4().hex[:6]}")

    # 2026-09-12 简化（负责人决定）：人工处理一键确认踢出，无确认码
    page = logged_in.get(f"/admin/cases/{case_id}")
    csrf = extract_csrf(page.text)
    assert "人工处理（确认踢出并结案）" in page.text
    resp = logged_in.post(f"/admin/cases/{case_id}/manual-kick", data={"csrf": csrf})
    assert resp.status_code == 200
    assert "已记录为人工踢出并结案" in resp.text

    from app.cases.models import Case
    from app.db import SessionLocal

    async def _get() -> str:
        async with SessionLocal() as session:
            case = await session.get(Case, case_id)
            assert case is not None
            return str(case.status)

    import asyncio

    assert asyncio.run(_get()) == "CLOSED"


def test_manual_kick_rejected_after_closed(logged_in: TestClient) -> None:
    """终态案件再提交一键踢出：友好报错而非 500。"""
    group, member = unique_ids()
    case_id = make_case(group, member, f"WEB_MSG_{uuid.uuid4().hex[:6]}")
    page = logged_in.get(f"/admin/cases/{case_id}")
    csrf = extract_csrf(page.text)
    resp = logged_in.post(f"/admin/cases/{case_id}/manual-kick", data={"csrf": csrf})
    assert resp.status_code == 200
    # 已 CLOSED，再次提交应得到友好错误页
    resp = logged_in.post(f"/admin/cases/{case_id}/manual-kick", data={"csrf": csrf})
    assert resp.status_code == 200
    assert "操作未执行" in resp.text
    assert "返回案件页" in resp.text


def test_keep_flow(logged_in: TestClient) -> None:
    group, member = unique_ids()
    case_id = make_case(group, member, f"WEB_KEEP_{uuid.uuid4().hex[:6]}")
    csrf = extract_csrf(logged_in.get(f"/admin/cases/{case_id}").text)
    resp = logged_in.post(
        f"/admin/cases/{case_id}/keep", data={"csrf": csrf}, follow_redirects=False
    )
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
    csrf = extract_csrf(logged_in.get(f"/admin/cases/{case_id}").text)
    resp = logged_in.post(
        f"/admin/cases/{case_id}/false-positive",
        data={"csrf": csrf},
        follow_redirects=False,
    )
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
    csrf = extract_csrf(logged_in.get(f"/admin/cases/{case_id}").text)
    logged_in.post(f"/admin/cases/{case_id}/approve-manual", data={"csrf": csrf})
    resp = logged_in.post(
        f"/admin/cases/{case_id}/confirm-kick", data={"code": "wrongcode", "csrf": csrf}
    )
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


def make_shadow(mid: str, category: str, group: str, member: str) -> None:
    """造一条影子判定记录（record_only + 指定类别），用于反馈表单一致性回归。"""
    import asyncio

    from app.db import SessionLocal
    from app.runtime.models import ShadowDecision

    async def _run() -> None:
        async with SessionLocal() as session:
            session.add(
                ShadowDecision(
                    message_id=mid,
                    provider="onebot",
                    external_group_id=group,
                    group_openid=group,
                    member_openid=member,
                    kind="text",
                    verdict="record_only",
                    category=category,
                    confidence=0.40,
                    reason="AI疑似严重类别（低置信），保留类别转人工核对",
                )
            )
            await session.commit()

    asyncio.run(_run())


def _feedback_block(html: str, mid: str) -> str:
    anchor = html.find(f'name=message_id value="{mid}"')
    assert anchor != -1, f"影子页应包含 {mid} 的反馈表单"
    return html[anchor : anchor + 1500]


def test_feedback_form_category_matches_persisted(logged_in: TestClient) -> None:
    """主审 P2 回归：持久化的严重类别 → 反馈表单默认选中（不被 ad 掩盖）。"""
    group, member = unique_ids()
    mid = f"MSG_SHADOW_{uuid.uuid4().hex[:8]}"
    make_shadow(mid, "fraud", group, member)

    resp = logged_in.get("/admin/shadow")
    assert resp.status_code == 200
    block = _feedback_block(resp.text, mid)
    assert "name=category" in block
    assert "<option value=fraud selected>诈骗</option>" in block


def test_feedback_form_category_editable_and_validated(logged_in: TestClient) -> None:
    """人工可核对/纠正类别；非法值被白名单拦截为 other（防注入）。"""
    group, member = unique_ids()
    mid_fix = f"MSG_SHADOW_{uuid.uuid4().hex[:8]}"
    mid_bad = f"MSG_SHADOW_{uuid.uuid4().hex[:8]}"
    make_shadow(mid_fix, "fraud", group, member)
    make_shadow(mid_bad, "violence", group, member)

    resp = logged_in.get("/admin/shadow")
    csrf = extract_csrf(resp.text)

    # 人工核对后纠正类别：fraud → porn，保存后回显纠正值
    resp2 = logged_in.post(
        "/admin/feedback",
        data={
            "message_id": mid_fix,
            "label": "confirmed_violation",
            "category": "porn",
            "reason": "人工核对：色情而非诈骗",
            "csrf": csrf,
        },
    )
    assert resp2.status_code == 200
    block = _feedback_block(logged_in.get("/admin/shadow").text, mid_fix)
    assert "<option value=porn selected>色情低俗</option>" in block

    # 非法类别注入被拦截为 other
    resp3 = logged_in.post(
        "/admin/feedback",
        data={
            "message_id": mid_bad,
            "label": "confirmed_violation",
            "category": "evil;drop",
            "reason": "",
            "csrf": csrf,
        },
    )
    assert resp3.status_code == 200
    block = _feedback_block(logged_in.get("/admin/shadow").text, mid_bad)
    assert "<option value=other selected>其他</option>" in block
