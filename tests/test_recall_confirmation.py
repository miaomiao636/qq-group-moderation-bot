"""Recall transport acceptance must not masquerade as a confirmed recall."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from app.actions.orchestrator import ActionIntent, orchestrate_actions, summarize_intents
from app.actions.recall_confirmation import (
    RecallConfirmation,
    RecallNotice,
    accept_notice,
    arm_confirmation,
    confirmation_label,
)
from app.adapters.onebot.recall_notice import parse_recall_notice
from app.db import SessionLocal
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from tests.test_onebot_actions import (
    _FakeOneBotClient,
    _high_decision,
    _ob_msg,
    _official_onebot_settings,
    _setup_onebot_group,
)


@pytest.mark.asyncio
async def test_api_success_without_notice_does_not_escalate_or_replay() -> None:
    group = str(uuid4().int)[:10]
    msg = _ob_msg(group, "1001")
    msg.sent_at = datetime.now(UTC)
    msg.external_self_id = "10000001"
    client = _FakeOneBotClient()
    async with SessionLocal() as session:
        await _setup_onebot_group(session, group)
        intents = await orchestrate_actions(
            session,
            msg,
            _high_decision(msg),
            onebot_client=client,
            settings=_official_onebot_settings(),
        )
        again = await orchestrate_actions(
            session,
            msg,
            _high_decision(msg),
            onebot_client=client,
            settings=_official_onebot_settings(),
        )
    assert [(i.action, i.status) for i in intents] == [("recall", "SUCCEEDED")]
    assert [i.id for i in again] == [i.id for i in intents]
    assert [call[0] for call in client.calls] == ["recall"]


async def _attempt(*, sent_at=None, client=None, role="member", account="10000001"):
    msg = _ob_msg(str(uuid4().int)[:10], "1001", role=role)
    msg.external_self_id = account
    msg.sent_at = sent_at or datetime.now(UTC).replace(microsecond=0)
    client = client or _FakeOneBotClient()
    async with SessionLocal() as session:
        await _setup_onebot_group(session, msg.external_group_id)
        intents = await orchestrate_actions(
            session,
            msg,
            _high_decision(msg),
            onebot_client=client,
            settings=_official_onebot_settings(),
        )
    return msg, client, intents


def _notice(msg, *, occurred_at=None):
    return RecallNotice(
        account_id=msg.external_self_id,
        group_id=msg.external_group_id,
        user_id=msg.external_user_id,
        message_id=msg.external_message_id,
        operator_id="10000001",
        occurred_at=occurred_at or datetime.now(UTC).replace(microsecond=0),
    )


@pytest.mark.asyncio
async def test_late_notice_survives_new_session_never_replays_or_relabels() -> None:
    from app.moderation.feedback import FeedbackRecord

    msg, client, intents = await _attempt()
    async with SessionLocal() as session:
        assert await accept_notice(session, _notice(msg))
        assert not await accept_notice(session, _notice(msg))
    async with SessionLocal() as session:
        row = await session.get(RecallConfirmation, intents[0].id)
        assert confirmation_label(row) == "已收到 QQ 撤回通知"
        assert row.notice_at is not None and row.notice_fingerprint
        again = await orchestrate_actions(
            session,
            msg,
            _high_decision(msg),
            onebot_client=client,
            settings=_official_onebot_settings(),
        )
        assert len(again) == 1 and again[0].status == "SUCCEEDED"
        assert not (
            await session.scalars(
                select(FeedbackRecord).where(FeedbackRecord.message_id == msg.message_id)
            )
        ).all()
    assert [c[0] for c in client.calls] == ["recall"]


@pytest.mark.asyncio
async def test_confirmed_recall_does_not_resume_legacy_pending_punishments() -> None:
    msg, client, intents = await _attempt()
    async with SessionLocal() as session:
        legacy = [
            ActionIntent(
                idempotency_key=uuid4().hex,
                action=action,
                status="PENDING",
                provider="onebot",
                group_openid=msg.external_group_id,
                message_id=msg.message_id,
                external_group_id=msg.external_group_id,
                external_user_id=msg.external_user_id,
                external_message_id=msg.external_message_id,
            )
            for action in ("mute", "warn")
        ]
        session.add_all(legacy)
        await session.commit()
        assert await accept_notice(session, _notice(msg))
        again = await orchestrate_actions(
            session,
            msg,
            _high_decision(msg),
            onebot_client=client,
            settings=_official_onebot_settings(),
        )
        assert {i.id for i in again} == {intents[0].id, *(i.id for i in legacy)}
        for row in legacy:
            await session.refresh(row)
            assert row.status == "PENDING"
    assert [call[0] for call in client.calls] == ["recall"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "10000002"),
        ("group_id", "222"),
        ("user_id", "222"),
        ("message_id", "222"),
        ("operator_id", "10000002"),
    ],
)
async def test_wrong_identity_notice_is_not_confirmation(field, value) -> None:
    msg, _, intents = await _attempt()
    async with SessionLocal() as session:
        assert not await accept_notice(session, replace(_notice(msg), **{field: value}))
        row = await session.get(RecallConfirmation, intents[0].id)
        assert row.confirmed_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("skew", [-18, 18])
async def test_platform_message_clock_skew_does_not_reject_local_napcat_notice(skew) -> None:
    msg, _, _ = await _attempt(sent_at=datetime.now(UTC) + timedelta(seconds=skew))
    async with SessionLocal() as session:
        assert await accept_notice(session, _notice(msg))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "local_age,platform_age,expected",
    [
        (-1, 1, False),
        (0, -1, False),
        (300, 300, True),
        (301, 10, False),
        (10, 301, False),
        (10, 0, True),
    ],
)
async def test_both_confirmation_windows_are_bounded(local_age, platform_age, expected) -> None:
    msg, _, intents = await _attempt()
    async with SessionLocal() as session:
        row = await session.get(RecallConfirmation, intents[0].id)
        assert (
            await accept_notice(
                session,
                _notice(
                    msg,
                    occurred_at=row.requested_at.replace(tzinfo=UTC, microsecond=0)
                    + timedelta(seconds=platform_age),
                ),
                received_at=row.requested_at.replace(tzinfo=UTC) + timedelta(seconds=local_age),
            )
            is expected
        )


@pytest.mark.asyncio
async def test_expired_confirmation_is_not_failed_or_rearmed() -> None:
    msg, _, intents = await _attempt()
    async with SessionLocal() as session:
        row = await session.get(RecallConfirmation, intents[0].id)
        original = row.requested_at
        assert not await arm_confirmation(session, intents[0].id, msg, "10000001")
        assert row.requested_at == original
        assert "需人工核实" in confirmation_label(row, now=original + timedelta(seconds=301))
        assert "历史记录" in confirmation_label(None)


@pytest.mark.asyncio
async def test_ambiguous_short_id_does_not_confirm_either_request() -> None:
    msg, _, intents = await _attempt()
    async with SessionLocal() as session:
        duplicate = ActionIntent(
            idempotency_key=uuid4().hex,
            action="recall",
            status="SUCCEEDED",
            provider="onebot",
            group_openid=msg.external_group_id,
            message_id=msg.message_id,
            external_group_id=msg.external_group_id,
            external_user_id=msg.external_user_id,
            external_message_id=msg.external_message_id,
        )
        session.add(duplicate)
        await session.commit()
        assert await arm_confirmation(session, duplicate.id, msg, "10000001")
        assert not await accept_notice(session, _notice(msg))
        for intent_id in (intents[0].id, duplicate.id):
            assert (await session.get(RecallConfirmation, intent_id)).confirmed_at is None


@pytest.mark.asyncio
async def test_notice_does_not_replace_failed_api_result() -> None:
    from app.core.contracts import ActionResult

    class FailedButNoticed(_FakeOneBotClient):
        async def recall(self, group, mid, /, *, actor="system"):
            self.calls.append(("recall", (group, mid, actor)))
            async with SessionLocal() as session:
                assert await accept_notice(
                    session,
                    RecallNotice("10000001", group, "1001", mid, "10000001", datetime.now(UTC)),
                )
            return ActionResult(action="recall", ok=False, err_message="test failure")

    _, client, intents = await _attempt(client=FailedButNoticed())
    assert [(i.action, i.status) for i in intents] == [("recall", "FAILED")]
    async with SessionLocal() as session:
        assert (await session.get(RecallConfirmation, intents[0].id)).confirmed_at is not None
    assert [c[0] for c in client.calls] == ["recall"]


@pytest.mark.asyncio
async def test_unknown_api_result_preserves_early_notice_and_does_not_escalate() -> None:
    class UnknownButNoticed(_FakeOneBotClient):
        async def recall(self, group, mid, /, *, actor="system"):
            self.calls.append(("recall", (group, mid, actor)))
            async with SessionLocal() as session:
                assert await accept_notice(
                    session,
                    RecallNotice("10000001", group, "1001", mid, "10000001", datetime.now(UTC)),
                )
            raise TimeoutError("test timeout")

    _, client, intents = await _attempt(client=UnknownButNoticed())
    assert [(i.action, i.status) for i in intents] == [("recall", "UNKNOWN")]
    async with SessionLocal() as session:
        assert (await session.get(RecallConfirmation, intents[0].id)).confirmed_at is not None
    assert [c[0] for c in client.calls] == ["recall"]


@pytest.mark.asyncio
async def test_concurrent_duplicate_notice_is_idempotent() -> None:
    msg, _, _ = await _attempt()
    notice = _notice(msg)

    async def receive():
        async with SessionLocal() as session:
            return await accept_notice(session, notice)

    assert sorted(await asyncio.gather(receive(), receive())) == [False, True]


@pytest.mark.asyncio
async def test_seen_notice_cannot_be_assigned_to_a_later_request() -> None:
    msg, _, intents = await _attempt()
    notice = _notice(msg)
    async with SessionLocal() as session:
        assert await accept_notice(session, notice)
        duplicate = ActionIntent(
            idempotency_key=uuid4().hex,
            action="recall",
            status="SUCCEEDED",
            provider="onebot",
            group_openid=msg.external_group_id,
            message_id=msg.message_id,
            external_group_id=msg.external_group_id,
            external_user_id=msg.external_user_id,
            external_message_id=msg.external_message_id,
        )
        session.add(duplicate)
        await session.commit()
        assert await arm_confirmation(session, duplicate.id, msg, "10000001")
        await session.execute(
            update(RecallConfirmation)
            .where(RecallConfirmation.intent_id == intents[0].id)
            .values(requested_at=datetime.now(UTC) - timedelta(seconds=400))
        )
        await session.commit()
        assert not await accept_notice(session, notice)
        assert (await session.get(RecallConfirmation, duplicate.id)).confirmed_at is None


@pytest.mark.asyncio
async def test_slow_message_processing_does_not_cross_compare_qq_clock() -> None:
    msg, _, _ = await _attempt(sent_at=datetime.now(UTC) - timedelta(seconds=400))
    async with SessionLocal() as session:
        assert await accept_notice(session, _notice(msg))


def test_notice_storage_failure_degrades_socket_without_retry(monkeypatch) -> None:
    from app.main import app

    from tests.test_onebot_ws import _auth_headers, _lifecycle, _status, _wait_for

    msg, _, _ = asyncio.run(_attempt())
    calls = []

    async def fail(*args, **kwargs):
        calls.append(1)
        raise OSError("storage unavailable")

    monkeypatch.setattr("app.actions.recall_confirmation.accept_notice", fail)
    with TestClient(app) as client:
        with client.websocket_connect("/onebot/ws", headers=_auth_headers()) as ws:
            ws.send_text(_lifecycle())
            ws.send_json(_event(msg))
            assert _wait_for(
                lambda: "recall_notice_storage" in str(_status(client).get("last_error"))
            )
        assert calls == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role,account", [("admin", "10000001"), ("owner", "10000001"), ("member", "10000002")]
)
async def test_protection_and_wrong_receiving_account_do_not_send(role, account) -> None:
    _, client, intents = await _attempt(role=role, account=account)
    assert intents[0].status == "SKIPPED" and client.calls == []
    async with SessionLocal() as session:
        assert await session.get(RecallConfirmation, intents[0].id) is None


@pytest.mark.asyncio
async def test_storage_failure_before_request_never_sends(monkeypatch) -> None:
    async def broken(*args, **kwargs):
        raise OSError("synthetic storage failure")

    monkeypatch.setattr("app.actions.orchestrator.arm_confirmation", broken)
    _, client, intents = await _attempt()
    assert client.calls == [] and intents[0].status == "UNKNOWN"


def _event(msg):
    return {
        "post_type": "notice",
        "notice_type": "group_recall",
        "self_id": 10000001,
        "group_id": int(msg.external_group_id),
        "user_id": 1001,
        "message_id": int(msg.external_message_id),
        "operator_id": 10000001,
        "time": int(datetime.now(UTC).timestamp()),
    }


@pytest.mark.parametrize(
    "key,value",
    [
        ("self_id", True),
        ("group_id", "12"),
        ("user_id", None),
        ("operator_id", -1),
        ("message_id", 1.5),
        ("message_id", 2**64),
        ("time", float("inf")),
        ("time", True),
        ("time", 253402300800),
        ("post_type", "message"),
        ("notice_type", "friend_recall"),
        ("self_id", 10000002),
    ],
)
def test_notice_contract_rejects_malformed_values(key, value) -> None:
    msg = _ob_msg("12345", "1001", mid="-1234")
    msg.sent_at = datetime.now(UTC)
    event = _event(msg)
    assert parse_recall_notice(event, expected_self_id="10000001") is not None
    event[key] = value
    assert parse_recall_notice(event, expected_self_id="10000001") is None


def test_authenticated_websocket_records_notice_and_detail_refreshes() -> None:
    from app.main import app
    from app.runtime.models import ShadowDecision

    from tests.test_onebot_ws import _auth_headers, _lifecycle, _wait_for

    async def seed():
        msg, client, intents = await _attempt()
        key = f"onebot:10000001:{msg.message_id}"
        async with SessionLocal() as session:
            session.add(
                ShadowDecision(
                    message_id=key,
                    external_message_id=msg.external_message_id,
                    group_openid=msg.external_group_id,
                    member_openid=msg.external_user_id,
                    provider="onebot",
                    external_group_id=msg.external_group_id,
                    external_user_id=msg.external_user_id,
                    kind="share_card",
                    verdict="violation_high",
                    detail_json=json.dumps({"action_intents": summarize_intents(intents)}),
                )
            )
            await session.commit()
        return msg, key

    msg, key = asyncio.run(seed())
    with TestClient(app) as client:
        assert (
            client.post(
                "/admin/login",
                data={"username": "admin", "password": "test-admin-pass"},
                follow_redirects=False,
            ).status_code
            == 303
        )

        def page():
            return client.get("/admin/shadow/detail", params={"message_id": key}).text

        assert "接口返回成功" in page() and "等待 QQ 撤回通知" in page()
        with client.websocket_connect("/onebot/ws", headers=_auth_headers()) as ws:
            ws.send_text(_lifecycle())
            ws.send_json(_event(msg))
            assert _wait_for(lambda: "已收到 QQ 撤回通知" in page())
        # A restart/new page read must use persisted evidence, not the frozen snapshot.
    with TestClient(app) as client:
        client.post("/admin/login", data={"username": "admin", "password": "test-admin-pass"})
        assert (
            "已收到 QQ 撤回通知"
            in client.get("/admin/shadow/detail", params={"message_id": key}).text
        )


@pytest.mark.asyncio
async def test_reports_separate_api_success_confirmation_and_history() -> None:
    from app.reports.service import build_daily, build_weekly

    day = datetime(2032, 1, 2, tzinfo=UTC)
    ids = []
    for _ in range(3):
        msg, _, intents = await _attempt()
        ids.append(intents[0].id)
        if len(ids) == 1:
            async with SessionLocal() as session:
                assert await accept_notice(session, _notice(msg))
    async with SessionLocal() as session:
        from app.models import ActionLog
        from sqlalchemy import delete

        await session.execute(
            update(ActionIntent).where(ActionIntent.id.in_(ids)).values(created_at=day)
        )
        # Synthetic historical API success has no confirmation registration.
        await session.execute(
            delete(RecallConfirmation).where(RecallConfirmation.intent_id == ids[-1])
        )
        await session.execute(
            update(ActionLog)
            .where(
                ActionLog.external_message_id.in_(
                    select(ActionIntent.external_message_id).where(ActionIntent.id.in_(ids))
                )
            )
            .values(created_at=day)
        )
        await session.commit()
        daily = await build_daily(session, day)
        weekly = await build_weekly(session, day)
        assert daily["actions_ok"] == 3
        for report in (daily, weekly):
            assert [
                report[f"onebot_recall_{key}"] for key in ("confirmed", "unconfirmed", "untracked")
            ] == [1, 1, 1]


@pytest.mark.asyncio
async def test_confirmation_cleanup_keeps_intent_and_no_false_confirmation(
    monkeypatch, tmp_path
) -> None:
    from app.reports.cleanup import purge_expired

    monkeypatch.setattr("app.runtime.pipeline.MEDIA_DIR", tmp_path)
    msg, _, intents = await _attempt()
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        await session.execute(
            update(RecallConfirmation)
            .where(RecallConfirmation.intent_id == intents[0].id)
            .values(requested_at=now - timedelta(days=181))
        )
        await session.commit()
        result = await purge_expired(session, now=now)
        assert result["recall_confirmations_deleted"] >= 1
        assert await session.get(RecallConfirmation, intents[0].id) is None
        assert (await session.get(ActionIntent, intents[0].id)).status == "SUCCEEDED"
        assert not await accept_notice(session, _notice(msg))
