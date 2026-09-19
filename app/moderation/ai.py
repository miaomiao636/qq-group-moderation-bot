"""Provider-neutral AI evidence and deterministic conditional-review policy."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
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
from app.moderation.decision import (
    ALLOWLIST_ALLOW_RULE_ID,
    ALLOWLIST_NON_EXEMPT_CATEGORIES,
    CERTIFICATE_AD_ALLOW_RULE_ID,
    MINIPROGRAM_QR_ALLOW_RULE_ID,
    POLICY_ALLOW_RULE_IDS,
    Category,
    ModerationDecision,
    RuleHit,
)

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

PROMPT_VERSION = "t204-v16"
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
    # 负责人 2026-09-18：视觉模型的结构化判定——图中是否含**微信小程序二维码**。
    # 命中即"一律通过"（严重类别与本地硬证据例外见 merge_ai_evidence）。
    # 只有视觉通道有意义；文字通道恒为 False。
    has_miniprogram_code: bool = False
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
    # 小程序码标记：严格布尔（供应商给字符串一律拒收，不猜），且只有视觉通道有意义。
    miniprogram_raw = payload.get("has_miniprogram_code", False)
    if not isinstance(miniprogram_raw, bool):
        raise AIProviderError("provider_invalid_has_miniprogram_code")
    has_miniprogram_code = bool(miniprogram_raw) and source == "vision"
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
            has_miniprogram_code=has_miniprogram_code,
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
                    request.policy_context
                    + "|"
                    + str(getattr(moderator, "base_url", ""))
                    + "|"
                    # R05：实际生效提示词摘要（含外置业务规则）必须进入缓存指纹，
                    # 否则改规则后旧缓存仍命中。
                    + str(getattr(moderator, "prompt_digest", ""))
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
            # 限流被触发意味着该消息会静默降级（AI 证据缺失）→ 必须告警留痕，
            # 不得让运营方误以为审核仍在正常工作。
            logger.warning(
                "AI 调用被限流/预算拦截（ai_rate_or_budget_limited）：minute_calls=%d "
                "daily_calls=%d spent_cents=%d model=%s —— 消息将降级转人工，"
                "请检查 AI_PER_MINUTE_LIMIT / AI_DAILY_CALL_LIMIT / AI_DAILY_BUDGET_CENTS",
                len(self.quota._minute_calls),
                self.quota._daily_calls,
                self.quota._spent_cents,
                moderator.model_id,
            )
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
    if primary.category == "ad" and any(
        hit.rule_id == CERTIFICATE_AD_ALLOW_RULE_ID for hit in local.rule_hits
    ):
        return "rule_or_allowlist_conflict"
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


_FRAUD_DIRECT_MIN = 0.85  # 诈骗误报代价最高，单独要求更高置信度

logger = logging.getLogger(__name__)


def _direct_threshold(category: str | None, base: float) -> float:
    """诈骗类误报代价最高（实测 0.78/0.82 的 fraud 为假阳性），单独抬高门槛。"""
    return max(base, _FRAUD_DIRECT_MIN) if category == "fraud" else base


def _confident_normal(result: AIModerationResult, low_threshold: float) -> bool:
    """确定性正常结论：无违规类别、未降级、不需人工且置信度达门槛（S01）。"""
    return (
        not result.degraded_reason
        and result.category is None
        and not result.needs_review
        and result.confidence >= low_threshold
    )


def _cross_modal_veto(opposite: list[AIModerationResult], low_threshold: float) -> bool:
    """跨模态矛盾检测（S01/R01 未闭环项）。

    对面模态对同一内容给出确定性正常、或明确要求人工时，本模态不得单独升罚。
    只对确实产生过结论的模态生效：文字通道仅在文本非空时调用、
    视觉通道仅在存在媒体时调用，因此"另一模态未参与"不构成矛盾。
    """
    return any(
        not r.degraded_reason and (_confident_normal(r, low_threshold) or r.needs_review)
        for r in opposite
    )


# D-039 的例外集合：**只有色情与暴力/违禁品**不因小程序码放行。
# 负责人 2026-09-18 晚修订：**诈骗不再例外**——图含小程序码时，诈骗内容同样放行
# （此前沿用 B-2 把 fraud 也列为例外，导致"支付宝亲密号"这类图被撤回）。
_MINIPROGRAM_QR_BLOCKED_CATEGORIES = frozenset({"porn", "violence"})
# 本地硬证据：图片外观不得覆盖这些本地判定（防"配一张带码图就绕过黑名单/联系方式/卡片规则"）。
_LOCAL_HARD_EVIDENCE_RULES = frozenset({"R001", "R003", "R006"})


def _secondary_pair_is_valid(
    primary: AIModerationResult,
    secondary: AIModerationResult,
    *,
    secondary_review_low: float,
    secondary_review_high: float,
) -> bool:
    """一审/二审是否构成**有效复核结论**（主审 F02-R：QR 门与主路径共用同一判据）。

    有效条件：二审为视觉、未降级、非同一模型、不要求人工、类别与首轮一致、
    首轮达到低阈值且二审达到高阈值。
    """
    return not (
        secondary.source != "vision"
        or secondary.degraded_reason
        or secondary.model_id == primary.model_id
        or secondary.needs_review
        or primary.category not in ("ad", "fraud", "porn", "violence", "flood")
        or primary.category != secondary.category
        or primary.confidence < secondary_review_low
        or secondary.confidence < secondary_review_high
    )


def attachment_reviews_unresolved(
    ai_results: list[AIModerationResult],
    local: ModerationDecision | None = None,
    *,
    primary_direct_threshold: float = 0.90,
    secondary_review_low: float = 0.60,
    secondary_review_high: float = 0.90,
) -> bool:
    """是否有**附件的复核对未形成结论**（主审 F02-R）——在线与离线**唯一**判据（主审 R6-01）。

    判据：任一首轮结果需要复核（灰区/冲突）却没有**恰好一个**有效二审
    （异类、低置信、非独立模型、降级、需人工），或存在孤儿二审。
    这样"另一张图上有小程序码"就不可能替这张图的未决复核收尾。

    **离线必须调本函数**：离线工具只有 `model_dump()` 出的字典，用
    `AIModerationResult.model_validate` 还原后传入即可——不存在"在线看二审有效性、
    离线只看两个布尔字段"的第二套判据。
    `local is None`（离线无本地判定对象）时只用结果里**已持久化**的 `review_reason`：
    既**不重算**，也**不按默认阈值把"缺证据"猜成"已消疑"**（缺证据的旧记录由调用方标
    `unknown`，按保守方向计入例外）。
    """
    primaries = [r for r in ai_results if r.source == "vision" and r.review_role == "primary"]
    for primary in primaries:
        secondaries = [
            r
            for r in ai_results
            if r.review_role == "secondary" and r.review_group == primary.review_group
        ]
        reason = primary.review_reason or (
            ""
            if local is None
            else secondary_review_reason(
                primary,
                local,
                direct_threshold=primary_direct_threshold,
                low_threshold=secondary_review_low,
            )
        )
        if (reason or secondaries) and (
            len(secondaries) != 1
            or not _secondary_pair_is_valid(
                primary,
                secondaries[0],
                secondary_review_low=secondary_review_low,
                secondary_review_high=secondary_review_high,
            )
        ):
            return True
    primary_groups = {r.review_group for r in primaries}
    return any(
        r.review_role == "secondary" and r.review_group not in primary_groups for r in ai_results
    )


def _attachment_reviews_unresolved(
    ai_results: list[AIModerationResult],
    local: ModerationDecision,
    *,
    primary_direct_threshold: float = 0.90,
    secondary_review_low: float = 0.60,
    secondary_review_high: float = 0.90,
) -> bool:
    """兼容包装（原调用点不变）：统一走 `attachment_reviews_unresolved`。"""
    return attachment_reviews_unresolved(
        ai_results,
        local,
        primary_direct_threshold=primary_direct_threshold,
        secondary_review_low=secondary_review_low,
        secondary_review_high=secondary_review_high,
    )


def _miniprogram_qr_allow(
    local: ModerationDecision,
    ai_results: list[AIModerationResult],
    *,
    primary_direct_threshold: float = 0.90,
    secondary_review_low: float = 0.60,
    secondary_review_high: float = 0.90,
) -> RuleHit | None:
    """D-039：图片含微信小程序二维码 → 放行标记；不该放行时返回 ``None``。

    保留两个例外：
    1. **色情 / 暴力违禁品**（负责人 2026-09-18 晚修订：仅此两类）：本地或任一 AI
       结果给出 porn/violence 时不放行；**诈骗不再例外**；
    2. **本地硬证据**：命中 R001 黑名单词 / R003 联系方式 / R006 分享卡片 / DR_ 动态
       规则时不放行。

    三个阈值必须由调用方传入**本服务实际配置值**（主审 F02-R-2）：QR 入口与主路径
    必须只有**一套**阈值，否则把二审通过线配成 0.95 时，QR 入口仍按隐式默认 0.90
    收尾"未决复核"，等于支持的配置被旁路。
    """
    if not any(result.source == "vision" and result.has_miniprogram_code for result in ai_results):
        return None
    # 主审 F02（R-108）：**任一附件尚未定论**（需人工 / 降级超时 / 结论缺失）时，
    # 不得用"另一张图上的码"替它完成审核——落回既有 record_only 转人工路径。
    # 只约束附件通道（vision/degraded），不牵连文字通道：否则"图带码 + 有文字"
    # 的普通消息会被无谓拦下。
    if any(
        result.degraded_reason or result.needs_review
        for result in ai_results
        if result.source in ("vision", "degraded")
    ):
        return None
    # 主审 F02-R：**任何附件的复核对未形成结论**（缺二审/异类/低置信/非独立模型/孤儿二审）
    # 时同样不授予放行——否则"另一张图上的码"会替这张图的未决复核收尾。
    if _attachment_reviews_unresolved(
        ai_results,
        local,
        primary_direct_threshold=primary_direct_threshold,
        secondary_review_low=secondary_review_low,
        secondary_review_high=secondary_review_high,
    ):
        return None
    if local.category in _MINIPROGRAM_QR_BLOCKED_CATEGORIES:
        return None
    if any(
        result.category in _MINIPROGRAM_QR_BLOCKED_CATEGORIES
        for result in ai_results
        if not result.degraded_reason
    ):
        return None
    if any(
        hit.rule_id in _LOCAL_HARD_EVIDENCE_RULES or hit.rule_id.startswith("DR_")
        for hit in local.rule_hits
    ):
        return None
    return RuleHit(
        rule_id=MINIPROGRAM_QR_ALLOW_RULE_ID,
        rule_name="miniprogram_qr",
        category="ad",
        confidence_delta=0.0,
        evidence_masked="图片含小程序二维码，按负责人口径放行（严重类别与本地硬证据除外，2026-09-18）",
    )


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
    # D-039（负责人 2026-09-18）：图片含微信小程序二维码 → 一律通过。
    # 放在"本地已违规"分支之前，因为它要能压过仅由广告软信号构成的本地高置信；
    # 例外（严重类别、本地硬证据）在 helper 内判断。
    qr_allow = _miniprogram_qr_allow(
        local,
        ai_results,
        primary_direct_threshold=primary_direct_threshold,
        secondary_review_low=secondary_review_low,
        secondary_review_high=secondary_review_high,
    )
    if qr_allow is not None:
        return local.model_copy(
            update={
                "verdict": "allow",
                "category": None,
                "confidence": 0.0,
                "recommended_actions": [],
                "rule_hits": local.rule_hits + hits + [qr_allow],
                "reason": "图片含小程序二维码，按负责人口径放行（D-039）",
            }
        )
    if local.verdict == "violation_high":
        return local.model_copy(update={"rule_hits": local.rule_hits + hits})
    # 负责人 2026-09-16 政策放行：D-031 办证 / D-032 卡片为全类别完全放行——
    # AI 结果（含独立二审确认、严重类别疑似）一律不得升级或转人工，仅并入证据。
    if local.verdict == "allow" and any(
        hit.rule_id in POLICY_ALLOW_RULE_IDS for hit in local.rule_hits
    ):
        return local.model_copy(update={"rule_hits": local.rule_hits + hits})
    # D-033 全局白名单：**仅豁免广告**（R-115 W01/C03 整改；R1 复验修复）——
    # 提前放行要求"审核已完成"：调用失败/配置错误（degraded）、待人工
    # （needs_review）、显式未知类别（other）一律转人工，绝不以"未出现四类有效
    # 结果"为放行充分条件。判据必须基于**原始全量结果**——`usable` 已滤掉
    # category=None，对空集合检查 needs_review 会恒真并静默放行（R1 缺陷：
    # "两模型都明确要求人工"仍被放行）。category=None 本身不是未决：仅当它同时
    # needs_review/降级时转人工；纯 None（确定性正常）与 AI 未启用（空列表）可
    # 放行（主审控制组，不得倒退）。
    if (
        local.verdict == "allow"
        and any(hit.rule_id == ALLOWLIST_ALLOW_RULE_ID for hit in local.rule_hits)
        and not any(result.category in ALLOWLIST_NON_EXEMPT_CATEGORIES for result in usable)
        and not any(result.degraded_reason for result in ai_results)
        and not any(result.needs_review for result in ai_results)
        and not any(result.category == "other" for result in ai_results)
    ):
        return local.model_copy(update={"rule_hits": local.rule_hits + hits})
    candidates: list[AIModerationResult] = []
    # R-115 C02：白名单命中时——广告证据不参与处罚候选（非广告证据独立达到原
    # 门槛才允许升级）；弱严重信号仍保留在证据与转人工路径中。
    _allowlist_scoped = any(hit.rule_id == ALLOWLIST_ALLOW_RULE_ID for hit in local.rule_hits)
    unresolved = any(result.degraded_reason for result in ai_results)
    # S01：跨模态矛盾（文字判广告/图文判正常、或任一模态要求人工）不得直接升罚。
    _text_signals = [r for r in ai_results if r.source == "text"]
    _vision_signals = [r for r in ai_results if r.source == "vision"]
    _text_veto = _cross_modal_veto(_text_signals, secondary_review_low)
    # A04：视觉侧的 veto 必须基于"复核对之后仍未消解的疑问"——
    # 延后到视觉循环结束后计算（见下方 `_vision_veto`），
    # 已获有效二审确认的 primary 不再被其原始 needs_review 标记否决。
    _vision_resolved_ids: set[int] = set()
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
            if not _secondary_pair_is_valid(
                primary,
                secondary,
                secondary_review_low=secondary_review_low,
                secondary_review_high=secondary_review_high,
            ):
                unresolved = True
            elif _text_veto:
                # S01：文字通道对同一内容给出确定性正常或要求人工——跨模态矛盾，
                # 视觉单通道不得升罚，保留人工。
                unresolved = True
            else:
                if not (_allowlist_scoped and primary.category == "ad"):
                    candidates.append(primary)
                _vision_resolved_ids.add(id(primary))
        elif (
            primary.category in ("ad", "fraud", "porn")
            and primary.confidence >= _direct_threshold(primary.category, primary_direct_threshold)
            and not primary.needs_review
        ):
            if _text_veto:
                unresolved = True
            else:
                if not (_allowlist_scoped and primary.category == "ad"):
                    candidates.append(primary)
                _vision_resolved_ids.add(id(primary))
    # A04：有效二审已确认的视觉结论，其原始 needs_review 疑问视为已消解；
    # 只有"确定性正常"或"未被消解的 gray/needs_review"才阻止文字单通道升罚。
    _vision_hard_normal = any(_confident_normal(v, secondary_review_low) for v in _vision_signals)
    _vision_gray_unresolved = any(
        v.source == "vision" and v.needs_review and id(v) not in _vision_resolved_ids
        for v in _vision_signals
    )
    _vision_veto = _vision_hard_normal or _vision_gray_unresolved
    # 文字通道（R01）：与视觉完全同一套本地保护。实测广告文本不被确定性规则命中，
    # AI 是唯一判据，允许高置信直接升级；但规则/白名单冲突、本地类别冲突、
    # needs_review、unknown_category 一律不升级（转人工或条件复核）。
    # 此前仅检查类别/置信度，复现了"本地已判转人工却被升级为撤回+禁言建议"。
    # T1（主审 2026-09-17 ddeb893 复验）：非降级文字结果提出 needs_review 时，
    # 其尚未消解的疑问必须与视觉"未消解 gray"同口径进入人工兜底条件——
    # category=None 不在 usable 中，若不在此显式记录，最终会静默落回 allow
    # （复现：纯文字 None+needs_review=True 被放行）。只补"原本会落到 allow"的
    # 路径；升级判定与既有 record_only 分支的原因/类别口径均不变。
    unresolved_text_review = False
    for text_result in ai_results:
        if text_result.source != "text" or text_result.degraded_reason:
            continue
        if text_result.needs_review:
            unresolved_text_review = True
        reason = secondary_review_reason(
            text_result,
            local,
            direct_threshold=primary_direct_threshold,
            low_threshold=secondary_review_low,
        )
        if reason in ("rule_or_allowlist_conflict", "local_category_conflict"):
            unresolved = True
        elif (
            reason == ""
            and text_result.category in ("ad", "fraud", "porn")
            and text_result.confidence
            >= _direct_threshold(text_result.category, primary_direct_threshold)
            and not text_result.needs_review
        ):
            if _vision_veto:
                # S01：视觉通道对同一内容给出确定性正常或要求人工——跨模态矛盾，
                # 文字单通道不得升罚，保留人工。
                unresolved = True
            else:
                if not (_allowlist_scoped and text_result.category == "ad"):
                    candidates.append(text_result)
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
    # v13（负责人 2026-09-15 方案 B-2 + 场景③小修）：低置信的严重类别疑似
    # （诈骗/色情/暴力违禁品）不得静默放行——保留类别转人工记录（不处罚不
    # 撤回），交人工核对。此处到达的严重结果均未通过候选条件（低置信/需人工/
    # 有冲突），静默丢弃类别会让疑似高危内容完全失去护栏。
    severe_suspects = [r for r in usable if r.category in ("fraud", "porn", "violence")]
    if (
        unresolved
        # T1：文字通道未消解的 needs_review（含 category=None）必须转人工，
        # 不得因"未产出可用类别"而静默落回 allow。
        or unresolved_text_review
        or meaningful
        or severe_suspects
        or local.verdict == "record_only"
        or local.is_protected_sender
    ):
        # v13.1（主审 P2 修复）：转人工分支的类别选择——严重疑似优先于普通广告
        # 类别（本地办证 record_only/ad 或先出现的广告结果不得掩盖严重疑似）；
        # 多个严重类别用确定性规则（置信度降序，同置信按固定顺序）；
        # 类别与置信度必须同源——不得把本地广告的高置信度充当严重疑似置信度。
        # verdict 恒为 record_only、recommended_actions 恒为空：本分支只做
        # 类别标记供人工核对，绝不改变处罚判定（办证内容底线不变）。
        severe_rank = {"fraud": 0, "porn": 1, "violence": 2}
        if severe_suspects:
            best_severe = sorted(
                severe_suspects,
                key=lambda r: (-r.confidence, severe_rank.get(r.category or "", 9)),
            )[0]
            category = best_severe.category
            confidence = best_severe.confidence
        else:
            # R-115 R2：白名单命中时，广告证据不作为持久类别/分数来源——类别与
            # 置信度只用非广告证据（与规则的弱/强分支同源口径一致；不得让 AI
            # 广告分把非广告记录的置信度抬到 0.85）。
            visible = [r for r in usable if r.category != "ad"] if _allowlist_scoped else usable
            category = local.category or (visible[0].category if visible else None)
            confidence = max(
                local.confidence, min(max((r.confidence for r in visible), default=0), 0.85)
            )
        if unresolved:
            reason = "AI复核未形成一致有效证据，转人工"
        elif severe_suspects and not meaningful:
            reason = "AI疑似严重类别（低置信），保留类别转人工核对"
        else:
            reason = "AI软证据，转人工复核"
        return local.model_copy(
            update={
                "verdict": "record_only",
                "category": category,
                "confidence": confidence,
                "rule_hits": local.rule_hits + hits,
                "recommended_actions": [],
                "reason": (local.reason + "；" if local.reason else "") + reason,
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
