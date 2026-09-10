"""安全配置的拒绝路径；全部值均为测试占位，不使用真实凭据。"""

from __future__ import annotations

import pytest
from app.config import Settings


@pytest.mark.parametrize("value", ["", "0", "-1", "abc", "１２３", "12 34"])
def test_real_onebot_actions_require_explicit_numeric_account(value: str) -> None:
    with pytest.raises(ValueError, match="ONEBOT_SELF_ID"):
        Settings(ONEBOT_ACTIONS_ENABLED=True, ONEBOT_SELF_ID=value, _env_file=None)


def test_shadow_remains_usable_without_account_binding() -> None:
    settings = Settings(ONEBOT_ACTIONS_ENABLED=False, ONEBOT_SELF_ID="", _env_file=None)
    assert settings.onebot_self_id == ""


@pytest.mark.parametrize("low,direct", [(0.9, 0.9), (0.95, 0.9)])
def test_invalid_ai_threshold_order_is_rejected(low: float, direct: float) -> None:
    with pytest.raises(ValueError, match="AI_SECONDARY_REVIEW_LOW"):
        Settings(AI_SECONDARY_REVIEW_LOW=low, AI_PRIMARY_DIRECT_THRESHOLD=direct, _env_file=None)


def test_same_model_cannot_impersonate_independent_reviewer() -> None:
    with pytest.raises(ValueError, match="AI_REVIEW_MODEL"):
        Settings(AI_VISION_MODEL="test-vision", AI_REVIEW_MODEL="test-vision", _env_file=None)


def test_agent_tokens_must_be_distinct() -> None:
    with pytest.raises(ValueError, match="AGENT_API_READ_TOKEN"):
        Settings(AGENT_API_TOKEN="test-only", AGENT_API_READ_TOKEN="test-only", _env_file=None)


def test_agent_cannot_reuse_human_admin_credential() -> None:
    with pytest.raises(ValueError, match="ADMIN_PASSWORD"):
        Settings(ADMIN_PASSWORD="test-only", AGENT_API_TOKEN="test-only", _env_file=None)


def test_unknown_scope_is_rejected_at_configuration_boundary() -> None:
    with pytest.raises(ValueError, match="AGENT_API_WRITE_SCOPES"):
        Settings(AGENT_API_WRITE_SCOPES="project:read,invented:superuser", _env_file=None)


def test_safe_agent_defaults_and_call_limit() -> None:
    settings = Settings(_env_file=None)
    assert settings.agent_api_write_scopes == "project:read"
    assert settings.ai_daily_call_limit == 1000
    assert settings.ai_prompt_version == "t204-v4"
    assert settings.admin_session_ttl_seconds == 3600


@pytest.mark.parametrize(
    "name,value", [("AI_DAILY_CALL_LIMIT", 0), ("ADMIN_SESSION_TTL_SECONDS", 0)]
)
def test_limits_cannot_be_disabled_by_zero(name: str, value: int) -> None:
    with pytest.raises(ValueError):
        Settings(**{name: value}, _env_file=None)


def test_onebot_production_does_not_require_unrelated_official_credentials() -> None:
    settings = Settings(
        APP_ENV="prod",
        ADMIN_PASSWORD="test-only-strong-password",
        ACTION_MODE="OFFICIAL",
        ONEBOT_WS_ENABLED=True,
        ONEBOT_ACTIONS_ENABLED=True,
        ONEBOT_ACCESS_TOKEN="test-token",
        ONEBOT_SELF_ID="10000001",
        QQ_APP_ID="",
        QQ_APP_SECRET="",
        _env_file=None,
    )
    assert settings.onebot_actions_enabled and not settings.qq_app_id


def test_official_only_production_still_requires_its_credentials() -> None:
    with pytest.raises(ValueError, match="QQ_APP_ID"):
        Settings(
            APP_ENV="prod",
            ADMIN_PASSWORD="test-only-strong-password",
            ACTION_MODE="OFFICIAL",
            ONEBOT_ACTIONS_ENABLED=False,
            QQ_APP_ID="",
            QQ_APP_SECRET="",
            _env_file=None,
        )
