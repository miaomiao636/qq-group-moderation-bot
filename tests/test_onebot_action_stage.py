"""W5 staged rollout: recall-only is the first live OneBot stage, never full by default."""

import uuid
from datetime import UTC, datetime

import pytest
from app.actions.orchestrator import orchestrate_actions
from app.config import Settings
from app.db import SessionLocal

from tests.test_onebot_actions import (
    _ConfirmedOneBotClient,
    _high_decision,
    _ob_msg,
    _official_onebot_settings,
    _setup_onebot_group,
)


def test_onebot_stage_defaults_to_recall_only_and_actions_remain_disabled(monkeypatch):
    monkeypatch.delenv("ONEBOT_ACTION_STAGE", raising=False)
    monkeypatch.delenv("ONEBOT_ACTIONS_ENABLED", raising=False)
    settings = Settings(_env_file=None)
    assert settings.onebot_action_stage == "recall_only"
    assert settings.onebot_actions_enabled is False


@pytest.mark.parametrize("value", ["", "all", "mute", "FULL"])
def test_invalid_onebot_stage_fails_configuration(value):
    with pytest.raises(ValueError, match="ONEBOT_ACTION_STAGE"):
        Settings(ONEBOT_ACTION_STAGE=value, _env_file=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["recall_only", "full"])
async def test_stage_controls_two_strikes_without_replaying_old_actions(stage):
    client = _ConfirmedOneBotClient()
    group = str(uuid.uuid4().int)[:10]
    settings = _official_onebot_settings().model_copy(update={"onebot_action_stage": stage})
    messages = [_ob_msg(group, "1001") for _ in range(2)]
    for msg in messages:
        msg.external_self_id = "10000001"
        msg.sent_at = datetime.now(UTC)
    intents = []
    async with SessionLocal() as session:
        await _setup_onebot_group(session, group)
        for msg in messages:
            intents.extend(
                await orchestrate_actions(
                    session, msg, _high_decision(msg), onebot_client=client, settings=settings
                )
            )
        for msg in messages:
            await orchestrate_actions(
                session, msg, _high_decision(msg), onebot_client=client, settings=settings
            )
    expected = (
        ["recall", "recall"]
        if stage == "recall_only"
        else ["recall", "mute", "warn", "recall", "mute"]
    )
    assert [name for name, _ in client.calls] == expected
    assert [intent.action for intent in intents] == expected
    assert all(intent.status == "SUCCEEDED" for intent in intents)
    if stage == "full":
        assert [args[2] for name, args in client.calls if name == "mute"] == [3600, 86400]
