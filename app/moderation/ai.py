"""Remote AI moderation contracts and safe soft-evidence merge.

T-204: AI is optional, provider-neutral, disabled by default, and never directly
executes moderation actions. A single model result is soft evidence only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.qq_official.contract import StandardMessage
from app.config import Settings, get_settings
from app.db import Base
from app.moderation.decision import Category, ModerationDecision, RuleHit

AIContentKind = Literal[
    "text",
    "ocr",
    "asr",
    "file_text",
    "share_card",
    "image",
    "gif",
    "video_frame",
]
AIResultSource = Literal["text", "vision", "degraded", "cache"]

PROMPT_VERSION = "t204-v1"
MAX_AI_TEXT_CHARS = 4_000
MAX_AI_MEDIA_BYTES = 5 * 1024 * 1024

_CONTACT_PATTERNS = (
    re.compile(r"1[3-9]\d{9}"),
    re.compile(r"(?:微信|vx|v信|qq)[:：\s]*[A-Za-z0-9_-]{5,}", re.IGNORECASE),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
)
_FORBIDDEN_RESPONSE_FIELDS = {
    "action",
    "actions",
    "kick",
    "ban",
    "recall",
    "mute",
    "warn",
    "sql",
    "command",
    "shell",
    "tool",
    "http",
    "url",
    "delete",
    "execute",
}
_ALLOWED_CATEGORIES = {"ad", "fraud", "porn", "violence", "flood", "other", None}


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AIProviderError(RuntimeError):
    """Raised when a provider call fails or returns an invalid contract."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AIModerationRequest(BaseModel):
    """Minimal provider-neutral AI request.

    `media_bytes` is excluded from repr/dumps to avoid accidental logging.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    message_id: str
    group_openid: str
    content_kind: AIContentKind
    text: str = ""
    media_bytes: bytes | None = Field(default=None, repr=False, exclude=True)
    media_mime: str = ""
    media_digest: str = ""
    rule_version_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _fill_media_digest(self) -> AIModerationRequest:
        if self.media_bytes and not self.media_digest:
            self.media_digest = hashlib.sha256(self.media_bytes).hexdigest()
        return self

    def sanitized_text(self) -> str:
        return sanitize_text(self.text)


class AIModerationResult(BaseModel):
    """Strict JSON result accepted from AI providers."""

    category: Category = None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = Field(default="", max_length=500)
    model_id: str = Field(min_length=1, max_length=128)
    prompt_version: str = Field(default=PROMPT_VERSION, min_length=1, max_length=64)
    provider: str = Field(default="unknown", max_length=64)
    source: AIResultSource = "text"
    needs_review: bool = True
    latency_ms: int = Field(default=0, ge=0)
    cost_cents: int = Field(default=0, ge=0)
    degraded_reason: str = Field(default="", max_length=200)
    raw_response_sha256: str = Field(default="", max_length=64)

    @field_validator("category")
    @classmethod
    def _valid_category(cls, value: Category) -> Category:
        if value not in _ALLOWED_CATEGORIES:
            raise ValueError("AI类别不在项目允许范围内")
        return value

    @field_validator("model_id", "prompt_version", "provider")
    @classmethod
    def _no_sensitive_markers(cls, value: str) -> str:
        if any(marker in value.lower() for marker in ("key=", "token=", "bearer ")):
            raise ValueError("AI结果元数据不得包含密钥")
        return value


class TextModerator(Protocol):
    """Provider-neutral text moderation interface."""

    model_id: str
    prompt_version: str

    async def moderate_text(self, request: AIModerationRequest) -> AIModerationResult:
        """Return a strict AI moderation result."""


class VisionModerator(Protocol):
    """Provider-neutral vision moderation interface."""

    model_id: str
    prompt_version: str

    async def moderate_image(self, request: AIModerationRequest) -> AIModerationResult:
        """Return a strict AI moderation result."""


class AICacheEntry(Base):
    """Versioned AI moderation cache."""

    __tablename__ = "ai_cache"

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(128), index=True)
    prompt_version: Mapped[str] = mapped_column(String(64), index=True)
    result_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AIUsageLog(Base):
    """Trace provider calls and costs without storing secrets or raw payloads."""

    __tablename__ = "ai_usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(64), default="")
    model_id: Mapped[str] = mapped_column(String(128), default="")
    group_openid: Mapped[str] = mapped_column(String(128), index=True)
    message_id: Mapped[str] = mapped_column(String(128), index=True)
    source: Mapped[str] = mapped_column(String(16), default="")
    cache_key: Mapped[str] = mapped_column(String(64), default="", index=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error_kind: Mapped[str] = mapped_column(String(64), default="")
    cost_cents: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


def sanitize_text(text: str) -> str:
    """Mask common contact identifiers and cap payload length before cloud upload."""
    masked = text
    for pattern in _CONTACT_PATTERNS:
        masked = pattern.sub("[CONTACT_MASKED]", masked)
    return masked[:MAX_AI_TEXT_CHARS]


def parse_enabled_groups(raw: str) -> set[str]:
    """Parse comma-separated group OpenIDs. `*` explicitly enables all groups."""
    return {part.strip() for part in raw.split(",") if part.strip()}


def ai_enabled_for_group(settings: Settings, group_openid: str) -> bool:
    """Remote AI must be globally enabled and explicitly enabled for this group."""
    groups = parse_enabled_groups(settings.ai_enabled_groups)
    return settings.ai_enabled and ("*" in groups or group_openid in groups)


def provider_payload_to_result(
    payload: Any,
    *,
    model_id: str,
    prompt_version: str,
    provider: str,
    source: AIResultSource,
    latency_ms: int = 0,
) -> AIModerationResult:
    """Validate provider JSON and convert it to the internal strict result."""
    if not isinstance(payload, dict):
        raise AIProviderError("provider_non_object_json")
    _reject_forbidden_response_fields(payload)
    raw_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    category_raw = payload.get("category")
    category: Category = None
    _VALID_CATEGORIES = {"ad", "fraud", "porn", "violence", "flood", "other"}
    # 词表外的良性表达（safe/benign/clean 等）或未知值一律视为无违规（None），
    # 避免模型对正常消息返回词表外类别时触发契约校验错误而整体降级。
    if category_raw not in (None, "", "none", "normal", "allow", "safe", "benign", "clean"):
        lowered = str(category_raw).lower()
        category = cast(Category, lowered) if lowered in _VALID_CATEGORIES else None
    confidence_raw = payload.get("confidence", 0.0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError) as exc:
        raise AIProviderError("provider_invalid_confidence") from exc
    evidence = str(payload.get("evidence") or payload.get("reason") or "")[:500]
    needs_review = bool(payload.get("needs_review", True))
    try:
        return AIModerationResult(
            category=category,
            confidence=confidence,
            evidence=evidence,
            model_id=model_id,
            prompt_version=prompt_version,
            provider=provider,
            source=source,
            needs_review=needs_review,
            latency_ms=latency_ms,
            cost_cents=int(payload.get("cost_cents") or 0),
            raw_response_sha256=raw_hash,
        )
    except ValueError as exc:
        raise AIProviderError("provider_contract_invalid") from exc


def degraded_ai_result(
    reason: str,
    *,
    model_id: str = "unconfigured",
    prompt_version: str = PROMPT_VERSION,
    provider: str = "none",
    source: AIResultSource = "degraded",
) -> AIModerationResult:
    """Return a typed degradation result that keeps the moderation chain alive."""
    return AIModerationResult(
        category=None,
        confidence=0.0,
        evidence="AI辅助审核降级",
        model_id=model_id or "unconfigured",
        prompt_version=prompt_version,
        provider=provider,
        source=source,
        needs_review=True,
        degraded_reason=reason,
    )


def ai_cache_key(
    request: AIModerationRequest,
    *,
    model_id: str,
    prompt_version: str,
) -> str:
    """Cache key includes sanitized content, media digest, model, prompt, and rules."""
    payload = {
        "text_sha256": hashlib.sha256(request.sanitized_text().encode("utf-8")).hexdigest(),
        "media_digest": request.media_digest,
        "content_kind": request.content_kind,
        "model_id": model_id,
        "prompt_version": prompt_version,
        "rule_version_ids": request.rule_version_ids,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


async def get_cached_ai_result(
    session: AsyncSession,
    cache_key: str,
    *,
    now: datetime | None = None,
) -> AIModerationResult | None:
    now = now or _utcnow()
    row = await session.get(AICacheEntry, cache_key)
    if row is None or _as_aware(row.expires_at) <= now:
        return None
    data = json.loads(row.result_json)
    result = AIModerationResult.model_validate(data)
    return result.model_copy(update={"source": "cache"})


async def store_ai_result(
    session: AsyncSession,
    cache_key: str,
    result: AIModerationResult,
    *,
    ttl_seconds: int = 86_400,
    now: datetime | None = None,
) -> None:
    now = now or _utcnow()
    row = await session.get(AICacheEntry, cache_key)
    values = {
        "model_id": result.model_id,
        "prompt_version": result.prompt_version,
        "result_json": result.model_dump_json(),
        "created_at": now,
        "expires_at": now + timedelta(seconds=ttl_seconds),
    }
    if row is None:
        row = AICacheEntry(cache_key=cache_key, **values)
        session.add(row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
    await session.commit()


async def record_ai_usage(
    session: AsyncSession,
    request: AIModerationRequest,
    result: AIModerationResult,
    *,
    cache_key: str,
    ok: bool,
    error_kind: str = "",
) -> None:
    session.add(
        AIUsageLog(
            provider=result.provider,
            model_id=result.model_id,
            group_openid=request.group_openid,
            message_id=request.message_id,
            source=result.source,
            cache_key=cache_key,
            ok=ok,
            error_kind=error_kind,
            cost_cents=result.cost_cents,
            latency_ms=result.latency_ms,
        )
    )
    await session.commit()


@dataclass
class AIQuota:
    """Small in-process rate/budget limiter for optional remote AI calls."""

    daily_budget_cents: int = 0
    per_minute_limit: int = 30
    _day: str = field(default_factory=lambda: _utcnow().date().isoformat())
    _spent_cents: int = 0
    _minute_calls: list[float] = field(default_factory=list)

    def allow(self, estimated_cost_cents: int = 0, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        self._reset_day_if_needed()
        self._minute_calls = [ts for ts in self._minute_calls if now - ts < 60]
        if len(self._minute_calls) >= self.per_minute_limit:
            return False
        if (
            self.daily_budget_cents
            and self._spent_cents + estimated_cost_cents > self.daily_budget_cents
        ):
            return False
        self._minute_calls.append(now)
        return True

    def record(self, cost_cents: int) -> None:
        self._reset_day_if_needed()
        self._spent_cents += max(cost_cents, 0)

    def _reset_day_if_needed(self) -> None:
        today = _utcnow().date().isoformat()
        if today != self._day:
            self._day = today
            self._spent_cents = 0
            self._minute_calls.clear()


@dataclass
class AIReviewService:
    """Runs optional AI moderators and merges their results as soft evidence."""

    enabled: bool
    enabled_groups: set[str]
    text_moderator: TextModerator | None = None
    vision_moderator: VisionModerator | None = None
    quota: AIQuota = field(default_factory=AIQuota)
    config_problem: str = ""
    cache_ttl_seconds: int = 86_400

    def enabled_for_group(self, group_openid: str) -> bool:
        return self.enabled and ("*" in self.enabled_groups or group_openid in self.enabled_groups)

    async def review_message(
        self,
        session: AsyncSession,
        msg: StandardMessage,
        decision: ModerationDecision,
        *,
        media_paths: list[Path] | None = None,
        rule_version_ids: tuple[int, ...] = (),
    ) -> tuple[ModerationDecision, list[AIModerationResult]]:
        """Run AI only for explicitly enabled groups and non-final local decisions."""
        if not self.enabled_for_group(msg.group_openid):
            return decision, []
        if decision.verdict == "violation_high":
            return decision, []

        results: list[AIModerationResult] = []
        text = _ai_text_from_message(msg)
        if self.config_problem:
            results.append(degraded_ai_result(self.config_problem))
            return merge_ai_evidence(decision, results), results
        if text and self.text_moderator is not None:
            request = AIModerationRequest(
                message_id=msg.message_id,
                group_openid=msg.group_openid,
                content_kind="text",
                text=text,
                rule_version_ids=list(rule_version_ids),
            )
            results.append(await self._call_text(session, request))
        for path in media_paths or []:
            if self.vision_moderator is None:
                continue
            results.append(await self._call_vision_path(session, msg, path, rule_version_ids))

        return merge_ai_evidence(decision, results), results

    async def _call_text(
        self, session: AsyncSession, request: AIModerationRequest
    ) -> AIModerationResult:
        assert self.text_moderator is not None
        return await self._call_with_cache(session, request, self.text_moderator, "text")

    async def _call_vision_path(
        self,
        session: AsyncSession,
        msg: StandardMessage,
        path: Path,
        rule_version_ids: tuple[int, ...],
    ) -> AIModerationResult:
        assert self.vision_moderator is not None
        try:
            media_size = await asyncio.to_thread(lambda: path.stat().st_size)
            if media_size > MAX_AI_MEDIA_BYTES:
                return degraded_ai_result(
                    "media_too_large_for_ai",
                    model_id=self.vision_moderator.model_id,
                    prompt_version=self.vision_moderator.prompt_version,
                    provider="openai-compatible",
                )
            media_bytes = await asyncio.to_thread(path.read_bytes)
        except OSError:
            return degraded_ai_result(
                "media_unreadable_for_ai",
                model_id=self.vision_moderator.model_id,
                prompt_version=self.vision_moderator.prompt_version,
                provider="openai-compatible",
            )
        request = AIModerationRequest(
            message_id=msg.message_id,
            group_openid=msg.group_openid,
            content_kind="gif" if path.suffix.lower() == ".gif" else "image",
            text=_ai_text_from_message(msg),
            media_bytes=media_bytes,
            media_mime=_mime_from_path(path),
            rule_version_ids=list(rule_version_ids),
        )
        return await self._call_with_cache(session, request, self.vision_moderator, "vision")

    async def _call_with_cache(
        self,
        session: AsyncSession,
        request: AIModerationRequest,
        moderator: TextModerator | VisionModerator,
        source: Literal["text", "vision"],
    ) -> AIModerationResult:
        cache_key = ai_cache_key(
            request, model_id=moderator.model_id, prompt_version=moderator.prompt_version
        )
        cached = await get_cached_ai_result(session, cache_key)
        if cached is not None:
            await record_ai_usage(session, request, cached, cache_key=cache_key, ok=True)
            return cached
        if not self.quota.allow():
            result = degraded_ai_result(
                "ai_rate_or_budget_limited",
                model_id=moderator.model_id,
                prompt_version=moderator.prompt_version,
            )
            await record_ai_usage(
                session,
                request,
                result,
                cache_key=cache_key,
                ok=False,
                error_kind=result.degraded_reason,
            )
            return result
        try:
            if source == "text":
                result = await cast(TextModerator, moderator).moderate_text(request)
            else:
                result = await cast(VisionModerator, moderator).moderate_image(request)
        except AIProviderError as exc:
            result = degraded_ai_result(
                exc.reason,
                model_id=moderator.model_id,
                prompt_version=moderator.prompt_version,
                provider="openai-compatible",
            )
            await record_ai_usage(
                session, request, result, cache_key=cache_key, ok=False, error_kind=exc.reason
            )
            return result
        self.quota.record(result.cost_cents)
        await store_ai_result(session, cache_key, result, ttl_seconds=self.cache_ttl_seconds)
        await record_ai_usage(session, request, result, cache_key=cache_key, ok=True)
        return result


def merge_ai_evidence(
    local: ModerationDecision,
    ai_results: list[AIModerationResult],
    *,
    independent_confirmed: bool = False,
) -> ModerationDecision:
    """Merge AI outputs. A single AI model can only move `allow` to `record_only`."""
    usable = [result for result in ai_results if not result.degraded_reason and result.category]
    if not usable:
        return local

    hits = [
        RuleHit(
            rule_id=f"AI_{result.source.upper()}",
            rule_name=f"ai_{result.provider}",
            category=result.category,
            confidence_delta=min(result.confidence, 0.35),
            evidence_masked=result.evidence[:120],
        )
        for result in usable
    ]
    high_ai = [result for result in usable if result.confidence >= 0.90]
    if local.verdict == "violation_high":
        return local.model_copy(update={"rule_hits": local.rule_hits + hits})
    if (
        independent_confirmed
        and len({r.model_id for r in high_ai}) >= 2
        and not local.is_protected_sender
    ):
        category = high_ai[0].category
        return local.model_copy(
            update={
                "verdict": "violation_high",
                "category": category,
                "confidence": max(local.confidence, min(max(r.confidence for r in high_ai), 0.95)),
                "rule_hits": local.rule_hits + hits,
                "recommended_actions": ["recall", "mute", "warn"],
                "reason": "AI主模型与独立复核模型均高置信，进入高置信违规",
            }
        )
    if high_ai or local.verdict == "allow":
        return local.model_copy(
            update={
                "verdict": "record_only",
                "category": local.category or usable[0].category,
                "confidence": max(local.confidence, min(max(r.confidence for r in usable), 0.85)),
                "rule_hits": local.rule_hits + hits,
                "recommended_actions": [],
                "reason": (local.reason + "；" if local.reason else "")
                + "AI辅助命中软证据，转人工复核",
            }
        )
    return local.model_copy(update={"rule_hits": local.rule_hits + hits})


@lru_cache
def build_default_ai_review_service() -> AIReviewService:
    """Build the default service from environment settings.

    This factory intentionally returns a safe disabled/no-op service unless both
    global and per-group switches are configured at runtime.
    """
    settings = get_settings()
    enabled_groups = parse_enabled_groups(settings.ai_enabled_groups)
    if not settings.ai_enabled:
        return AIReviewService(enabled=False, enabled_groups=enabled_groups)
    missing = []
    if not settings.ai_api_key:
        missing.append("AI_API_KEY")
    if not settings.ai_base_url:
        missing.append("AI_BASE_URL")
    text_moderator: TextModerator | None = None
    vision_moderator: VisionModerator | None = None
    if missing:
        return AIReviewService(
            enabled=True,
            enabled_groups=enabled_groups,
            config_problem="missing_" + "_".join(missing).lower(),
        )

    from app.adapters.ai.openai_compatible import (
        OpenAICompatibleTextModerator,
        OpenAICompatibleVisionModerator,
    )

    if settings.ai_text_model:
        text_moderator = OpenAICompatibleTextModerator(
            base_url=settings.ai_base_url,
            api_key=settings.ai_api_key,
            model_id=settings.ai_text_model,
            timeout_seconds=settings.ai_timeout_seconds,
            prompt_version=settings.ai_prompt_version,
        )
    if settings.ai_vision_model:
        vision_moderator = OpenAICompatibleVisionModerator(
            base_url=settings.ai_base_url,
            api_key=settings.ai_api_key,
            model_id=settings.ai_vision_model,
            timeout_seconds=settings.ai_timeout_seconds,
            prompt_version=settings.ai_prompt_version,
        )
    if text_moderator is None and vision_moderator is None:
        return AIReviewService(
            enabled=True,
            enabled_groups=enabled_groups,
            config_problem="missing_ai_models",
        )
    return AIReviewService(
        enabled=True,
        enabled_groups=enabled_groups,
        text_moderator=text_moderator,
        vision_moderator=vision_moderator,
        quota=AIQuota(
            daily_budget_cents=settings.ai_daily_budget_cents,
            per_minute_limit=settings.ai_per_minute_limit,
        ),
    )


def _reject_forbidden_response_fields(payload: Any) -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in _FORBIDDEN_RESPONSE_FIELDS:
                raise AIProviderError("provider_forbidden_action_field")
            _reject_forbidden_response_fields(value)
    elif isinstance(payload, list):
        for value in payload:
            _reject_forbidden_response_fields(value)


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _ai_text_from_message(msg: StandardMessage) -> str:
    parts = [msg.text]
    if msg.share_card is not None:
        parts.extend(
            [
                msg.share_card.source,
                msg.share_card.title,
                msg.share_card.prompt,
                msg.share_card.tag,
            ]
        )
    for attachment in msg.attachments:
        if attachment.asr_refer_text:
            parts.append(attachment.asr_refer_text)
    return "\n".join(part for part in parts if part)


def _mime_from_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".gif":
        return "image/gif"
    return "application/octet-stream"
