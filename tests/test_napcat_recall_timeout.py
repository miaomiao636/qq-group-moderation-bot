"""Local callback timeout is uncertain, never success or a replay invitation."""

import asyncio
import json
import uuid

import pytest
from app.actions.orchestrator import orchestrate_actions
from app.adapters.onebot.actions import OneBotActionClient, OneBotActionError
from app.db import SessionLocal
from app.models import ActionLog
from sqlalchemy import select

from tests.test_onebot_actions import (
    _FakeCaller,
    _high_decision,
    _ob_msg,
    _official_onebot_settings,
    _setup_onebot_group,
)

PREFIX = (
    "Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/recallMsg "
    "ListenerName:NodeIKernelMsgListener/onMsgInfoListUpdate EventRet:"
)


@pytest.mark.parametrize("field", ["wording", "message"])
@pytest.mark.parametrize("tail", ["{}", '{"result":0,"errMsg":""}'])
def test_callback_timeout_is_unknown(field, tail):
    caller = _FakeCaller(response={"status": "failed", "retcode": 1200, field: PREFIX + tail})
    with pytest.raises(OneBotActionError) as exc:
        asyncio.run(OneBotActionClient(caller).recall("100", "999"))
    assert exc.value.kind == "timeout"
    assert len(caller.calls) == 1


def test_generic_wording_does_not_hide_timeout_message():
    caller = _FakeCaller(
        response={
            "status": "failed",
            "retcode": 1200,
            "wording": "操作失败",
            "message": PREFIX + "{}",
        }
    )
    with pytest.raises(OneBotActionError):
        asyncio.run(OneBotActionClient(caller).recall("100", "999"))


@pytest.mark.parametrize(
    "wording",
    [
        "权限不足",
        "Timeout: unrelated",
        "unrelated " + PREFIX,
        PREFIX.replace("recallMsg", "sendMsg"),
        PREFIX.replace("onMsgInfoListUpdate", "onOtherEvent"),
    ],
)
def test_other_failures_stay_failed(wording):
    caller = _FakeCaller(response={"status": "failed", "retcode": 1200, "wording": wording})
    result = asyncio.run(OneBotActionClient(caller).recall("100", "999"))
    assert result.ok is False and result.err_code == 1200


@pytest.mark.parametrize("action", ["mute", "warn"])
def test_other_actions_do_not_use_recall_signature(action):
    caller = _FakeCaller(response={"status": "failed", "retcode": 1200, "wording": PREFIX + "{}"})
    client = OneBotActionClient(caller)
    result = asyncio.run(
        client.mute("100", "200", 60) if action == "mute" else client.warn("100", "999", "test")
    )
    assert result.ok is False and len(caller.calls) == 1


@pytest.mark.asyncio
async def test_callback_timeout_freezes_chain_sanitizes_and_never_replays():
    marker = "private-upstream-value-must-not-be-saved"
    caller = _FakeCaller(
        response={
            "status": "failed",
            "retcode": 1200,
            "message": PREFIX + '{"result":0,"detail":"' + marker + '"}',
        }
    )
    client = OneBotActionClient(caller)
    group = "CALLBACK_" + uuid.uuid4().hex[:12]
    msg = _ob_msg(group, "1001")
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
        assert "回执超时" in intents[0].reason
        assert marker not in intents[0].result_json + intents[0].reason
        result = json.loads(intents[0].result_json)
        assert result["err_code"] == 1200 and result["attempts"] == 1
        again = await orchestrate_actions(
            session,
            msg,
            _high_decision(msg),
            onebot_client=client,
            settings=_official_onebot_settings(),
        )
        assert again[0].id == intents[0].id and again[0].status == "UNKNOWN"
        logs = (
            (await session.execute(select(ActionLog).where(ActionLog.group_openid == group)))
            .scalars()
            .all()
        )
        assert logs and all(marker not in str(vars(log)) for log in logs)
    assert len(caller.calls) == 1
