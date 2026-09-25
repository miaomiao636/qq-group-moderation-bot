"""T-106 guarded official action orchestration tests."""

from __future__ import annotations

import uuid

import pytest
from app.actions.orchestrator import (
    ActionIntent,
    _create_intent,
    _execute_intent,
    orchestrate_actions,
)
from app.adapters.qq_official.actions import ActionResult
from app.adapters.qq_official.contract import Sender, StandardMessage
from app.cases.models import Case, ViolationRecord
from app.config import Settings
from app.db import SessionLocal
from app.models import ActionLog
from app.moderation.decision import ModerationDecision
from app.runtime.pipeline import run_pipeline
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


class FakeOfficialClient:
    def __init__(self, fail_action: str = "") -> None:
        self.fail_action = fail_action
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def recall(
        self, group_openid: str, message_id: str, *, actor: str = "system"
    ) -> ActionResult:
        self.calls.append(("recall", (group_openid, message_id, actor)))
        if self.fail_action == "recall":
            raise TimeoutError("unknown")
        return ActionResult(action="recall", ok=True, status_code=200, attempts=1)

    async def mute(
        self, group_openid: str, member_openid: str, seconds: int, *, actor: str = "system"
    ) -> ActionResult:
        self.calls.append(("mute", (group_openid, member_openid, seconds, actor)))
        if self.fail_action == "mute":
            raise TimeoutError("unknown")
        return ActionResult(action="mute", ok=True, status_code=200, attempts=1)

    async def warn(
        self,
        group_openid: str,
        reply_to_message_id: str,
        text: str,
        *,
        msg_seq: int = 1,
        actor: str = "system",
    ) -> ActionResult:
        self.calls.append(("warn", (group_openid, reply_to_message_id, text, actor)))
        if self.fail_action == "warn":
            raise TimeoutError("unknown")
        return ActionResult(action="warn", ok=True, status_code=200, attempts=1)


def _official_settings() -> Settings:
    return Settings(
        app_env="prod",
        admin_password="strong-admin-pass",
        qq_app_id="APP",
        qq_app_secret="SECRET",
        action_mode="OFFICIAL",
        _env_file=None,
    )


async def _enable_actions(session: AsyncSession, group_openid: str) -> None:
    """T-303 UX：OFFICIAL 模式测试需显式为测试群启用动作。"""
    from app.models import ProviderGroupSettings

    gs = await session.get(ProviderGroupSettings, ("qq_official", group_openid))
    if gs is None:
        gs = ProviderGroupSettings(
            provider="qq_official", external_group_id=group_openid, action_enabled=True
        )
        session.add(gs)
    else:
        gs.action_enabled = True
    await session.commit()


def _msg(
    group: str, member: str, *, message_id: str | None = None, role: str = "member"
) -> StandardMessage:
    return StandardMessage(
        message_id=message_id or f"ACT_MSG_{uuid.uuid4().hex[:8]}",
        group_openid=group,
        sender=Sender(member_openid=member, role=role),
        text="违规测试内容",
    )


def _high_decision(msg: StandardMessage) -> ModerationDecision:
    return ModerationDecision(
        message_id=msg.message_id,
        group_openid=msg.group_openid,
        sender_member_openid=msg.sender.member_openid,
        sender_role=msg.sender.role,
        verdict="violation_high",
        category="ad",
        confidence=0.95,
        recommended_actions=["recall", "mute", "warn"],
        reason="测试高置信违规",
        is_protected_sender=msg.sender.role in ("owner", "admin"),
    )


@pytest.mark.asyncio
async def test_default_shadow_never_calls_official_client() -> None:
    client = FakeOfficialClient()
    group = f"G_ACT_SHADOW_{uuid.uuid4().hex[:6]}"
    msg = _msg(group, "M1")
    async with SessionLocal() as session:
        intents = await orchestrate_actions(
            session,
            msg,
            _high_decision(msg),
            official_client=client,
            settings=Settings(action_mode="SHADOW", _env_file=None),
        )
    assert intents == []
    assert client.calls == []


def test_official_mode_requires_explicit_prerequisites() -> None:
    with pytest.raises(ValueError, match="ACTION_MODE=OFFICIAL"):
        Settings(action_mode="OFFICIAL", app_env="local", _env_file=None)
    with pytest.raises(ValueError, match="EMERGENCY_STOP=false"):
        Settings(
            app_env="prod",
            admin_password="strong-admin-pass",
            qq_app_id="APP",
            qq_app_secret="SECRET",
            action_mode="OFFICIAL",
            emergency_stop=True,
            _env_file=None,
        )


@pytest.mark.asyncio
async def test_official_first_strike_persists_intents_then_calls() -> None:
    client = FakeOfficialClient()
    group = f"G_ACT_FIRST_{uuid.uuid4().hex[:6]}"
    member = "M1"
    msg = _msg(group, member)
    async with SessionLocal() as session:
        await _enable_actions(session, group)
        intents = await orchestrate_actions(
            session,
            msg,
            _high_decision(msg),
            official_client=client,
            settings=_official_settings(),
        )
        action_log_count = (
            await session.execute(
                select(func.count()).select_from(ActionLog).where(ActionLog.group_openid == group)
            )
        ).scalar_one()

    # Legacy recommendations must never become new punishment intents.
    assert [intent.action for intent in intents] == ["recall"]
    assert {intent.status for intent in intents} == {"SUCCEEDED"}
    assert [call[0] for call in client.calls] == ["recall"]
    assert action_log_count == 1
    assert "kick" not in {intent.action for intent in intents}


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_action", ["mute", "warn"])
async def test_persisted_legacy_punishment_intents_are_never_sent(legacy_action: str) -> None:
    client = FakeOfficialClient()
    group = f"G_ACT_LEGACY_{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        pending = ActionIntent(
            idempotency_key=uuid.uuid4().hex,
            action=legacy_action,
            status="PENDING",
            group_openid=group,
            message_id="OLD_1",
            external_group_id=group,
            external_message_id="OLD_1",
        )
        completed = ActionIntent(
            idempotency_key=uuid.uuid4().hex,
            action=legacy_action,
            status="SUCCEEDED",
            group_openid=group,
            message_id="OLD_2",
            external_group_id=group,
            external_message_id="OLD_2",
            result_json='{"legacy":true}',
        )
        historical_log = ActionLog(
            action=legacy_action,
            group_openid=group,
            message_id="OLD_2",
            ok=True,
            attempts=1,
        )
        session.add_all([pending, completed, historical_log])
        await session.commit()
        result = await _execute_intent(session, client, pending)
        assert result is not None and result.attempts == 0
        assert pending.status == "SKIPPED"
        assert await _execute_intent(session, client, completed) is None
        await session.refresh(completed)
        await session.refresh(historical_log)
        assert completed.status == "SUCCEEDED"
        assert completed.result_json == '{"legacy":true}'
        assert historical_log.action == legacy_action and historical_log.ok is True
        assert (
            await session.execute(
                select(func.count()).select_from(ActionLog).where(ActionLog.group_openid == group)
            )
        ).scalar_one() == 1
    assert client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_action", ["mute", "warn"])
async def test_new_punishment_intent_cannot_be_created(legacy_action: str) -> None:
    msg = _msg(f"G_ACT_DENY_{uuid.uuid4().hex[:6]}", "M1")
    async with SessionLocal() as session:
        with pytest.raises(ValueError, match="非法动作"):
            await _create_intent(session, msg, legacy_action, {"external_user_id": "M1"}, "system")
        count = (
            await session.execute(
                select(func.count())
                .select_from(ActionIntent)
                .where(ActionIntent.message_id == msg.message_id)
            )
        ).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_official_second_strike_still_only_recalls() -> None:
    client = FakeOfficialClient()
    group = f"G_ACT_SECOND_{uuid.uuid4().hex[:6]}"
    member = "M1"
    async with SessionLocal() as session:
        await _enable_actions(session, group)
        first = _msg(group, member)
        await orchestrate_actions(
            session,
            first,
            _high_decision(first),
            official_client=client,
            settings=_official_settings(),
        )
        second = _msg(group, member)
        intents = await orchestrate_actions(
            session,
            second,
            _high_decision(second),
            official_client=client,
            settings=_official_settings(),
        )
        case = (
            await session.execute(select(Case).where(Case.group_openid == group))
        ).scalar_one_or_none()

    assert [intent.action for intent in intents] == ["recall"]
    assert [call[0] for call in client.calls] == ["recall", "recall"]
    assert case is None


@pytest.mark.asyncio
async def test_duplicate_message_does_not_replay_or_create_second_violation() -> None:
    client = FakeOfficialClient()
    group = f"G_ACT_DUP_{uuid.uuid4().hex[:6]}"
    member = "M1"
    msg = _msg(group, member)
    async with SessionLocal() as session:
        await _enable_actions(session, group)
        await orchestrate_actions(
            session,
            msg,
            _high_decision(msg),
            official_client=client,
            settings=_official_settings(),
        )
        intents = await orchestrate_actions(
            session,
            msg,
            _high_decision(msg),
            official_client=client,
            settings=_official_settings(),
        )
        violation_count = (
            await session.execute(
                select(func.count())
                .select_from(ViolationRecord)
                .where(ViolationRecord.message_id == msg.message_id)
            )
        ).scalar_one()

    assert len(intents) == 1
    assert [call[0] for call in client.calls] == ["recall"]
    assert violation_count == 1


@pytest.mark.asyncio
async def test_same_message_id_from_onebot_does_not_reuse_official_intents() -> None:
    client = FakeOfficialClient()
    group = f"G_ACT_NAMESPACE_{uuid.uuid4().hex[:6]}"
    message_id = f"ACT_SHARED_{uuid.uuid4().hex[:8]}"
    onebot = StandardMessage(
        message_id=message_id,
        provider="onebot",
        external_group_id=group,
        external_user_id="200000003",
        sender=Sender(member_openid="200000003"),
        text="违规测试内容",
    )
    official = _msg(group, "M_OFFICIAL", message_id=message_id)
    async with SessionLocal() as session:
        await _enable_actions(session, group)
        onebot_intents = await orchestrate_actions(
            session,
            onebot,
            _high_decision(onebot),
            official_client=client,
            settings=_official_settings(),
        )
        official_intents = await orchestrate_actions(
            session,
            official,
            _high_decision(official),
            official_client=client,
            settings=_official_settings(),
        )

    assert [intent.status for intent in onebot_intents] == ["SKIPPED"]
    assert [intent.action for intent in official_intents] == ["recall"]
    assert [call[0] for call in client.calls] == ["recall"]


@pytest.mark.asyncio
async def test_unknown_action_result_is_not_replayed() -> None:
    client = FakeOfficialClient(fail_action="recall")
    group = f"G_ACT_UNKNOWN_{uuid.uuid4().hex[:6]}"
    msg = _msg(group, "M1")
    async with SessionLocal() as session:
        await _enable_actions(session, group)
        intents = await orchestrate_actions(
            session,
            msg,
            _high_decision(msg),
            official_client=client,
            settings=_official_settings(),
        )
        second = await orchestrate_actions(
            session,
            msg,
            _high_decision(msg),
            official_client=client,
            settings=_official_settings(),
        )

    assert len(intents) == 1
    assert intents[0].status == "UNKNOWN"
    assert second[0].status == "UNKNOWN"
    assert [call[0] for call in client.calls] == ["recall"]


@pytest.mark.asyncio
async def test_protected_sender_is_blocked_at_orchestrator_layer() -> None:
    client = FakeOfficialClient()
    group = f"G_ACT_PROTECTED_{uuid.uuid4().hex[:6]}"
    msg = _msg(group, "M_OWNER", role="owner")
    async with SessionLocal() as session:
        intents = await orchestrate_actions(
            session,
            msg,
            _high_decision(msg),
            official_client=client,
            settings=_official_settings(),
        )

    assert len(intents) == 1
    assert intents[0].status == "SKIPPED"
    assert client.calls == []


@pytest.mark.asyncio
async def test_pipeline_default_shadow_never_calls_official_client() -> None:
    client = FakeOfficialClient()
    group = f"G_ACT_PIPE_{uuid.uuid4().hex[:6]}"
    payload = {
        "id": f"ACT_PIPE_{uuid.uuid4().hex[:8]}",
        "group_openid": group,
        "group_id": group,
        "author": {
            "member_openid": "M_ACT_PIPE",
            "member_role": "member",
            "bot": False,
            "username": "tester",
        },
        "content": "刷单兼职加我微信abcde12345",
        "attachments": [],
        "timestamp": "2026-09-06T10:00:00+08:00",
    }
    async with SessionLocal() as session:
        record = await run_pipeline(payload, session, official_action_client=client)

    assert record is not None
    assert record.verdict == "violation_high"
    assert client.calls == []
