"""远程AI Adapter的运行时组合根。"""

from functools import lru_cache
from pathlib import Path

from app.adapters.ai.openai_compatible import (
    OpenAICompatibleTextModerator,
    OpenAICompatibleVisionModerator,
)
from app.config import get_settings
from app.moderation.ai import (
    AIQuota,
    AIReviewService,
    TextModerator,
    VisionModerator,
    parse_enabled_groups,
)


@lru_cache
def build_default_ai_review_service() -> AIReviewService:
    """根据环境配置组合AI适配器；默认返回安全禁用服务。"""
    settings = get_settings()
    enabled_groups = parse_enabled_groups(settings.ai_enabled_groups)
    extra_rules = ""
    if settings.ai_prompt_rules_file:
        rules_path = Path(settings.ai_prompt_rules_file)
        if rules_path.exists():
            extra_rules = rules_path.read_text(encoding="utf-8")
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
    if settings.ai_text_model:
        text_moderator = OpenAICompatibleTextModerator(
            base_url=settings.ai_base_url,
            api_key=settings.ai_api_key,
            model_id=settings.ai_text_model,
            timeout_seconds=settings.ai_timeout_seconds,
            prompt_version=settings.ai_prompt_version,
            extra_system_rules=extra_rules,
        )
    if settings.ai_vision_model:
        vision_moderator = OpenAICompatibleVisionModerator(
            base_url=settings.ai_base_url,
            api_key=settings.ai_api_key,
            model_id=settings.ai_vision_model,
            timeout_seconds=settings.ai_timeout_seconds,
            prompt_version=settings.ai_prompt_version,
            extra_system_rules=extra_rules,
        )
    # P0-3: 第二复核模型（仅在灰区/冲突/疑难时调用）
    review_vision_moderator: VisionModerator | None = None
    if settings.ai_review_model:
        review_vision_moderator = OpenAICompatibleVisionModerator(
            base_url=settings.ai_review_base_url or settings.ai_base_url,
            api_key=settings.ai_review_api_key or settings.ai_api_key,
            model_id=settings.ai_review_model,
            timeout_seconds=settings.ai_timeout_seconds,
            prompt_version=settings.ai_prompt_version,
            extra_system_rules=extra_rules,
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
        review_vision_moderator=review_vision_moderator,
        primary_direct_threshold=settings.ai_primary_direct_threshold,
        secondary_review_low=settings.ai_secondary_review_low,
        secondary_review_high=settings.ai_secondary_review_high,
        quota=AIQuota(
            daily_budget_cents=settings.ai_daily_budget_cents,
            per_minute_limit=settings.ai_per_minute_limit,
            daily_call_limit=settings.ai_daily_call_limit,
        ),
    )
