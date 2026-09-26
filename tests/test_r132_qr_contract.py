# ruff: noqa: E402, I001, F401
# Reviewer probe pack (PR #45 / r132), promoted verbatim into the repo test suite.
# Isolation asserts intentionally run BEFORE application imports (E402 is by design).
# This header changes no assertion and no logic.

import pytest
from app.moderation.ai import (
    AIProviderError,
    AIModerationRequest,
    ai_cache_key,
    provider_payload_to_result,
)
from app.moderation import ai
from app.adapters.ai.openai_compatible import OpenAICompatibleTextModerator


@pytest.mark.parametrize("bad", [None, 0, 1, "true", "false", [], {}])
def test_qr_flag_rejects_non_boolean_values(bad):
    with pytest.raises(AIProviderError, match="provider_invalid_has_miniprogram_code"):
        provider_payload_to_result(
            {"category": None, "confidence": 1, "needs_review": False, "has_miniprogram_code": bad},
            model_id="synthetic",
            prompt_version="synthetic",
            provider="synthetic",
            source="vision",
        )


@pytest.mark.parametrize("source", ["text", "cache", "degraded"])
def test_other_sources_cannot_grant_qr_allow(source):
    result = provider_payload_to_result(
        {"category": None, "confidence": 1, "needs_review": False, "has_miniprogram_code": True},
        model_id="synthetic",
        prompt_version="synthetic",
        provider="synthetic",
        source=source,
    )
    assert result.has_miniprogram_code is False


def test_builtin_prompt_revision_invalidates_fixed_env_version(monkeypatch):
    request = AIModerationRequest(message_id="m", group_openid="g", content_kind="image")
    old = ai_cache_key(request, model_id="same", prompt_version="same-env")
    monkeypatch.setattr(ai, "PROMPT_VERSION", "synthetic-next")
    assert old != ai_cache_key(request, model_id="same", prompt_version="same-env")


def test_effective_prompt_digest_changes_even_with_fixed_version():
    class NoNetwork:
        pass

    first = OpenAICompatibleTextModerator(
        base_url="https://synthetic.invalid",
        api_key="synthetic",
        model_id="same",
        prompt_version="same",
        client=NoNetwork(),
        extra_system_rules="old",
    )
    second = OpenAICompatibleTextModerator(
        base_url="https://synthetic.invalid",
        api_key="synthetic",
        model_id="same",
        prompt_version="same",
        client=NoNetwork(),
        extra_system_rules="new",
    )
    assert first.prompt_digest != second.prompt_digest
