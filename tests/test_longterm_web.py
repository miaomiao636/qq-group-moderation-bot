"""Long-term admin reliability: atomic correction, complete dates and reachable candidates.

Uses a per-test temporary SQLite database and synthetic records only.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import UTC, datetime

import pytest
from app.cases.models import Case, ViolationRecord
from app.db import Base
from app.models import AdminAudit
from app.moderation.dynamic_rules import RuleVersion
from app.moderation.feedback import FeedbackRecord, RuleCandidate, mine_rule_candidates
from app.web import auth, confirm, routes
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def web_ui(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'longterm-web.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(routes, "SessionLocal", factory)
    application = FastAPI()
    application.include_router(routes.router)
    token = auth.login("admin", "test-admin-pass")
    assert token is not None
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application, raise_app_exceptions=False),
            base_url="http://isolated.test",
        ) as client:
            client.cookies.set(auth.SESSION_COOKIE, token)
            yield client, factory, engine, auth.csrf_token(token)
    finally:
        confirm.reset_all()
        auth.logout(token)
        await engine.dispose()


async def _case_with_violation(factory, *, status="PENDING_REVIEW"):
    async with factory() as session:
        record = ViolationRecord(
            provider="onebot",
            group_openid="synthetic-group",
            member_openid="synthetic-member",
            external_group_id="synthetic-group",
            external_user_id="synthetic-member",
            message_id="synthetic-violation",
            category="ad",
            confidence=0.99,
        )
        session.add(record)
        await session.flush()
        case = Case(
            case_no="SYNTHETIC-CORRECTION",
            provider="onebot",
            group_openid="synthetic-group",
            member_openid="synthetic-member",
            external_group_id="synthetic-group",
            external_user_id="synthetic-member",
            status=status,
            violation_ids_json=json.dumps([record.id]),
            audit_json='{"synthetic_prior_evidence":"preserve"}',
        )
        session.add(case)
        await session.flush()
        record.case_id = case.id
        await session.commit()
        return case.id, record.id


async def _correction_snapshot(factory, case_id, record_id):
    async with factory() as session:
        case = await session.get(Case, case_id)
        record = await session.get(ViolationRecord, record_id)
        assert case is not None and record is not None
        audits = list(
            await session.scalars(
                select(AdminAudit).where(
                    AdminAudit.target_type == "case", AdminAudit.target_id == str(case_id)
                )
            )
        )
        return {
            "status": case.status,
            "closed_at": case.closed_at,
            "audit_json": case.audit_json,
            "revoked": record.revoked,
            "revoke_reason": record.revoke_reason,
            "admin_audits": [(row.id, row.action, row.detail_json) for row in audits],
        }


async def test_false_positive_storage_failure_rolls_back_entire_operation_and_can_retry(web_ui):
    client, factory, engine, csrf = web_ui
    case_id, record_id = await _case_with_violation(factory)
    before = await _correction_snapshot(factory, case_id, record_id)
    interrupted = False

    def refuse_violation_write(_connection, _cursor, statement, parameters, _context, _many):
        nonlocal interrupted
        if not interrupted and statement.lstrip().lower().startswith("update violation_records"):
            interrupted = True
            raise OperationalError(
                statement, parameters, sqlite3.OperationalError("synthetic storage write failure")
            )

    event.listen(engine.sync_engine, "before_cursor_execute", refuse_violation_write)
    try:
        response = await client.post(f"/admin/cases/{case_id}/false-positive", data={"csrf": csrf})
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", refuse_violation_write)
    assert interrupted, "The probe must interrupt the actual violation revocation write."
    assert response.status_code in {500, 503}
    assert await _correction_snapshot(factory, case_id, record_id) == before

    retry = await client.post(f"/admin/cases/{case_id}/false-positive", data={"csrf": csrf})
    assert retry.status_code == 303
    after = await _correction_snapshot(factory, case_id, record_id)
    assert after["status"] == "CLOSED" and after["closed_at"] is not None
    assert after["revoked"] is True
    assert after["revoke_reason"]
    audit = json.loads(after["audit_json"])
    assert audit["synthetic_prior_evidence"] == "preserve"
    assert [(item["from"], item["to"]) for item in audit["transitions"]] == [
        ("PENDING_REVIEW", "FALSE_POSITIVE"),
        ("FALSE_POSITIVE", "STRIKE_REVOKED"),
        ("STRIKE_REVOKED", "CLOSED"),
    ]
    assert [entry[1] for entry in after["admin_audits"]] == ["case_false_positive"]


@pytest.mark.parametrize(
    "action", ["false-positive", "keep", "cancel", "manual-kick", "confirm-kick"]
)
async def test_case_audit_storage_failure_rolls_back_entire_human_operation(web_ui, action):
    client, factory, engine, csrf = web_ui
    case_id, record_id = await _case_with_violation(
        factory,
        status="MANUAL_PENDING" if action in {"cancel", "confirm-kick"} else "PENDING_REVIEW",
    )
    data = {"csrf": csrf}
    if action == "confirm-kick":
        data["code"] = confirm.generate(case_id)
    before = await _correction_snapshot(factory, case_id, record_id)
    interrupted = False

    def refuse_audit(_connection, _cursor, statement, parameters, _context, _many):
        nonlocal interrupted
        if statement.lstrip().lower().startswith("insert into admin_audit"):
            interrupted = True
            raise OperationalError(
                statement, parameters, sqlite3.OperationalError("synthetic audit failure")
            )

    event.listen(engine.sync_engine, "before_cursor_execute", refuse_audit)
    try:
        response = await client.post(f"/admin/cases/{case_id}/{action}", data=data)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", refuse_audit)
    assert interrupted
    assert response.status_code in {500, 503}
    assert await _correction_snapshot(factory, case_id, record_id) == before
    assert (await client.post(f"/admin/cases/{case_id}/{action}", data=data)).status_code == 303
    if action == "confirm-kick":
        assert not confirm.verify_and_consume(case_id, data["code"])


def test_confirmation_reservation_prevents_overlap_and_restores_only_unexpired_generation(
    monkeypatch,
):
    clock = [0.0]
    monkeypatch.setattr(confirm.time, "monotonic", lambda: clock[0])
    try:
        code = confirm.generate(901)
        with (
            pytest.raises(ValueError, match="rollback"),
            confirm.reserve_code(901, code) as accepted,
        ):
            assert accepted
            assert not confirm.verify_and_consume(901, code)
            raise ValueError("rollback")
        assert confirm.verify_and_consume(901, code)
        assert not confirm.verify_and_consume(901, code)

        code = confirm.generate(902)
        with (
            pytest.raises(ValueError, match="expired"),
            confirm.reserve_code(902, code) as accepted,
        ):
            assert accepted
            clock[0] += confirm.CODE_TTL_SECONDS + 1
            raise ValueError("expired")
        assert not confirm.verify_and_consume(902, code)

        for failed in (False, True):
            old = confirm.generate(903)
            try:
                with confirm.reserve_code(903, old) as accepted:
                    assert accepted
                    new = confirm.generate(903)
                    if failed:
                        raise ValueError("old generation failed")
            except ValueError:
                pass
            assert confirm.verify_and_consume(903, new)
    finally:
        confirm.reset_all()


async def test_confirm_code_survives_commit_failure_and_cannot_replay_success(web_ui, monkeypatch):
    client, factory, _, csrf = web_ui
    case_id, record_id = await _case_with_violation(factory, status="MANUAL_PENDING")
    before = await _correction_snapshot(factory, case_id, record_id)
    code = confirm.generate(case_id)
    interrupted = False

    original_commit = factory.class_.commit

    async def refuse_commit(session):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise OperationalError(
                "COMMIT", None, sqlite3.OperationalError("synthetic commit failure")
            )
        await original_commit(session)

    monkeypatch.setattr(factory.class_, "commit", refuse_commit)
    response = await client.post(
        f"/admin/cases/{case_id}/confirm-kick", data={"csrf": csrf, "code": code}
    )
    assert interrupted and response.status_code in {500, 503}
    assert await _correction_snapshot(factory, case_id, record_id) == before
    retry = await client.post(
        f"/admin/cases/{case_id}/confirm-kick", data={"csrf": csrf, "code": code}
    )
    assert retry.status_code == 303
    after = await _correction_snapshot(factory, case_id, record_id)
    assert after["status"] == "CLOSED"
    assert [entry[1] for entry in after["admin_audits"]] == ["case_confirm_manual_kick"]
    assert not confirm.verify_and_consume(case_id, code)


@pytest.mark.parametrize("replacement", ["clear", "generate-and-consume"])
def test_failed_confirmation_does_not_resurrect_removed_code(replacement):
    try:
        code = confirm.generate(904)
        with pytest.raises(RuntimeError), confirm.reserve_code(904, code) as accepted:
            assert accepted
            if replacement == "clear":
                confirm.clear(904)
            else:
                new = confirm.generate(904)
                assert confirm.verify_and_consume(904, new)
            raise RuntimeError("synthetic rollback")
        assert not confirm.verify_and_consume(904, code)
    finally:
        confirm.reset_all()


@pytest.mark.parametrize("winner", ["keep", "false-positive"])
async def test_opposing_human_decisions_commit_only_one_conclusion(web_ui, monkeypatch, winner):
    from app.cases import service

    client, factory, engine, csrf = web_ui
    case_id, record_id = await _case_with_violation(factory)
    locked = asyncio.Event()
    contender_entered = asyncio.Event()
    release = asyncio.Event()
    original_transition = service.transition_case
    writer_attempts = 0

    async def pause_first_transition(*args, **kwargs):
        if not locked.is_set():
            locked.set()
            await release.wait()
        return await original_transition(*args, **kwargs)

    def observe_writer(_connection, _cursor, statement, _parameters, _context, _many):
        nonlocal writer_attempts
        if statement.lstrip().lower().startswith("update cases set status=cases.status"):
            writer_attempts += 1
            if writer_attempts == 2:
                contender_entered.set()

    monkeypatch.setattr(service, "transition_case", pause_first_transition)
    event.listen(engine.sync_engine, "before_cursor_execute", observe_writer)
    loser = "false-positive" if winner == "keep" else "keep"
    tasks = []
    try:
        first = asyncio.create_task(
            client.post(f"/admin/cases/{case_id}/{winner}", data={"csrf": csrf})
        )
        tasks.append(first)
        await asyncio.wait_for(locked.wait(), 2)
        second = asyncio.create_task(
            client.post(f"/admin/cases/{case_id}/{loser}", data={"csrf": csrf})
        )
        tasks.append(second)
        await asyncio.wait_for(contender_entered.wait(), 2)
        assert not second.done()
        release.set()
        first_response, second_response = await asyncio.wait_for(asyncio.gather(*tasks), 3)
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        event.remove(engine.sync_engine, "before_cursor_execute", observe_writer)
    assert first_response.status_code == 303
    assert second_response.status_code == 200 and "操作未执行" in second_response.text
    after = await _correction_snapshot(factory, case_id, record_id)
    assert after["status"] == "CLOSED"
    assert after["revoked"] is (winner == "false-positive")
    targets = [item["to"] for item in json.loads(after["audit_json"])["transitions"]]
    assert targets == (
        ["KEEP", "CLOSED"] if winner == "keep" else ["FALSE_POSITIVE", "STRIKE_REVOKED", "CLOSED"]
    )
    assert [entry[1] for entry in after["admin_audits"]] == [
        "case_keep" if winner == "keep" else "case_false_positive"
    ]


@pytest.mark.parametrize("status", ["CLOSED", "MANUAL_PENDING", "EXECUTING"])
async def test_false_positive_rejects_illegal_original_state_without_changes(web_ui, status):
    client, factory, _, csrf = web_ui
    case_id, record_id = await _case_with_violation(factory, status=status)
    before = await _correction_snapshot(factory, case_id, record_id)
    response = await client.post(f"/admin/cases/{case_id}/false-positive", data={"csrf": csrf})
    assert response.status_code == 200
    assert "操作未执行" in response.text
    assert await _correction_snapshot(factory, case_id, record_id) == before


@pytest.mark.parametrize("csrf", ["", "invalid-csrf"])
async def test_false_positive_requires_csrf_and_human_login(web_ui, csrf):
    client, factory, _, real_csrf = web_ui
    case_id, record_id = await _case_with_violation(factory)
    before = await _correction_snapshot(factory, case_id, record_id)
    response = await client.post(f"/admin/cases/{case_id}/false-positive", data={"csrf": csrf})
    assert response.status_code == 403
    client.cookies.clear()
    response = await client.post(f"/admin/cases/{case_id}/false-positive", data={"csrf": real_csrf})
    assert response.status_code == 401
    assert await _correction_snapshot(factory, case_id, record_id) == before


async def test_case_date_filter_includes_final_second_and_excludes_next_midnight(web_ui):
    client, factory, _, _ = web_ui
    times = [
        ("BEFORE-RANGE", datetime(2026, 9, 19, 23, 59, 59, 999999, tzinfo=UTC)),
        ("START-MIDNIGHT", datetime(2026, 9, 20, tzinfo=UTC)),
        ("LAST-SECOND", datetime(2026, 9, 20, 23, 59, 59, tzinfo=UTC)),
        ("LAST-FRACTION", datetime(2026, 9, 20, 23, 59, 59, 999999, tzinfo=UTC)),
        ("NEXT-MIDNIGHT", datetime(2026, 9, 21, tzinfo=UTC)),
    ]
    async with factory() as session:
        session.add_all(
            Case(
                case_no=name,
                group_openid="synthetic-date-group",
                member_openid="synthetic-date-member",
                status="CLOSED",
                created_at=created_at,
            )
            for name, created_at in times
        )
        await session.commit()
    response = await client.get(
        "/admin/", params={"date_from": "2026-09-20", "date_to": "2026-09-20"}
    )
    assert response.status_code == 200
    for expected in ("START-MIDNIGHT", "LAST-SECOND", "LAST-FRACTION"):
        assert expected in response.text
    for outside in ("BEFORE-RANGE", "NEXT-MIDNIGHT"):
        assert outside not in response.text


@pytest.mark.parametrize(
    "params",
    [
        {"date_from": "not-a-date"},
        {"date_to": "2026-02-30"},
        {"date_from": "2026-09-21", "date_to": "2026-09-20"},
    ],
)
async def test_case_date_filter_rejects_invalid_or_reversed_dates(web_ui, params):
    client, _, _, _ = web_ui
    response = await client.get("/admin/", params=params)
    assert response.status_code == 422


async def test_candidate_pages_reach_oldest_unreviewed_candidate_and_bound_page_size(web_ui):
    client, factory, _, _ = web_ui
    async with factory() as session:
        for index in range(85):
            session.add(
                RuleCandidate(
                    scope="group",
                    scope_key="synthetic-candidate-group",
                    item_type="keyword",
                    pattern=f"syntheticword{index:03}",
                    created_at=datetime(2026, 9, 21, tzinfo=UTC),
                )
            )
        await session.commit()
        expected_ids = list(
            await session.scalars(select(RuleCandidate.id).order_by(RuleCandidate.id.desc()))
        )
    observed = []
    pattern = r"/admin/feedback/candidates/(\d+)/copy-to-draft"
    for page, count in [(1, 20), (2, 20), (3, 20), (4, 20), (5, 5)]:
        response = await client.get("/admin/feedback", params={"candidate_page": page})
        assert response.status_code == 200
        ids = [int(value) for value in re.findall(pattern, response.text)]
        assert len(ids) == count
        assert "共 85 条" in response.text
        observed.extend(ids)
    assert observed == expected_ids
    for size, count in [(50, 50), (999999999, 20)]:
        response = await client.get("/admin/feedback", params={"candidate_page_size": size})
        assert len(re.findall(pattern, response.text)) == count
    last = await client.get("/admin/feedback", params={"candidate_page": 99999})
    assert [int(value) for value in re.findall(pattern, last.text)] == expected_ids[-5:]


async def test_candidate_copy_is_csrf_guarded_and_publication_still_requires_human_plan(web_ui):
    client, factory, _, csrf = web_ui
    async with factory() as session:
        for index in range(3):
            session.add(
                FeedbackRecord(
                    message_id=f"synthetic-support-{index}",
                    group_openid="synthetic-supported-group",
                    external_group_id="synthetic-supported-group",
                    provider="onebot",
                    member_openid=f"synthetic-member-{index}",
                    external_user_id=f"synthetic-member-{index}",
                    label="confirmed_violation",
                    category="ad",
                    operator="synthetic-human",
                    sample_text_masked="人工复核样例",
                )
            )
        await session.commit()
        candidates = await mine_rule_candidates(session)
        candidate_id = next(
            candidate.id for candidate in candidates if candidate.pattern == "人工复核样例"
        )
    copy_url = f"/admin/feedback/candidates/{candidate_id}/copy-to-draft"
    assert (await client.post(copy_url)).status_code == 403
    async with factory() as session:
        assert (await session.get(RuleCandidate, candidate_id)).status == "PROPOSED"
        assert list(await session.scalars(select(RuleVersion))) == []
    assert (await client.post(copy_url, data={"csrf": csrf})).status_code == 303
    async with factory() as session:
        candidate = await session.get(RuleCandidate, candidate_id)
        draft_id = candidate.copied_version_id
        assert candidate.status == "COPIED_TO_DRAFT"
        assert (await session.get(RuleVersion, draft_id)).status == "DRAFT"
    publish_url = f"/admin/rules/versions/{draft_id}/publish"
    assert (await client.post(publish_url)).status_code == 403
    preview = await client.post(publish_url, data={"csrf": csrf})
    assert preview.status_code == 303
    plan_url = preview.headers["location"]
    assert plan_url.startswith("/admin/plans/")
    assert (await client.post(plan_url + "/execute", data={"csrf": csrf})).status_code == 403
    async with factory() as session:
        assert (await session.get(RuleVersion, draft_id)).status == "DRAFT"
    assert (await client.post(plan_url + "/approve", data={"csrf": csrf})).status_code == 303
    assert (await client.post(plan_url + "/execute", data={"csrf": csrf})).status_code == 303
    async with factory() as session:
        assert (await session.get(RuleVersion, draft_id)).status == "ACTIVE"
