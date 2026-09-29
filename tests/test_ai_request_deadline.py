"""STABILITY-20260921: provider trickle responses cannot occupy workers forever."""

import asyncio

import httpx
import pytest
from app.adapters.ai.openai_compatible import OpenAICompatibleVisionModerator
from app.moderation.ai import AIModerationRequest, AIProviderError


class DripStream(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        while True:
            yield b" "
            await asyncio.sleep(0.02)

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("method", ["moderate_text", "moderate_image"])
async def test_whole_request_deadline_closes_a_trickling_response(method):
    stream = DripStream()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
    ) as client:
        moderator = OpenAICompatibleVisionModerator(
            base_url="https://synthetic.invalid",
            api_key="synthetic-key",
            model_id="synthetic-model",
            timeout_seconds=0.2,
            client=client,
        )
        request = AIModerationRequest(
            message_id="synthetic",
            group_openid="synthetic",
            content_kind="image" if method == "moderate_image" else "text",
            text="synthetic",
            media_bytes=b"synthetic-image",
        )
        with pytest.raises(AIProviderError, match="provider_timeout"):
            await asyncio.wait_for(getattr(moderator, method)(request), timeout=1.0)
        assert stream.closed
        assert not client.is_closed  # The injected client remains caller-owned.


async def test_successful_response_keeps_existing_result_contract():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": '{"category":null,"confidence":0.2,'
                                '"evidence":"synthetic","needs_review":true}'
                            }
                        }
                    ]
                },
            )
        )
    ) as client:
        moderator = OpenAICompatibleVisionModerator(
            base_url="https://synthetic.invalid",
            api_key="synthetic-key",
            model_id="synthetic-model",
            timeout_seconds=0.2,
            client=client,
        )
        result = await moderator.moderate_text(
            AIModerationRequest(
                message_id="synthetic", group_openid="synthetic", content_kind="text"
            )
        )
        assert result.category is None and result.needs_review is True
        assert result.confidence == 0.2 and result.evidence == "synthetic"
        assert not client.is_closed
