"""T-205 feedback and rule-candidate learning tests."""

from __future__ import annotations

import uuid
from collections.abc import Generator

import pytest
from app.adapters.qq_official.contract import Sender, StandardMessage
from app.db import SessionLocal
from app.moderation.dynamic_rules import DynamicRuleEngine, RuleVersion, load_active_snapshot
from app.moderation.feedback import (
    RuleCandidate,
    copy_candidate_to_draft,
    mine_rule_candidates,
    record_feedback,
)
from app.runtime.models import ShadowDecision
from fastapi.testclient import TestClient
from sqlalchemy import select


def _message(text: str, group: str) -> StandardMessage:
    return StandardMessage(
        message_id=f"FB_MSG_{uuid.uuid4().hex[:8]}",
        group_openid=group,
        sender=Sender(member_openid="M_FB"),
        text=text,
    )


@pytest.mark.asyncio
async def test_unknown_recall_is_not_used_as_truth() -> None:
    group = f"G_FB_UNKNOWN_{uuid.uuid4().hex[:6]}"
    for idx in range(3):
        async with SessionLocal() as session:
            await record_feedback(
                session,
                f"FB_UNKNOWN_{idx}_{uuid.uuid4().hex[:6]}",
                "unknown_recall",
                "ad",
                "test",
                sample_text="共同违规短语，联系我",
                group_openid=group,
                member_openid=f"M{idx % 2}",
            )
    async with SessionLocal() as session:
        candidates = await mine_rule_candidates(session)
    assert all(c.scope_key != group for c in candidates)


@pytest.mark.asyncio
async def test_confirmed_feedback_generates_candidate_with_conflict_report() -> None:
    group = f"G_FB_CAND_{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        for idx, member in enumerate(("M1", "M2", "M1")):
            await record_feedback(
                session,
                f"FB_POS_{idx}_{uuid.uuid4().hex[:6]}",
                "confirmed_violation",
                "ad",
                "test",
                sample_text="共同违规短语，联系我",
                group_openid=group,
                member_openid=member,
            )
        await record_feedback(
            session,
            f"FB_NEG_{uuid.uuid4().hex[:6]}",
            "confirmed_normal",
            "ad",
            "test",
            sample_text="共同违规短语，但是这是管理员解释为什么不要信",
            group_openid=group,
            member_openid="M3",
        )
        candidates = await mine_rule_candidates(session)

    target = next(c for c in candidates if c.pattern == "共同违规短语")
    assert target.support_count == 3
    assert target.member_count == 2
    assert target.conflict_count == 1
    assert target.status == "PROPOSED"


@pytest.mark.asyncio
async def test_candidate_copy_to_draft_does_not_publish() -> None:
    group = f"G_FB_COPY_{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        for idx, member in enumerate(("M1", "M2", "M1")):
            await record_feedback(
                session,
                f"FB_COPY_{idx}_{uuid.uuid4().hex[:6]}",
                "confirmed_violation",
                "ad",
                "test",
                sample_text="复制候选短语，联系我",
                group_openid=group,
                member_openid=member,
            )
        candidates = await mine_rule_candidates(session)
        candidate = next(c for c in candidates if c.pattern == "复制候选短语")
        draft_id = await copy_candidate_to_draft(session, candidate.id, operator="test")
        draft = await session.get(RuleVersion, draft_id)
        assert draft is not None
        snapshot = await load_active_snapshot(session, group)

    decision = DynamicRuleEngine(snapshot).evaluate(_message("复制候选短语", group))
    assert draft.status == "DRAFT"
    assert decision.verdict != "violation_high"


def test_admin_feedback_record_mine_and_copy_flow(logged_in_client: TestClient) -> None:
    import asyncio

    group = f"G_FB_WEB_{uuid.uuid4().hex[:6]}"
    phrase = "后台反馈短语"

    async def _seed() -> None:
        async with SessionLocal() as session:
            for idx, member in enumerate(("M1", "M2", "M1")):
                message_id = f"FB_WEB_{idx}_{uuid.uuid4().hex[:6]}"
                session.add(
                    ShadowDecision(
                        message_id=message_id,
                        group_openid=group,
                        member_openid=member,
                        sender_name="tester",
                        kind="text",
                        verdict="record_only",
                        category="ad",
                        confidence=0.5,
                        reason="seed",
                        detail_json=f'{{"text_preview":"{phrase}，联系我"}}',
                    )
                )
                await session.flush()
                await record_feedback(session, message_id, "confirmed_violation", "ad", "test-web")

    asyncio.run(_seed())
    page = logged_in_client.get("/admin/feedback")
    csrf = _extract_csrf(page.text)
    resp = logged_in_client.post(
        "/admin/feedback/mine", data={"csrf": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303

    async def _candidate_id() -> int:
        async with SessionLocal() as session:
            candidate = (
                await session.execute(
                    select(RuleCandidate).where(
                        RuleCandidate.scope_key == group,
                        RuleCandidate.pattern == phrase,
                        RuleCandidate.status == "PROPOSED",
                    )
                )
            ).scalar_one()
            return candidate.id

    candidate_id = asyncio.run(_candidate_id())
    csrf = _extract_csrf(logged_in_client.get("/admin/feedback").text)
    resp = logged_in_client.post(
        f"/admin/feedback/candidates/{candidate_id}/copy-to-draft",
        data={"csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/rules?notice=")


@pytest.fixture()
def logged_in_client() -> Generator[TestClient, None, None]:
    from app.main import app

    with TestClient(app) as client:
        client.post(
            "/admin/login",
            data={"username": "admin", "password": "test-admin-pass"},
            follow_redirects=False,
        )
        yield client


def _extract_csrf(html: str) -> str:
    import re

    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "页面应包含CSRF令牌"
    return match.group(1)
