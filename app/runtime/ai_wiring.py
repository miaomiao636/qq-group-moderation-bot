"""远程AI Adapter的运行时组合根。"""

from functools import lru_cache

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
