"""OpenAI-compatible moderation adapter.

The adapter is intentionally thin: it sends sanitized text or explicitly
selected image bytes to `/chat/completions` and accepts only strict JSON.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import httpx

from app.moderation.ai import (
    PROMPT_VERSION,
    AIModerationRequest,
    AIModerationResult,
    AIProviderError,
    provider_payload_to_result,
)

SYSTEM_PROMPT = (
    "你是QQ群内容安全审核器。只返回JSON对象，不要返回解释。"
    "字段必须是 category、confidence、evidence、needs_review。"
    "category只能是 ad/fraud/porn/violence/flood/other/null。"
    "广告/引流=兼职招聘、刷单、代发、加微信/QQ引流、外部群邀请、"
    "带联系方式的推广图。诈骗=钓鱼、虚假中奖、仿冒客服。"
    "human_feedback字段含人工纠正记录，请参考历史判定调整你的判断。"
    "不要输出任何动作、命令、SQL、工具调用或处罚建议。"
)


class OpenAICompatibleTextModerator:
    """Text moderator for MiMo or any OpenAI-compatible chat-completions API."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_id: str,
        prompt_version: str = PROMPT_VERSION,
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
        provider: str = "openai-compatible",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_id = model_id
        self.prompt_version = prompt_version
        self.provider = provider
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None

    async def moderate_text(self, request: AIModerationRequest) -> AIModerationResult:
        started = time.monotonic()
        payload = {
            "model": self.model_id,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "content_kind": request.content_kind,
                            "text": request.sanitized_text(),
                            "rule_version_ids": request.rule_version_ids,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        data = await self._post(payload)
        model_payload = _extract_json_payload(data)
        latency_ms = int((time.monotonic() - started) * 1000)
        return provider_payload_to_result(
            model_payload,
            model_id=self.model_id,
            prompt_version=self.prompt_version,
            provider=self.provider,
            source="text",
            latency_ms=latency_ms,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.base_url or not self.api_key or not self.model_id:
            raise AIProviderError("provider_missing_config")
        try:
            resp = await self._client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.TimeoutException as exc:
            raise AIProviderError("provider_timeout") from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (429, 529):
                raise AIProviderError("provider_rate_limited") from exc
            raise AIProviderError("provider_http_error") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise AIProviderError("provider_transport_or_json_error") from exc
        if not isinstance(data, dict):
            raise AIProviderError("provider_non_object_json")
        return data


class OpenAICompatibleVisionModerator(OpenAICompatibleTextModerator):
    """Vision moderator for OpenAI-compatible multimodal chat APIs."""

    async def moderate_image(self, request: AIModerationRequest) -> AIModerationResult:
        if not request.media_bytes:
            raise AIProviderError("provider_missing_media")
        started = time.monotonic()
        encoded = base64.b64encode(request.media_bytes).decode("ascii")
        mime = request.media_mime or "application/octet-stream"
        payload = {
            "model": self.model_id,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "content_kind": request.content_kind,
                                    "text": request.sanitized_text(),
                                    "media_digest": request.media_digest,
                                    "rule_version_ids": request.rule_version_ids,
                                    "human_feedback": request.feedback_context or None,
                                },
                                ensure_ascii=False,
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{encoded}"},
                        },
                    ],
                },
            ],
        }
        data = await self._post(payload)
        model_payload = _extract_json_payload(data)
        latency_ms = int((time.monotonic() - started) * 1000)
        return provider_payload_to_result(
            model_payload,
            model_id=self.model_id,
            prompt_version=self.prompt_version,
            provider=self.provider,
            source="vision",
            latency_ms=latency_ms,
        )


def _extract_json_payload(data: dict[str, Any]) -> dict[str, Any]:
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIProviderError("provider_missing_choice_content") from exc
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        raise AIProviderError("provider_invalid_choice_content")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise AIProviderError("provider_invalid_model_json") from exc
    if not isinstance(parsed, dict):
        raise AIProviderError("provider_invalid_model_json")
    return parsed
