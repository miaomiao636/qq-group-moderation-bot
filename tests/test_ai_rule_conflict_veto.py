"""Independent models cannot silently override a human-published policy conflict."""

import pytest

from tests.test_ai_conditional_review import FakeVision, review


@pytest.mark.parametrize(
    "confidence", [True, False, "0.99", "true", None, float("nan"), float("inf"), -0.1, 1.1]
)
def test_provider_confidence_must_be_finite_numeric_probability(confidence) -> None:
    from app.moderation.ai import AIProviderError, provider_payload_to_result

    with pytest.raises(AIProviderError):
        provider_payload_to_result(
            {"category": "ad", "confidence": confidence, "needs_review": False},
            model_id="test-primary",
            prompt_version="test-v1",
            provider="test",
            source="vision",
        )


@pytest.mark.asyncio
async def test_two_models_agreeing_cannot_override_explicit_rule_conflict(tmp_path) -> None:
    primary = FakeVision("primary", "ad", 0.99)
    secondary = FakeVision("review", "ad", 0.99)
    (decision, results), _ = await review(tmp_path, primary, secondary, conflict=True)
    assert secondary.calls == 1
    assert len(results) == 2
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []
