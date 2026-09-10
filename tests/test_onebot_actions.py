"""T-307 OneBot action adapter tests (P0-7: async/retcode semantics)."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from app.actions.orchestrator import orchestrate_actions
from app.adapters.onebot.actions import OneBotActionClient, OneBotActionError
from app.adapters.qq_official.actions import ActionResult
from app.adapters.qq_official.contract import Sender, StandardMessage
from app.config import Settings
from app.core.routing import upsert_group_route
from app.db import SessionLocal
from app.models import ProviderGroupSettings
from app.moderation.decision import ModerationDecision
from app.runtime.onebot_actions import OneBotActionHub
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class _FakeCaller:
    def __init__(self, *, response: dict | None = None, error: OneBotActionError | None = None):
        self.response = response
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, action: str, params: Any) -> dict[str, Any]:
        self.calls.append((action, dict(params)))
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return dict(self.response)


def test_recall_success_ok_status() -> None:
    client = OneBotActionClient(_FakeCaller(response={"status": "ok", "retcode": 0}))
    result = asyncio.run(client.recall("100", "-123456789"))
    assert result.ok and result.action == "recall" and result.attempts == 1
    assert client._caller.calls == [("delete_msg", {"message_id": -123456789})]


def test_recall_async_is_unknown_not_success() -> None:
    """P0-7: status=async means queued, not confirmed success -> UNKNOWN."""
    caller = _FakeCaller(response={"status": "async", "retcode": 0})
    with pytest.raises(OneBotActionError) as exc:
        asyncio.run(OneBotActionClient(caller).recall("100", "999"))
    assert exc.value.kind == "response_lost"


def test_ok_with_nonzero_retcode_is_unknown() -> None:
    """P0-7: status=ok but retcode!=0 is contradictory -> UNKNOWN."""
    caller = _FakeCaller(response={"status": "ok", "retcode": 1})
    with pytest.raises(OneBotActionError) as exc:
        asyncio.run(OneBotActionClient(caller).recall("100", "999"))
    assert exc.value.kind == "response_lost"


def test_explicit_failure_returned_as_failed() -> None:
    caller = _FakeCaller(response={"status": "failed", "retcode": 100, "wording": "no perm"})
    result = asyncio.run(OneBotActionClient(caller).recall("100", "999"))
    assert not result.ok and result.err_code == 100 and "no perm" in result.err_message


@pytest.mark.parametrize(
    "response",
    [
        {"status": "failed"},
        {"status": "failed", "retcode": 0},
        {"status": "ok", "retcode": False},
        {"status": "ok", "retcode": 0.0},
        {"status": "failed", "retcode": "100"},
    ],
)
def test_malformed_onebot_response_is_unknown(response: dict) -> None:
    with pytest.raises(OneBotActionError, match="响应"):
        asyncio.run(OneBotActionClient(_FakeCaller(response=response)).recall("100", "999"))


def test_pre_send_not_ready_is_failed_not_unknown() -> None:
    caller = _FakeCaller(error=OneBotActionError("not_ready", "degraded"))
    result = asyncio.run(OneBotActionClient(caller).recall("100", "999"))
    assert not result.ok


def test_pre_send_disconnected_is_failed_not_unknown() -> None:
    caller = _FakeCaller(error=OneBotActionError("disconnected", "no ws"))
    result = asyncio.run(OneBotActionClient(caller).recall("100", "999"))
    assert not result.ok


def test_post_send_timeout_propagates_for_unknown() -> None:
    caller = _FakeCaller(error=OneBotActionError("timeout", "10s"))
    with pytest.raises(OneBotActionError) as exc:
        asyncio.run(OneBotActionClient(caller).recall("100", "999"))
    assert exc.value.kind == "timeout"


def test_non_numeric_message_id_is_failed() -> None:
    client = OneBotActionClient(_FakeCaller(response={"status": "ok", "retcode": 0}))
    result = asyncio.run(client.recall("100", "not-a-number"))
    assert not result.ok
    assert client._caller.calls == []


def test_non_numeric_group_id_is_failed() -> None:
    client = OneBotActionClient(_FakeCaller(response={"status": "ok", "retcode": 0}))
    result = asyncio.run(client.mute("xyz", "200", 3600))
    assert not result.ok


def test_mute_out_of_range_seconds_is_failed() -> None:
    client = OneBotActionClient(_FakeCaller(response={"status": "ok", "retcode": 0}))
    assert not asyncio.run(client.mute("100", "200", 0)).ok
    assert not asyncio.run(client.mute("100", "200", 30 * 24 * 3600 + 1)).ok


def test_mute_sends_set_group_ban_with_int_ids() -> None:
    caller = _FakeCaller(response={"status": "ok", "retcode": 0})
    asyncio.run(OneBotActionClient(caller).mute("100", "200", 3600))
    assert caller.calls == [("set_group_ban", {"group_id": 100, "user_id": 200, "duration": 3600})]


def test_warn_sends_send_group_msg_with_reply_and_text() -> None:
    caller = _FakeCaller(response={"status": "ok", "retcode": 0})
    asyncio.run(OneBotActionClient(caller).warn("100", "999", "stop"))
    assert caller.calls and caller.calls[0][0] == "send_group_msg"
    params = caller.calls[0][1]
    assert params["group_id"] == 100
    msg = params["message"]
    assert msg[0] == {"type": "reply", "data": {"id": 999}}
    assert msg[1] == {"type": "text", "data": {"text": "stop"}}


def test_warn_empty_text_is_failed() -> None:
    client = OneBotActionClient(_FakeCaller(response={"status": "ok", "retcode": 0}))
    assert not asyncio.run(client.warn("100", "999", "   ")).ok


def test_invalid_response_missing_status_propagates() -> None:
    caller = _FakeCaller(response={"retcode": 0})
    with pytest.raises(OneBotActionError) as exc:
        asyncio.run(OneBotActionClient(caller).recall("100", "999"))
    assert exc.value.kind == "response_lost"


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, frame: str) -> None:
        self.sent.append(frame)


def _ready_hub(ws: _FakeWS | None, *, ready: bool = True) -> OneBotActionHub:
    hub = OneBotActionHub()
    hub.configure(
        timeout_seconds=0.2,
        readiness=lambda: "ready" if ready else "degraded",
        expected_self_id="10000001",
    )
    if ws is not None:
        hub.bind(ws, self_id="10000001")
    return hub


def test_hub_rejects_second_connection_and_foreign_identity() -> None:
    first, second = _FakeWS(), _FakeWS()
    hub = _ready_hub(first)
    assert hub.bind(second, self_id="10000002") is False
    assert hub.bind(second, self_id="10000001") is False
    assert hub.is_current(first)
    hub.unbind(first)
    assert hub.bind(second, self_id="10000002") is False
    assert hub.bind(second, self_id="10000001") is True


def test_response_from_other_socket_or_identity_cannot_resolve_pending() -> None:
    async def run() -> None:
        ws, other = _FakeWS(), _FakeWS()
        hub = _ready_hub(ws)
        task = asyncio.create_task(hub.call("delete_msg", {"message_id": 1}))
        await asyncio.sleep(0)
        frame = json.loads(ws.sent[0])
        response = {"echo": frame["echo"], "status": "ok", "retcode": 0}
        assert not hub.handle_response(response, websocket=other)
        assert not hub.handle_response(dict(response, self_id=10000002), websocket=ws)
        assert hub.handle_response(response, websocket=ws)
        assert (await task)["retcode"] == 0

    asyncio.run(run())


def test_hub_call_roundtrip_resolves_by_echo() -> None:
    ws = _FakeWS()
    hub = _ready_hub(ws)

    async def main() -> dict:
        task = asyncio.create_task(hub.call("delete_msg", {"message_id": 1}))

        async def replier() -> None:
            await asyncio.sleep(0.05)
            frame = json.loads(ws.sent[0])
            consumed = hub.handle_response(
                {"status": "ok", "retcode": 0, "echo": frame["echo"], "data": {}}, websocket=ws
            )
            assert consumed

        t = asyncio.create_task(replier())
        r = await task
        await t
        return r

    resp = asyncio.run(main())
    assert resp["status"] == "ok"


def test_hub_call_timeout_when_no_response() -> None:
    ws = _FakeWS()
    hub = _ready_hub(ws)

    async def main() -> None:
        with pytest.raises(OneBotActionError) as exc:
            await hub.call("delete_msg", {"message_id": 1})
        assert exc.value.kind == "timeout"

    asyncio.run(main())


def test_hub_send_deadline_is_unknown_and_lock_wait_deadline_is_not_sent() -> None:
    class BlockedWS(_FakeWS):
        async def send_text(self, data: str) -> None:
            self.sent.append(json.loads(data))
            await asyncio.Event().wait()

    async def main() -> None:
        ws = BlockedWS()
        hub = _ready_hub(ws)
        hub.configure(timeout_seconds=0.02, readiness=lambda: "ready", expected_self_id="10000001")
        with pytest.raises(OneBotActionError) as exc:
            await asyncio.wait_for(hub.call("delete_msg", {"message_id": 1}), timeout=0.2)
        assert exc.value.kind == "timeout"
        assert len(ws.sent) == 1
        assert not hub._pending
        assert hub._send_lock is not None
        await hub._send_lock.acquire()
        try:
            with pytest.raises(OneBotActionError) as exc:
                await asyncio.wait_for(hub.call("delete_msg", {"message_id": 2}), timeout=0.2)
            assert exc.value.kind == "not_ready"
            assert len(ws.sent) == 1
            assert not hub._pending
        finally:
            hub._send_lock.release()

    asyncio.run(main())


def test_hub_unbind_fails_pending_as_response_lost() -> None:
    ws = _FakeWS()
    hub = _ready_hub(ws)

    async def main() -> None:
        task = asyncio.create_task(hub.call("delete_msg", {"message_id": 1}))
        await asyncio.sleep(0.02)
        hub.unbind(ws)
        with pytest.raises(OneBotActionError) as exc:
            await task
        assert exc.value.kind == "response_lost"

    asyncio.run(main())


def _raise_call(hub: OneBotActionHub, expected_kind: str) -> Any:
    async def _() -> None:
        with pytest.raises(OneBotActionError) as exc:
            await hub.call("delete_msg", {"message_id": 1})
        assert exc.value.kind == expected_kind

    return _()


def test_hub_call_disconnected_when_unbound() -> None:
    hub = _ready_hub(None)
    asyncio.run(_raise_call(hub, "disconnected"))


def test_hub_call_not_ready_when_degraded() -> None:
    ws = _FakeWS()
    hub = _ready_hub(ws, ready=False)
    asyncio.run(_raise_call(hub, "not_ready"))


def test_hub_handle_response_ignores_events_and_unknown_echo() -> None:
    ws = _FakeWS()
    hub = _ready_hub(ws)
    assert hub.handle_response({"post_type": "message", "echo": "x"}, websocket=ws) is False
    assert hub.handle_response({"echo": "never-sent"}, websocket=ws) is False
    assert hub.handle_response({"status": "ok", "retcode": 0}, websocket=ws) is False


def test_onebot_actions_enabled_requires_ws() -> None:
    from app.config import Settings

    with pytest.raises(ValueError, match="ONEBOT_WS_ENABLED"):
        Settings(
            onebot_ws_enabled=False,
            onebot_actions_enabled=True,
            onebot_self_id="10000001",
            _env_file=None,
        )


class _FakeOneBotClient:
    def __init__(self, *, fail_action: str = "", fail_kind: str = "timeout") -> None:
        self.fail_action = fail_action
        self.fail_kind = fail_kind
        self.calls: list[tuple[str, tuple]] = []

    async def recall(self, group, mid, /, *, actor="system") -> ActionResult:
        self.calls.append(("recall", (group, mid, actor)))
        if self.fail_action == "recall":
            raise OneBotActionError(self.fail_kind, "test timeout")
        return ActionResult(action="recall", ok=True, status_code=0, attempts=1)

    async def mute(self, group, user, seconds, /, *, actor="system") -> ActionResult:
        self.calls.append(("mute", (group, user, seconds, actor)))
        if self.fail_action == "mute":
            raise OneBotActionError(self.fail_kind, "test timeout")
        return ActionResult(action="mute", ok=True, status_code=0, attempts=1)

    async def warn(self, group, reply, text, /, *, msg_seq=1, actor="system") -> ActionResult:
        self.calls.append(("warn", (group, reply, text, actor)))
        if self.fail_action == "warn":
            raise OneBotActionError(self.fail_kind, "test timeout")
        return ActionResult(action="warn", ok=True, status_code=0, attempts=1)


def _official_onebot_settings() -> Settings:
    return Settings(
        app_env="prod",
        admin_password="strong-admin-pass",
        qq_app_id="APP",
        qq_app_secret="SECRET",
        action_mode="OFFICIAL",
        onebot_actions_enabled=True,
        onebot_action_stage="full",
        onebot_self_id="10000001",
        _env_file=None,
    )


def _ob_msg(
    group: str, member: str, *, mid: str | None = None, role: str = "member"
) -> StandardMessage:
    ext_mid = mid or str(uuid.uuid4().int)[:12]
    return StandardMessage(
        message_id=ext_mid,
        provider="onebot",
        external_group_id=group,
        external_user_id=member,
        external_message_id=ext_mid,
        sender=Sender(member_openid=member, role=role),
        text="violation test content",
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
        reason="test high confidence violation",
        is_protected_sender=msg.sender.role in ("owner", "admin"),
    )


async def _setup_onebot_group(session: AsyncSession, group: str, *, enabled: bool = True) -> None:
    await upsert_group_route(session, group, message_provider="onebot", action_provider="onebot")
    gs = await session.get(ProviderGroupSettings, ("onebot", group))
    if gs is None:
        gs = ProviderGroupSettings(
            provider="onebot", external_group_id=group, action_enabled=enabled
        )
        session.add(gs)
    else:
        gs.action_enabled = enabled
    await session.commit()


@pytest.mark.asyncio
async def test_official_shadow_never_calls_onebot_client() -> None:
    client = _FakeOneBotClient()
    group = f"OB_SHADOW_{uuid.uuid4().hex[:6]}"
    msg = _ob_msg(group, "1001")
    async with SessionLocal() as session:
        await _setup_onebot_group(session, group)
        intents = await orchestrate_actions(
            session,
            msg,
            _high_decision(msg),
            onebot_client=client,
            settings=Settings(action_mode="SHADOW", _env_file=None),
        )
    assert intents == []
    assert client.calls == []


@pytest.mark.asyncio
async def test_official_onebot_actions_disabled_is_skipped() -> None:
    client = _FakeOneBotClient()
    group = f"OB_DIS_{uuid.uuid4().hex[:6]}"
    msg = _ob_msg(group, "1001")
    async with SessionLocal() as session:
        await _setup_onebot_group(session, group)
        intents = await orchestrate_actions(
            session,
            msg,
            _high_decision(msg),
            onebot_client=client,
            settings=Settings(
                app_env="prod",
                admin_password="p",
                qq_app_id="a",
                qq_app_secret="s",
                action_mode="OFFICIAL",
                onebot_actions_enabled=False,
                _env_file=None,
            ),
        )
    assert len(intents) == 1 and intents[0].status == "SKIPPED"
    assert "ONEBOT_ACTIONS_ENABLED" in intents[0].reason
    assert client.calls == []


@pytest.mark.asyncio
async def test_official_onebot_success_executes_recall_mute_warn() -> None:
    client = _FakeOneBotClient()
    group = f"OB_OK_{uuid.uuid4().hex[:6]}"
    mid = str(uuid.uuid4().int)[:12]
    msg = _ob_msg(group, "1001", mid=mid)
    async with SessionLocal() as session:
        await _setup_onebot_group(session, group)
        intents = await orchestrate_actions(
            session,
            msg,
            _high_decision(msg),
            onebot_client=client,
            settings=_official_onebot_settings(),
        )
    statuses = {i.action: i.status for i in intents}
    assert statuses == {"recall": "SUCCEEDED", "mute": "SUCCEEDED", "warn": "SUCCEEDED"}
    actions_called = [c[0] for c in client.calls]
    assert actions_called == ["recall", "mute", "warn"]


@pytest.mark.asyncio
async def test_timeout_freezes_unknown_and_no_replay() -> None:
    client = _FakeOneBotClient(fail_action="recall", fail_kind="timeout")
    group = f"OB_TO_{uuid.uuid4().hex[:6]}"
    mid = str(uuid.uuid4().int)[:12]
    msg = _ob_msg(group, "1001", mid=mid)
    async with SessionLocal() as session:
        await _setup_onebot_group(session, group)
        intents = await orchestrate_actions(
            session,
            msg,
            _high_decision(msg),
            onebot_client=client,
            settings=_official_onebot_settings(),
        )
    assert len(intents) == 1 and intents[0].status == "UNKNOWN"
    assert client.calls.__len__() == 1
    async with SessionLocal() as session:
        again = await orchestrate_actions(
            session,
            msg,
            _high_decision(msg),
            onebot_client=client,
            settings=_official_onebot_settings(),
        )
    assert again[0].status == "UNKNOWN"
    assert client.calls.__len__() == 1


@pytest.mark.asyncio
async def test_emergency_stop_blocks_official_mode_at_config() -> None:
    with pytest.raises(ValueError, match="EMERGENCY_STOP=false"):
        Settings(
            app_env="prod",
            admin_password="p",
            qq_app_id="a",
            qq_app_secret="s",
            action_mode="OFFICIAL",
            onebot_actions_enabled=True,
            emergency_stop=True,
            _env_file=None,
        )


@pytest.mark.asyncio
async def test_group_action_disabled_is_skipped() -> None:
    client = _FakeOneBotClient()
    group = f"OB_GD_{uuid.uuid4().hex[:6]}"
    msg = _ob_msg(group, "1001")
    async with SessionLocal() as session:
        await _setup_onebot_group(session, group, enabled=False)
        intents = await orchestrate_actions(
            session,
            msg,
            _high_decision(msg),
            onebot_client=client,
            settings=_official_onebot_settings(),
        )
    assert intents and intents[0].status == "SKIPPED"
    assert client.calls == []


@pytest.mark.asyncio
async def test_protected_role_is_skipped() -> None:
    client = _FakeOneBotClient()
    group = f"OB_PRO_{uuid.uuid4().hex[:6]}"
    msg = _ob_msg(group, "1001", role="admin")
    async with SessionLocal() as session:
        await _setup_onebot_group(session, group)
        intents = await orchestrate_actions(
            session,
            msg,
            _high_decision(msg),
            onebot_client=client,
            settings=_official_onebot_settings(),
        )
    assert intents and intents[0].status == "SKIPPED"
    assert client.calls == []


@pytest.mark.asyncio
async def test_no_route_fail_closed() -> None:
    client = _FakeOneBotClient()
    group = f"OB_NR_{uuid.uuid4().hex[:6]}"
    msg = _ob_msg(group, "1001")
    async with SessionLocal() as session:
        gs = await session.get(ProviderGroupSettings, ("onebot", group))
        if gs is None:
            session.add(
                ProviderGroupSettings(
                    provider="onebot", external_group_id=group, action_enabled=True
                )
            )
            await session.commit()
        intents = await orchestrate_actions(
            session,
            msg,
            _high_decision(msg),
            onebot_client=client,
            settings=_official_onebot_settings(),
        )
    assert intents and intents[0].status == "SKIPPED"
    assert client.calls == []


@pytest.mark.asyncio
async def test_action_logs_written_for_audit() -> None:
    from app.models import ActionLog

    client = _FakeOneBotClient()
    group = f"OB_AL_{uuid.uuid4().hex[:6]}"
    mid = str(uuid.uuid4().int)[:12]
    msg = _ob_msg(group, "1001", mid=mid)
    async with SessionLocal() as session:
        await _setup_onebot_group(session, group)
        await orchestrate_actions(
            session,
            msg,
            _high_decision(msg),
            onebot_client=client,
            settings=_official_onebot_settings(),
        )
        logs = (
            (
                await session.execute(
                    select(ActionLog).where(
                        ActionLog.provider == "onebot",
                        ActionLog.external_group_id == group,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(logs) == 3
    assert {log.action for log in logs} == {"recall", "mute", "warn"}
    assert all(log.ok for log in logs)
