"""T-205 feedback and rule-candidate learning tests."""

from __future__ import annotations

import uuid
from collections.abc import Generator

import pytest
from app.adapters.qq_official.contract import Sender, StandardMessage
from app.cases.models import Case, ViolationRecord
from app.cases.service import count_active_violations, record_violation
from app.db import SessionLocal
from app.models import AdminAudit
from app.moderation.decision import ModerationDecision
from app.moderation.dynamic_rules import (
    DynamicRuleEngine,
    RuleVersion,
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


@pytest.mark.asyncio
async def test_cross_provider_samples_do_not_combine_into_candidate() -> None:
    group = f"G_FB_PROVIDER_{uuid.uuid4().hex}"
    async with SessionLocal() as session:
        for idx, provider in enumerate(("onebot", "onebot", "qq_official")):
            session.add(
                FeedbackRecord(
                    message_id=f"{provider}-{uuid.uuid4().hex}",
                    provider=provider,
                    group_openid=group,
                    external_group_id=group,
                    member_openid=f"member-{idx}",
                    external_user_id=f"member-{idx}",
                    label="confirmed_violation",
                    category="ad",
                    operator="test",
                    sample_text_masked="跨来源不得合并短语",
                )
            )
        await session.commit()
        candidates = await mine_rule_candidates(session)
    assert not [candidate for candidate in candidates if candidate.scope_key == group]


@pytest.mark.asyncio
async def test_new_provider_ambiguity_blocks_existing_candidate_copy() -> None:
    group = f"G_FB_AMBIG_{uuid.uuid4().hex}"
    async with SessionLocal() as session:
        for idx in range(3):
            await record_feedback(
                session,
                uuid.uuid4().hex,
                "confirmed_violation",
                "ad",
                "test",
                group_openid=group,
                member_openid=f"member-{idx}",
                sample_text="明确候选短语",
            )
        candidates = await mine_rule_candidates(session)
        candidate = next(c for c in candidates if c.scope_key == group)
        session.add(
            ShadowDecision(
                message_id=uuid.uuid4().hex,
                provider="onebot",
                group_openid=group,
                external_group_id=group,
                member_openid="member",
                kind="text",
                verdict="allow",
            )
        )
        await session.commit()
        with pytest.raises(ValueError, match="来源|provider"):
            await copy_candidate_to_draft(session, candidate.id, operator="test")


@pytest.mark.asyncio
async def test_automatic_candidate_threshold_cannot_be_lowered_to_single_label() -> None:
    async with SessionLocal() as session:
        with pytest.raises(ValueError, match="3|2"):
            await mine_rule_candidates(session, min_messages=1, min_members=1)


async def _label_mining_sample(
    session, group: str, index: int, label: str, *, message_id: str = ""
) -> None:
    await record_feedback(
        session,
        message_id or f"{group}-message-{index}",
        label,
        "ad",
        "test",
        group_openid=group,
        member_openid=f"member-{index % 2}",
        sample_text="反馈修正候选短语",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "latest_label", ["unknown_recall", "other_recall", "confirmed_normal", "false_positive"]
)
async def test_latest_label_revokes_historical_positive_support(latest_label: str) -> None:
    group = f"G_FB_RELABEL_{uuid.uuid4().hex}"
    async with SessionLocal() as session:
        for index in range(3):
            await _label_mining_sample(session, group, index, "confirmed_violation")
            await _label_mining_sample(session, group, index, latest_label)
        candidates = await mine_rule_candidates(session)
        assert not [candidate for candidate in candidates if candidate.scope_key == group]


@pytest.mark.asyncio
async def test_latest_negative_label_is_counted_once_and_can_be_retracted() -> None:
    group = f"G_FB_NEGATIVE_RELABEL_{uuid.uuid4().hex}"
    async with SessionLocal() as session:
        for index in range(3):
            await _label_mining_sample(session, group, index, "confirmed_violation")
        for label in ("confirmed_normal", "false_positive"):
            await _label_mining_sample(session, group, 3, label)
        candidates = await mine_rule_candidates(session)
        candidate = next(candidate for candidate in candidates if candidate.scope_key == group)
        assert candidate.conflict_count == 1
        await _label_mining_sample(session, group, 3, "unknown_recall")
        await mine_rule_candidates(session)
        await session.refresh(candidate)
        assert candidate.conflict_count == 0
        await _label_mining_sample(session, group, 3, "confirmed_violation")
        await mine_rule_candidates(session)
        await session.refresh(candidate)
        assert candidate.support_count == 4
        assert candidate.conflict_count == 0


@pytest.mark.asyncio
async def test_remine_dismisses_stale_proposals_but_preserves_copied_history() -> None:
    group = f"G_FB_STALE_{uuid.uuid4().hex}"
    async with SessionLocal() as session:
        for index in range(3):
            await _label_mining_sample(session, group, index, "confirmed_violation")
        candidates = await mine_rule_candidates(session)
        copied = next(candidate for candidate in candidates if candidate.scope_key == group)
        version_id = await copy_candidate_to_draft(session, copied.id, operator="test")
        # Simulate a previously human-published version using only local state;
        # correcting its evidence must not silently retract that rule history.
        await publish_rule_version(session, version_id, operator="test-human")
        candidates = await mine_rule_candidates(session)
        proposed = next(candidate for candidate in candidates if candidate.scope_key == group)
        assert proposed.id != copied.id
        for index in range(3):
            await _label_mining_sample(session, group, index, "unknown_recall")
        candidates = await mine_rule_candidates(session)
        assert not [candidate for candidate in candidates if candidate.scope_key == group]
        await session.refresh(proposed)
        await session.refresh(copied)
        assert proposed.status == "DISMISSED"
        assert proposed.support_count == 0
        assert proposed.member_count == 0
        assert copied.status == "COPIED_TO_DRAFT"
        assert copied.support_count == 3
        assert copied.copied_version_id == version_id
        draft = await session.get(RuleVersion, version_id)
        assert draft is not None and draft.status == "ACTIVE"
        proposed_examples = (
            await session.scalars(
                select(RuleCandidateExample).where(RuleCandidateExample.candidate_id == proposed.id)
            )
        ).all()
        copied_examples = (
            await session.scalars(
                select(RuleCandidateExample).where(RuleCandidateExample.candidate_id == copied.id)
            )
        ).all()
        assert proposed_examples == []
        assert len(copied_examples) == 3


@pytest.mark.asyncio
async def test_copy_rechecks_latest_support_without_a_manual_remine() -> None:
    group = f"G_FB_COPY_STALE_{uuid.uuid4().hex}"
    async with SessionLocal() as session:
        for index in range(3):
            await _label_mining_sample(session, group, index, "confirmed_violation")
        candidates = await mine_rule_candidates(session)
        candidate = next(candidate for candidate in candidates if candidate.scope_key == group)
        await _label_mining_sample(session, group, 0, "confirmed_normal")
        with pytest.raises(ValueError, match="支持|重新挖掘"):
            await copy_candidate_to_draft(session, candidate.id, operator="test")
        await session.refresh(candidate)
        assert candidate.status == "DISMISSED"
        assert candidate.support_count == 2
        assert candidate.conflict_count == 1


@pytest.mark.asyncio
async def test_latest_feedback_identity_includes_group() -> None:
    group = f"G_FB_IDENTITY_{uuid.uuid4().hex}"
    async with SessionLocal() as session:
        for index in range(3):
            message_id = f"{group}-message-{index}"
            await _label_mining_sample(session, group, index, "confirmed_violation")
            await _label_mining_sample(
                session, f"{group}-other", index, "unknown_recall", message_id=message_id
            )
        candidates = await mine_rule_candidates(session)
        candidate = next(candidate for candidate in candidates if candidate.scope_key == group)
        assert candidate.support_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["confirmed_normal", "false_positive"])
async def test_mapped_negative_feedback_revokes_only_the_exact_strike(label: str) -> None:
    group = f"G_FB_STRIKE_{uuid.uuid4().hex}"
    member = "fixture-member"
    raw_id = uuid.uuid4().hex
    internal_id = f"onebot:fixture-account:{raw_id}"
    message = StandardMessage(
        provider="onebot",
        message_id=raw_id,
        external_message_id=raw_id,
        external_group_id=group,
        external_user_id=member,
        sender=Sender(member_openid=member),
        text="人工待确认样本",
    )
    decision = ModerationDecision(
        message_id=raw_id,
        group_openid=group,
        sender_member_openid=member,
        verdict="violation_high",
        category="ad",
        confidence=0.95,
        reason="fixture-only",
    )
    async with SessionLocal() as session:
        first = await record_violation(session, message, decision)
        session.add(
            ShadowDecision(
                message_id=internal_id,
                external_message_id=raw_id,
                provider="onebot",
                external_group_id=group,
                external_user_id=member,
                group_openid=group,
                member_openid=member,
                verdict="violation_high",
            )
        )
        unrelated = []
        for provider, other_group, other_user, other_mid in (
            ("qq_official", group, member, raw_id),
            ("onebot", group + "-other", member, raw_id),
            ("onebot", group, member + "-other", raw_id),
            ("onebot", group, member, raw_id + "-other"),
        ):
            record = ViolationRecord(
                provider=provider,
                external_group_id=other_group,
                external_user_id=other_user,
                group_openid=other_group,
                member_openid=other_user,
                message_id=other_mid,
                category="ad",
                confidence=0.95,
            )
            session.add(record)
            unrelated.append(record)
        await session.commit()
        feedback = await record_feedback(
            session, internal_id, label, "ad", "fixture-human", reason="人工核实非违规"
        )
        await session.refresh(first.violation)
        assert first.violation.revoked
        assert str(feedback.id) in first.violation.revoke_reason
        assert "fixture-human" in first.violation.revoke_reason
        assert "人工核实非违规" in first.violation.revoke_reason
        audit = await session.scalar(
            select(AdminAudit).where(
                AdminAudit.action == "feedback_revoke_strike",
                AdminAudit.target_id == str(first.violation.id),
            )
        )
        assert audit is not None and audit.operator == "fixture-human"
        assert '"external_actions_undone": false' in audit.detail_json
        for record in unrelated:
            await session.refresh(record)
            assert not record.revoked
        # The other message for this same member remains a valid strike; remove
        # that fixture only from the active-count test without deleting history.
        unrelated[-1].revoked = True
        await session.commit()
        assert await count_active_violations(session, group, member, provider="onebot") == 0
        next_id = uuid.uuid4().hex
        second = await record_violation(
            session,
            message.model_copy(update={"message_id": next_id, "external_message_id": next_id}),
            decision.model_copy(update={"message_id": next_id}),
        )
        assert second.strike_no == 1
        mute = next(action for action in second.planned_actions if action.action == "mute")
        assert mute.params["seconds"] == 3600
        await record_feedback(session, internal_id, "confirmed_violation", "ad", "fixture-human")
        await session.refresh(first.violation)
        assert first.violation.revoked  # Re-labeling cannot silently restore an old punishment.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing", ["shadow", "external_message_id", "external_group_id", "external_user_id"]
)
async def test_negative_feedback_without_verified_mapping_does_not_revoke(missing: str) -> None:
    suffix = uuid.uuid4().hex
    group, member, raw_id = f"group-{suffix}", "fixture-member", f"raw-{suffix}"
    internal_id = f"onebot:fixture:{raw_id}"
    async with SessionLocal() as session:
        violation = ViolationRecord(
            provider="onebot",
            external_group_id=group,
            external_user_id=member,
            group_openid=group,
            member_openid=member,
            message_id=raw_id,
            category="ad",
            confidence=0.95,
        )
        session.add(violation)
        if missing != "shadow":
            values = {
                "external_message_id": raw_id,
                "external_group_id": group,
                "external_user_id": member,
            }
            values[missing] = ""
            session.add(
                ShadowDecision(
                    message_id=internal_id,
                    provider="onebot",
                    group_openid=group,
                    member_openid=member,
                    verdict="violation_high",
                    **values,
                )
            )
        await session.commit()
        await record_feedback(session, internal_id, "false_positive", "ad", "fixture-human")
        await session.refresh(violation)
        assert not violation.revoked


@pytest.mark.asyncio
async def test_feedback_revocation_preserves_pending_case_for_human_review() -> None:
    suffix = uuid.uuid4().hex
    group, member, raw_id = f"group-{suffix}", "fixture-member", f"raw-{suffix}"
    internal_id = f"onebot:fixture:{raw_id}"
    async with SessionLocal() as session:
        case = Case(
            case_no=f"TEST-{suffix}",
            provider="onebot",
            external_group_id=group,
            external_user_id=member,
            group_openid=group,
            member_openid=member,
            status="PENDING_REVIEW",
        )
        session.add(case)
        await session.flush()
        violation = ViolationRecord(
            provider="onebot",
            external_group_id=group,
            external_user_id=member,
            group_openid=group,
            member_openid=member,
            message_id=raw_id,
            category="ad",
            confidence=0.95,
            case_id=case.id,
        )
        session.add(violation)
        session.add(
            ShadowDecision(
                message_id=internal_id,
                external_message_id=raw_id,
                provider="onebot",
                external_group_id=group,
                external_user_id=member,
                group_openid=group,
                member_openid=member,
                verdict="violation_high",
            )
        )
        await session.commit()
        await record_feedback(session, internal_id, "unknown_recall", "ad", "fixture-human")
        await session.refresh(violation)
        assert not violation.revoked
        await record_feedback(session, internal_id, "false_positive", "ad", "fixture-human")
        await session.refresh(violation)
        await session.refresh(case)
        assert violation.revoked
        assert violation.case_id == case.id
        assert case.status == "PENDING_REVIEW"
        assert case.closed_at is None
