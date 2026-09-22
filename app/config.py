"""应用配置：从环境变量加载，配置值在加载时进行类型与范围校验，真实凭据不进入仓库。"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（app/ 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 合法日志级别
_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

_TRUSTED_BIND_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


def _normalize_sqlite_url(url: str) -> str:
    """把相对 SQLite 路径规范化为基于项目根目录的绝对路径。

    问题背景：`.env.example` 使用 `sqlite+aiosqlite:///./data/moderation.db`，
    该相对路径会按进程当前工作目录解析。从不同目录执行迁移或启动时，
    会连接到不同数据库，造成"迁移后启动却连到空库"的漂移。

    本函数在配置层把相对路径统一解析到 `PROJECT_ROOT` 下，使迁移与启动
    无论从哪个工作目录运行都连接同一数据库。绝对路径与 `:memory:` 保持不变。

    兼容的路径形式：
    - `sqlite+aiosqlite:///./data/moderation.db`（相对，含 `./`）
    - `sqlite+aiosqlite:///data/moderation.db`（相对，不含 `./`）
    - `sqlite+aiosqlite:////abs/path/db.db`（绝对，Unix）
    - `sqlite+aiosqlite:///C:/data/db.db` 或 `sqlite+aiosqlite:///C:\\data\\db.db`（Windows 绝对）
    - `sqlite+aiosqlite:///:memory:`（内存库，原样保留）

    明确拒绝的形式：
    - `sqlite+aiosqlite:///C:relative\\db.db`（Windows 盘符相对路径：依赖各盘符的
      当前工作目录，行为不可靠，配置加载时直接报错）
    """
    if not url.startswith("sqlite"):
        return url
    # 形如 sqlite+aiosqlite:///path 或 sqlite:///path
    prefix, sep, path = url.partition(":///")
    if not sep:
        return url
    if not path or path == ":memory:":
        return url
    # Windows 盘符路径：`C:/...` 或 `C:\...` 是绝对路径，原样保留；
    # `C:relative\db.db` 是盘符相对路径（依赖各盘符的当前目录），明确拒绝
    if len(path) >= 2 and path[1] == ":":
        if len(path) >= 3 and path[2] in ("/", "\\"):
            return url
        raise ValueError(
            f"DATABASE_URL 含 Windows 盘符相对路径 {url!r}（如 C:relative\\db.db）。"
            "该路径依赖各盘符的当前工作目录，行为不可靠，请改用绝对路径"
            "（如 sqlite+aiosqlite:///C:/data/moderation.db）"
            "或基于项目根目录的相对路径（如 sqlite+aiosqlite:///./data/moderation.db）。"
        )
    # Unix 绝对路径：以 / 开头
    if path.startswith("/"):
        return url
    # 相对路径：基于项目根目录解析
    resolved = (PROJECT_ROOT / path).resolve()
    return f"{prefix}:///{resolved}"


class Settings(BaseSettings):
    """应用配置。

    所有敏感字段（密钥、Token、密码）只从环境变量读取，绝不写入源码或仓库。
    配置值在加载时进行类型与范围校验，非法值直接拒绝启动。
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 运行环境：local / test / prod
    app_env: Literal["local", "test", "prod"] = Field(default="local", alias="APP_ENV")

    # 运行模式：SAFE（安全模式，默认）/ CONVENIENT（便捷模式）
    run_mode: Literal["SAFE", "CONVENIENT"] = Field(default="SAFE", alias="RUN_MODE")
    action_mode: Literal["SHADOW", "OFFICIAL"] = Field(default="SHADOW", alias="ACTION_MODE")
    emergency_stop: bool = Field(default=False, alias="EMERGENCY_STOP")

    # 数据库
    database_url: str = Field(
        default=f"sqlite+aiosqlite:///{PROJECT_ROOT / 'data' / 'moderation.db'}",
        alias="DATABASE_URL",
    )

    # 管理后台监听地址与端口：默认仅本机，禁止无保护暴露公网
    web_host: str = Field(default="127.0.0.1", alias="WEB_HOST")
    web_port: int = Field(default=8000, ge=1, le=65535, alias="WEB_PORT")

    # 管理后台鉴权（生产环境必须设置强口令）
    admin_username: str = Field(default="admin", alias="ADMIN_USERNAME")
    admin_password: str = Field(default="", alias="ADMIN_PASSWORD", repr=False)
    admin_session_ttl_seconds: int = Field(
        default=3600, ge=300, le=86400, alias="ADMIN_SESSION_TTL_SECONDS"
    )

    # Agent REST API 独立令牌（P0-1）：与 ADMIN_PASSWORD 分离，可独立撤销轮换
    agent_api_token: str = Field(default="", alias="AGENT_API_TOKEN", repr=False)
    agent_api_read_token: str = Field(default="", alias="AGENT_API_READ_TOKEN", repr=False)
    agent_api_write_scopes: str = Field(default="project:read", alias="AGENT_API_WRITE_SCOPES")

    # 数据保留期（天）
    raw_retention_days: int = Field(default=30, ge=1, alias="RAW_RETENTION_DAYS")
    decision_retention_days: int = Field(default=180, ge=1, alias="DECISION_RETENTION_DAYS")
    # Independent of retention and per-file limits. No eviction to satisfy quota.
    media_quota_bytes: int = Field(default=2 * 1024**3, ge=1, alias="MEDIA_QUOTA_BYTES")

    # 日志级别
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # QQ 官方机器人凭据（T-102；未配置时适配器拒绝执行动作，应用仍可启动）
    qq_app_id: str = Field(default="", alias="QQ_APP_ID")
    qq_app_secret: str = Field(default="", alias="QQ_APP_SECRET", repr=False)
    qq_api_base: str = Field(default="https://api.bot.qq.com", alias="QQ_API_BASE")

    # 远程AI辅助审核（T-204）：默认关闭，按群显式启用；密钥只从环境变量读取
    ai_enabled: bool = Field(default=False, alias="AI_ENABLED")
    ai_enabled_groups: str = Field(default="", alias="AI_ENABLED_GROUPS")
    ai_base_url: str = Field(default="", alias="AI_BASE_URL")
    ai_api_key: str = Field(default="", alias="AI_API_KEY", repr=False)
    ai_text_model: str = Field(default="", alias="AI_TEXT_MODEL")
    ai_vision_model: str = Field(default="", alias="AI_VISION_MODEL")
    ai_timeout_seconds: float = Field(default=5.0, ge=0.2, le=60.0, alias="AI_TIMEOUT_SECONDS")
    ai_daily_budget_cents: int = Field(default=0, ge=0, alias="AI_DAILY_BUDGET_CENTS")
    ai_per_minute_limit: int = Field(default=30, ge=1, le=600, alias="AI_PER_MINUTE_LIMIT")
    # 主审 F09b：默认值必须与 app/moderation/ai.py 的 PROMPT_VERSION、.env.example 保持一致，
    # 否则新部署（未设置 AI_PROMPT_VERSION 时）会把审计版本标签写成历史版本。
    ai_prompt_version: str = Field(default="t204-v17", alias="AI_PROMPT_VERSION")
    # 图片感知哈希白名单模式：off（默认，不读不写）/ shadow（只观察不改变判定）/ enforce（未实现）。
    # 必须放在**应用配置**里：`.env` 由 pydantic-settings 装载、不进 os.environ，
    # 只读环境变量会导致写在 .env 里的模式读不到。
    image_hash_mode: str = Field(default="off", alias="IMAGE_HASH_MODE")
    # 业务规则片段文件（相对仓库根）。与 SYSTEM_PROMPT 分离，避免把业务词硬编码进
    # 通用提示词；为空则不追加。见 config/ai_prompt_rules.txt。
    ai_prompt_rules_file: str = Field(default="", alias="AI_PROMPT_RULES_FILE")
    ai_daily_call_limit: int = Field(default=1000, ge=1, alias="AI_DAILY_CALL_LIMIT")

    # P0-3: 双模型条件复核（第二模型仅在灰区/冲突/疑难时调用，控制成本）
    ai_review_model: str = Field(default="", alias="AI_REVIEW_MODEL")
    ai_review_base_url: str = Field(default="", alias="AI_REVIEW_BASE_URL")
    ai_review_api_key: str = Field(default="", alias="AI_REVIEW_API_KEY", repr=False)
    ai_primary_direct_threshold: float = Field(
        default=0.90, ge=0.0, le=1.0, alias="AI_PRIMARY_DIRECT_THRESHOLD"
    )
    ai_secondary_review_low: float = Field(
        default=0.60, ge=0.0, le=1.0, alias="AI_SECONDARY_REVIEW_LOW"
    )
    ai_secondary_review_high: float = Field(
        default=0.90, ge=0.0, le=1.0, alias="AI_SECONDARY_REVIEW_HIGH"
    )

    # NapCat/OneBot 11 反向WebSocket入站（T-306）：默认关闭；访问令牌只从
    # 环境变量或系统凭据读取，绝不写入仓库。启用即强制要求令牌与本机/内网绑定。
    onebot_ws_enabled: bool = Field(default=False, alias="ONEBOT_WS_ENABLED")
    onebot_access_token: str = Field(default="", alias="ONEBOT_ACCESS_TOKEN", repr=False)
    onebot_self_id: str = Field(default="", alias="ONEBOT_SELF_ID")
    onebot_ws_path: str = Field(default="/onebot/ws", alias="ONEBOT_WS_PATH")
    onebot_queue_max: int = Field(default=500, ge=1, le=10000, alias="ONEBOT_QUEUE_MAX")
    onebot_heartbeat_timeout_seconds: int = Field(
        default=90, ge=5, le=3600, alias="ONEBOT_HEARTBEAT_TIMEOUT_SECONDS"
    )

    # T-307：OneBot 真实管理动作（撤回/禁言/警告）。独立于 ACTION_MODE=OFFICIAL
    # 的第二道开关：代码同步、服务重启或 NapCat 重连都不会自动开启真实处罚。
    onebot_actions_enabled: bool = Field(default=False, alias="ONEBOT_ACTIONS_ENABLED")
    # Local owner-controlled rollout stage; changing it requires a service restart.
    onebot_action_stage: Literal["recall_only", "full"] = Field(
        default="recall_only", alias="ONEBOT_ACTION_STAGE"
    )
    onebot_action_timeout_seconds: int = Field(
        default=10, ge=1, le=60, alias="ONEBOT_ACTION_TIMEOUT_SECONDS"
    )

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        """跨字段校验：日志级别合法、生产环境必须设置管理员密码、SQLite 路径规范化。"""
        if self.log_level.upper() not in _VALID_LOG_LEVELS:
            raise ValueError(
                f"LOG_LEVEL 非法：{self.log_level!r}。"
                f"可选值：{', '.join(sorted(_VALID_LOG_LEVELS))}。"
            )
        if self.app_env == "prod" and not self.admin_password.strip():
            raise ValueError(
                "生产环境（APP_ENV=prod）必须设置非空 ADMIN_PASSWORD，"
                "仅含空白字符的密码同样被拒绝。"
            )
        if self.action_mode == "OFFICIAL":
            missing = []
            if self.app_env != "prod":
                missing.append("APP_ENV=prod")
            if not self.admin_password.strip():
                missing.append("ADMIN_PASSWORD")
            # Legacy OFFICIAL names the global live-action gate. A OneBot-only
            # deployment does not need unrelated official API credentials;
            # the official adapter independently refuses to build without them.
            if not self.onebot_actions_enabled and not self.qq_app_id.strip():
                missing.append("QQ_APP_ID")
            if not self.onebot_actions_enabled and not self.qq_app_secret.strip():
                missing.append("QQ_APP_SECRET")
            if self.emergency_stop:
                missing.append("EMERGENCY_STOP=false")
            if missing:
                raise ValueError(
                    "ACTION_MODE=OFFICIAL 需要显式生产配置："
                    + "、".join(missing)
                    + "。默认保持 SHADOW，不调用官方处罚接口。"
                )
        # 相对 SQLite 路径统一解析到项目根目录，避免依赖当前工作目录
        self.database_url = _normalize_sqlite_url(self.database_url)
        if (
            self.ai_enabled
            and self.ai_base_url
            and not self.ai_base_url.startswith(("https://", "http://"))
        ):
            raise ValueError("AI_BASE_URL 必须以 https:// 或 http:// 开头。")
        if self.ai_secondary_review_low >= self.ai_primary_direct_threshold:
            raise ValueError("AI_SECONDARY_REVIEW_LOW 必须小于 AI_PRIMARY_DIRECT_THRESHOLD。")
        if self.ai_review_model.strip() and (
            self.ai_review_model.strip() == self.ai_vision_model.strip()
        ):
            raise ValueError("AI_REVIEW_MODEL 必须与 AI_VISION_MODEL 不同，不能伪装独立复核。")
        allowed_scopes = {
            "project:read",
            "settings:write",
            "actions:enable",
            "routing:write",
            "emergency:stop",
            "rules:publish",
            "rules:rollback",
        }
        scopes = {part.strip() for part in self.agent_api_write_scopes.split(",") if part.strip()}
        if not scopes.issubset(allowed_scopes):
            raise ValueError("AGENT_API_WRITE_SCOPES 含不支持的权限。")
        if self.agent_api_token and self.agent_api_read_token == self.agent_api_token:
            raise ValueError("AGENT_API_READ_TOKEN 必须与 AGENT_API_TOKEN 分离。")
        if self.admin_password and self.admin_password in (
            self.agent_api_token,
            self.agent_api_read_token,
        ):
            raise ValueError("Agent 令牌不得复用 ADMIN_PASSWORD。")
        if self.onebot_self_id and not (
            self.onebot_self_id.isascii()
            and self.onebot_self_id.isdigit()
            and int(self.onebot_self_id) > 0
        ):
            raise ValueError("ONEBOT_SELF_ID 必须是非零 ASCII 数字账号。")
        if self.onebot_actions_enabled and not self.onebot_self_id:
            raise ValueError("ONEBOT_SELF_ID 必须在启用真实动作前显式绑定专用账号。")
        self._validate_onebot_ws()
        # T-307：真实动作经反向WS出站，必须同时开启 WS 入站
        if self.onebot_actions_enabled and not self.onebot_ws_enabled:
            raise ValueError(
                "ONEBOT_ACTIONS_ENABLED 配置错误：真实动作经反向WS出站，"
                "必须同时开启 ONEBOT_WS_ENABLED"
            )
        return self

    def _validate_onebot_ws(self) -> None:
        """T-306：反向WS启用条件 fail-closed——强制令牌与本机/可信内网绑定。"""
        if not self.onebot_ws_enabled:
            return
        problems: list[str] = []
        if not self.onebot_access_token.strip():
            problems.append("ONEBOT_ACCESS_TOKEN 不能为空（NapCat连接必须校验访问令牌）")
        host = self.web_host.strip()
        if not _is_loopback_or_private(host):
            problems.append(
                f"WEB_HOST={host!r} 不允许：OneBot入站只能监听本机回环或可信内网地址"
                "（如 127.0.0.1 / 192.168.x.x / 10.x.x.x），禁止暴露公网"
            )
        if not self.onebot_ws_path.startswith("/"):
            problems.append(f"ONEBOT_WS_PATH={self.onebot_ws_path!r} 必须以 / 开头")
        if problems:
            raise ValueError("ONEBOT_WS_ENABLED 配置错误：" + "；".join(problems))


def _is_loopback_or_private(host: str) -> bool:
    """判断监听地址是否为回环或明确允许的私有网段。

    ``ipaddress.is_private`` 也会把 ``0.0.0.0`` / ``::`` 等不可路由地址
    归为 private；这些通配监听地址会暴露所有网卡，不能作为安全边界。
    """
    if host.lower() == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if addr.is_unspecified or addr.is_multicast:
        return False
    return addr.is_loopback or any(
        addr.version == network.version and addr in network for network in _TRUSTED_BIND_NETWORKS
    )


@lru_cache
def get_settings() -> Settings:
    """返回缓存的配置实例。"""
    return Settings()
