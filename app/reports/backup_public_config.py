"""Recovery template from an explicit public-settings allowlist, never raw .env."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

# Unknown settings, comments, credentials and heartbeat capability URLs are omitted.
PUBLIC_SETTINGS = frozenset(
    [
        "APP_ENV",
        "RUN_MODE",
        "ACTION_MODE",
        "EMERGENCY_STOP",
        "WEB_HOST",
        "WEB_PORT",
        "ADMIN_USERNAME",
        "ADMIN_SESSION_TTL_SECONDS",
        "AGENT_API_WRITE_SCOPES",
        "RAW_RETENTION_DAYS",
        "DECISION_RETENTION_DAYS",
        "MEDIA_QUOTA_BYTES",
        "LOG_LEVEL",
        "QQ_APP_ID",
        "QQ_API_BASE",
        "AI_ENABLED",
        "AI_ENABLED_GROUPS",
        "AI_BASE_URL",
        "AI_TEXT_MODEL",
        "AI_VISION_MODEL",
        "AI_TIMEOUT_SECONDS",
        "AI_DAILY_BUDGET_CENTS",
        "AI_PER_MINUTE_LIMIT",
        "AI_PROMPT_VERSION",
        "IMAGE_HASH_MODE",
        "AI_PROMPT_RULES_FILE",
        "AI_DAILY_CALL_LIMIT",
        "AI_REVIEW_MODEL",
        "AI_REVIEW_BASE_URL",
        "AI_PRIMARY_DIRECT_THRESHOLD",
        "AI_SECONDARY_REVIEW_LOW",
        "AI_SECONDARY_REVIEW_HIGH",
        "ONEBOT_WS_ENABLED",
        "ONEBOT_SELF_ID",
        "ONEBOT_WS_PATH",
        "ONEBOT_QUEUE_MAX",
        "ONEBOT_HEARTBEAT_TIMEOUT_SECONDS",
        "ONEBOT_ACTIONS_ENABLED",
        "ONEBOT_ACTION_STAGE",
        "ONEBOT_ACTION_TIMEOUT_SECONDS",
    ]
)
URL_SETTINGS = frozenset({"QQ_API_BASE", "AI_BASE_URL", "AI_REVIEW_BASE_URL"})


def write_recovery_template(source: Path, target: Path) -> None:
    values = dotenv_values(source, encoding="utf-8", interpolate=False)
    lines = [
        "# Recovery template only. This is NOT the original .env.",
        "# Fill service credentials using .env.example before an approved recovery.",
        "# No services are started by restore; review switches and UNKNOWN intents.",
        'DATABASE_URL="sqlite+aiosqlite:///./data/moderation.db"',
    ]
    for key in sorted(PUBLIC_SETTINGS):
        value = values.get(key)
        if value is None or "$" + "{" in value:
            continue
        if key in URL_SETTINGS and value:
            url = urlsplit(value)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                continue
        lines.append(f"{key}={json.dumps(value, ensure_ascii=False)}")
    with target.open("x", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
