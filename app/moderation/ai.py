"""Provider-neutral AI evidence and deterministic conditional-review policy."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Boolean, DateTime, Integer, String, Text, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.config import Settings
from app.core.contracts import StandardMessage
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
AIReviewRole = Literal["auxiliary", "primary", "secondary"]

PROMPT_VERSION = "t204-v4"
AI_POLICY_VERSION = "conditional-review-v1"
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
    # T-305：消息来源通道的群中立标识；为空时用量记录回退到 group_openid。
    external_group_id: str = ""
    message_provider: str = "qq_official"
    content_kind: AIContentKind
    text: str = ""
    media_bytes: bytes | None = Field(default=None, repr=False, exclude=True)
    media_mime: str = ""
    media_digest: str = ""
    rule_version_ids: list[int] = Field(default_factory=list)
    # T-205增强：人工确认反馈的纠正上下文，让模型从人工判定中学习
    feedback_context: str = ""
    review_role: AIReviewRole = "auxiliary"
    policy_context: str = ""

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
    # Local orchestration metadata, never accepted from the provider JSON.
    review_role: AIReviewRole = "primary"
    review_group: str = ""
    review_reason: str = ""
    policy_version: str = AI_POLICY_VERSION
    cache_hit: bool = False
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_known: bool = False

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
    group_openid: Mapped[str] = mapped_column(String(128), index=True)  # 旧镜像
    # T-305：消息来源通道的群中立标识（provider 列此处含义是AI供应商，故不复用）
    external_group_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    # 保留既有迁移列；不因使用量统计不需要成员信息而删除历史字段。
    external_user_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
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
    unknown_category = False
    # Normalize only explicit normal aliases. Unknown labels require review.
    if category_raw not in (None, "", "none", "normal", "allow", "safe", "benign", "clean"):
        lowered = str(category_raw).lower()
        unknown_category = lowered not in _VALID_CATEGORIES
        category = cast(Category, lowered) if not unknown_category else "other"
    confidence_raw = payload.get("confidence", 0.0)
    if type(confidence_raw) not in (int, float):
        raise AIProviderError("provider_invalid_confidence")
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError) as exc:
        raise AIProviderError("provider_invalid_confidence") from exc
    evidence = str(payload.get("evidence") or payload.get("reason") or "")[:500]
    needs_review_raw = payload.get("needs_review", True)
    if not isinstance(needs_review_raw, bool):
        raise AIProviderError("provider_invalid_needs_review")
    needs_review = needs_review_raw or unknown_category
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
        "message_provider": request.message_provider,
        "group_id": request.external_group_id or request.group_openid,
        "review_role": request.review_role,
        "policy_context": request.policy_context,
        "policy_version": AI_POLICY_VERSION,
        # Changes to the shipped prompt invalidate old caches even if an old
        # deployment still explicitly supplies AI_PROMPT_VERSION in its env.
        "builtin_prompt_revision": PROMPT_VERSION,
        "text_sha256": hashlib.sha256(request.sanitized_text().encode("utf-8")).hexdigest(),
        "media_digest": request.media_digest,
        "content_kind": request.content_kind,
        "model_id": model_id,
        "prompt_version": prompt_version,
        "rule_version_ids": request.rule_version_ids,
        "feedback_sha256": hashlib.sha256(request.feedback_context.encode("utf-8")).hexdigest()
        if request.feedback_context
        else "",
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
    return result.model_copy(update={"cache_hit": True, "cost_cents": 0})


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
            external_group_id=request.external_group_id or request.group_openid,
            message_id=request.message_id,
            source=(
                "cache"
                if result.cache_hit
                else ("error" if result.source == "degraded" else result.source)
            )
            + (f"_{result.review_role}" if result.review_role != "auxiliary" else ""),
            cache_key=cache_key,
            ok=ok,
            error_kind=error_kind,
            cost_cents=result.cost_cents,
            latency_ms=result.latency_ms,
        )
    )
    await session.commit()


async def summarize_ai_usage(
    session: AsyncSession, *, since: datetime | None = None, until: datetime | None = None
) -> dict[str, int]:
    """Daily call counts from durable logs; monetary cost may remain unpriced.

    Cache hits and locally rejected calls are excluded from provider calls.
    Token counts and review reasons are retained in each decision's ai_results.
    """
    since = since or _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (
        await session.execute(
            select(
                AIUsageLog.source,
                AIUsageLog.error_kind,
                func.count(),
                func.coalesce(func.sum(AIUsageLog.cost_cents), 0),
            )
            .where(
                AIUsageLog.created_at >= since,
                AIUsageLog.created_at < until if until is not None else true(),
            )
            .group_by(AIUsageLog.source, AIUsageLog.error_kind)
        )
    ).all()
    summary = {
        "provider_calls": 0,
        "primary_calls": 0,
        "secondary_calls": 0,
        "cache_hits": 0,
        "blocked_calls": 0,
        "reported_cost_cents": 0,
    }
    for source, error_kind, count, cost in rows:
        if str(source).startswith("cache"):
            summary["cache_hits"] += count
        elif error_kind == "ai_rate_or_budget_limited":
            summary["blocked_calls"] += count
        else:
            summary["provider_calls"] += count
            summary["reported_cost_cents"] += cost
            if str(source).endswith("_primary"):
                summary["primary_calls"] += count
            elif str(source).endswith("_secondary"):
                summary["secondary_calls"] += count
    return summary


@dataclass
class AIQuota:
    """Small in-process rate/budget limiter for optional remote AI calls."""

    daily_budget_cents: int = 0
    per_minute_limit: int = 30
    daily_call_limit: int = 1_000
    _day: str = field(default_factory=lambda: _utcnow().date().isoformat())
    _spent_cents: int = 0
    _minute_calls: list[float] = field(default_factory=list)
    _daily_calls: int = 0
    _restored_day: str = ""

    def allow(self, estimated_cost_cents: int = 0, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        self._reset_day_if_needed()
        self._minute_calls = [ts for ts in self._minute_calls if now - ts < 60]
        if len(self._minute_calls) >= self.per_minute_limit:
            return False
        if self._daily_calls >= self.daily_call_limit:
            return False
        if (
            self.daily_budget_cents
            and self._spent_cents + estimated_cost_cents > self.daily_budget_cents
        ):
            return False
        self._minute_calls.append(now)
        self._daily_calls += 1
        return True

    def record(self, cost_cents: int) -> None:
        self._reset_day_if_needed()
        self._spent_cents += max(cost_cents, 0)

    def _reset_day_if_needed(self) -> None:
        today = _utcnow().date().isoformat()
        if today != self._day:
            self._day = today
            self._spent_cents = 0
            self._daily_calls = 0
            self._minute_calls.clear()


@dataclass
class AIReviewService:
    """Runs optional AI moderators and merges their results as soft evidence."""

    enabled: bool
    enabled_groups: set[str]
    text_moderator: TextModerator | None = None
    vision_moderator: VisionModerator | None = None
    review_vision_moderator: VisionModerator | None = None  # P0-3: 第二复核模型
    quota: AIQuota = field(default_factory=AIQuota)
    config_problem: str = ""
    cache_ttl_seconds: int = 86_400
    # P0-3: 双模型条件复核阈值
    primary_direct_threshold: float = 0.90
    secondary_review_low: float = 0.60
    secondary_review_high: float = 0.90

    def __post_init__(self) -> None:
        if not 0 <= self.secondary_review_low < self.primary_direct_threshold <= 1:
            raise ValueError("AI review thresholds must satisfy low < direct <= 1")
        if not 0 <= self.secondary_review_high <= 1:
            raise ValueError("AI secondary threshold must be between 0 and 1")

    def _policy_context(self) -> str:
        """Version all settings that affect review routing and interpretation."""
        return json.dumps(
            {
                "primary": getattr(self.vision_moderator, "model_id", ""),
                "secondary": getattr(self.review_vision_moderator, "model_id", ""),
                "direct": self.primary_direct_threshold,
                "low": self.secondary_review_low,
                "high": self.secondary_review_high,
            },
            sort_keys=True,
        )

    def _merge(
        self, local: ModerationDecision, results: list[AIModerationResult]
    ) -> ModerationDecision:
        return merge_ai_evidence(
            local,
            results,
            primary_direct_threshold=self.primary_direct_threshold,
            secondary_review_low=self.secondary_review_low,
            secondary_review_high=self.secondary_review_high,
        )

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
        if not self.enabled_for_group(msg.external_group_id):
            return decision, []
        if decision.verdict == "violation_high" or decision.is_protected_sender:
            return decision, []

        results: list[AIModerationResult] = []
        text = _ai_text_from_message(msg)
        if self.config_problem:
            results.append(degraded_ai_result(self.config_problem))
            return self._merge(decision, results), results
        if text and self.text_moderator is not None:
            request = AIModerationRequest(
                message_id=msg.message_id,
                group_openid=msg.external_group_id,
                message_provider=msg.provider,
                content_kind="text",
                text=text,
                rule_version_ids=list(rule_version_ids),
                policy_context=self._policy_context(),
            )
            results.append(await self._call_text(session, request))
        for path in media_paths or []:
            if self.vision_moderator is None:
                continue
            results.extend(
                await self._call_vision_path(session, msg, path, rule_version_ids, decision)
            )

        return self._merge(decision, results), results

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
        decision: ModerationDecision,
    ) -> list[AIModerationResult]:
        assert self.vision_moderator is not None
        try:
            media_size = await asyncio.to_thread(lambda: path.stat().st_size)
            if media_size > MAX_AI_MEDIA_BYTES:
                return [
                    degraded_ai_result(
                        "media_too_large_for_ai",
                        model_id=self.vision_moderator.model_id,
                        prompt_version=self.vision_moderator.prompt_version,
                        provider="openai-compatible",
                    )
                ]
            media_bytes = await asyncio.to_thread(path.read_bytes)
        except OSError:
            return [
                degraded_ai_result(
                    "media_unreadable_for_ai",
                    model_id=self.vision_moderator.model_id,
                    prompt_version=self.vision_moderator.prompt_version,
                    provider="openai-compatible",
                )
            ]
        request = AIModerationRequest(
            message_id=msg.message_id,
            group_openid=msg.external_group_id,
            message_provider=msg.provider,
            content_kind="gif" if path.suffix.lower() == ".gif" else "image",
            text=_ai_text_from_message(msg),
            media_bytes=media_bytes,
            media_mime=_mime_from_path(path),
            rule_version_ids=list(rule_version_ids),
            review_role="primary",
            policy_context=self._policy_context(),
        )
        # R-105: human labels remain sample-specific. Only published, versioned
        # rules may generalize them; do not inject recent labels into prompts.
        primary = await self._call_with_cache(session, request, self.vision_moderator, "vision")
        reason = secondary_review_reason(
            primary,
            decision,
            direct_threshold=self.primary_direct_threshold,
            low_threshold=self.secondary_review_low,
        )
        primary = primary.model_copy(update={"review_reason": reason})
        if not reason:
            return [primary]
        reviewer = self.review_vision_moderator
        if reviewer is None or reviewer.model_id == self.vision_moderator.model_id:
            secondary = degraded_ai_result(
                "secondary_missing" if reviewer is None else "secondary_not_independent",
                model_id=reviewer.model_id if reviewer else "unconfigured",
            ).model_copy(
                update={
                    "review_role": "secondary",
                    "review_group": request.media_digest,
                    "review_reason": reason,
                }
            )
        else:
            review_req = request.model_copy(update={"review_role": "secondary"})
            secondary = await self._call_with_cache(session, review_req, reviewer, "vision")
            secondary = secondary.model_copy(update={"review_reason": reason})
        return [primary, secondary]

    async def _call_with_cache(
        self,
        session: AsyncSession,
        request: AIModerationRequest,
        moderator: TextModerator | VisionModerator,
        source: Literal["text", "vision"],
    ) -> AIModerationResult:
        cache_request = request.model_copy(
            update={
                "policy_context": (
                    request.policy_context + "|" + str(getattr(moderator, "base_url", ""))
                )
            }
        )
        cache_key = ai_cache_key(
            cache_request, model_id=moderator.model_id, prompt_version=moderator.prompt_version
        )
        cached = await get_cached_ai_result(session, cache_key)
        if cached is not None:
            await record_ai_usage(session, request, cached, cache_key=cache_key, ok=True)
            return cached
        self.quota._reset_day_if_needed()
        if self.quota._restored_day != self.quota._day:
            usage = await summarize_ai_usage(session)
            self.quota._daily_calls = max(self.quota._daily_calls, usage["provider_calls"])
            self.quota._spent_cents = max(self.quota._spent_cents, usage["reported_cost_cents"])
            self.quota._restored_day = self.quota._day
        if not self.quota.allow():
            result = degraded_ai_result(
                "ai_rate_or_budget_limited",
                model_id=moderator.model_id,
                prompt_version=moderator.prompt_version,
            ).model_copy(
                update={"review_role": request.review_role, "review_group": request.media_digest}
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
            if result.model_id != moderator.model_id:
                raise AIProviderError("provider_model_identity_mismatch")
        except Exception as exc:  # noqa: BLE001 - provider errors must fail closed
            reason = exc.reason if isinstance(exc, AIProviderError) else "provider_call_failed"
            result = degraded_ai_result(
                reason,
                model_id=moderator.model_id,
                prompt_version=moderator.prompt_version,
                provider="openai-compatible",
            ).model_copy(
                update={"review_role": request.review_role, "review_group": request.media_digest}
            )
            await record_ai_usage(
                session, request, result, cache_key=cache_key, ok=False, error_kind=reason
            )
            return result
        result = result.model_copy(
            update={
                "source": source,
                "review_role": request.review_role,
                "review_group": request.media_digest,
                "policy_version": AI_POLICY_VERSION,
                "review_reason": "",
                "cache_hit": False,
            }
        )
        self.quota.record(result.cost_cents)
        await store_ai_result(session, cache_key, result, ttl_seconds=self.cache_ttl_seconds)
        await record_ai_usage(session, request, result, cache_key=cache_key, ok=True)
        return result


def secondary_review_reason(
    primary: AIModerationResult,
    local: ModerationDecision,
    *,
    direct_threshold: float,
    low_threshold: float,
) -> str:
    """Only trusted local policy chooses whether a second opinion is required."""
    if primary.degraded_reason:
        return "primary_degraded"
    allow_hit = any(hit.rule_id.startswith("DR_ALLOW_") for hit in local.rule_hits)
    if "冲突" in local.reason or (allow_hit and primary.category is not None):
        return "rule_or_allowlist_conflict"
    if (
        local.verdict == "record_only"
        and local.category
        and primary.category
        and local.category != primary.category
    ):
        return "local_category_conflict"
    if primary.needs_review:
        return "primary_needs_review"
    if primary.category == "other":
        return "unknown_category"
    if primary.category is None or primary.confidence < low_threshold:
        return ""
    if primary.confidence < direct_threshold:
        return "confidence_gray_zone"
    return ""


def merge_ai_evidence(
    local: ModerationDecision,
    ai_results: list[AIModerationResult],
    *,
    independent_confirmed: bool = False,
    primary_direct_threshold: float = 0.90,
    secondary_review_low: float = 0.60,
    secondary_review_high: float = 0.90,
) -> ModerationDecision:
    """Merge complete review pairs; a missing/failed/disagreeing review is a veto.

    Gray-zone promotion requires primary >= low, secondary >= high, equal
    categories and distinct model IDs. ``independent_confirmed`` is retained
    for call compatibility but cannot override the evidence-based gate.
    """
    usable = [result for result in ai_results if not result.degraded_reason and result.category]
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
    if local.verdict == "violation_high":
        return local.model_copy(update={"rule_hits": local.rule_hits + hits})
    candidates: list[AIModerationResult] = []
    unresolved = any(result.degraded_reason for result in ai_results)
    primaries = [r for r in ai_results if r.source == "vision" and r.review_role == "primary"]
    for primary in primaries:
        secondaries = [
            r
            for r in ai_results
            if r.review_role == "secondary" and r.review_group == primary.review_group
        ]
        reason = primary.review_reason or secondary_review_reason(
            primary,
            local,
            direct_threshold=primary_direct_threshold,
            low_threshold=secondary_review_low,
        )
        if reason == "rule_or_allowlist_conflict":
            # Model agreement is evidence, not authority to override a published
            # allow rule or resolve contradictory local policy without a human.
            unresolved = True
        if reason or secondaries:
            if len(secondaries) != 1:
                unresolved = True
                continue
            secondary = secondaries[0]
            if (
                secondary.source != "vision"
                or secondary.degraded_reason
                or secondary.model_id == primary.model_id
                or secondary.needs_review
                or primary.category not in ("ad", "fraud", "porn", "violence", "flood")
                or primary.category != secondary.category
                or primary.confidence < secondary_review_low
                or secondary.confidence < secondary_review_high
            ):
                unresolved = True
            else:
                candidates.append(primary)
        elif (
            primary.category in ("ad", "fraud")
            and primary.confidence >= primary_direct_threshold
            and not primary.needs_review
        ):
            candidates.append(primary)
    # An orphaned secondary can never become a new primary by filtering.
    primary_groups = {r.review_group for r in primaries}
    if any(
        r.review_role == "secondary" and r.review_group not in primary_groups for r in ai_results
    ):
        unresolved = True
    if candidates and not unresolved and not local.is_protected_sender:
        best = max(candidates, key=lambda r: r.confidence)
        return local.model_copy(
            update={
                "verdict": "violation_high",
                "category": best.category,
                "confidence": min(best.confidence, 0.95),
                "rule_hits": local.rule_hits + hits,
                "recommended_actions": ["recall", "mute", "warn"],
                "reason": "AI条件复核策略通过"
                if best.review_reason
                else "主视觉模型高置信且无冲突",
            }
        )
    meaningful = [r for r in usable if r.confidence >= secondary_review_low]
    if unresolved or meaningful or local.verdict == "record_only" or local.is_protected_sender:
        return local.model_copy(
            update={
                "verdict": "record_only",
                "category": local.category or (usable[0].category if usable else None),
                "confidence": max(
                    local.confidence, min(max((r.confidence for r in usable), default=0), 0.85)
                ),
                "rule_hits": local.rule_hits + hits,
                "recommended_actions": [],
                "reason": (local.reason + "；" if local.reason else "")
                + ("AI复核未形成一致有效证据，转人工" if unresolved else "AI软证据，转人工复核"),
            }
        )
    return local.model_copy(update={"rule_hits": local.rule_hits + hits})


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
