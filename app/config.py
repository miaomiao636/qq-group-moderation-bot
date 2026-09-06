"""应用配置：从环境变量加载，配置值在加载时进行类型与范围校验，真实凭据不进入仓库。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（app/ 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 合法日志级别
_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


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
    admin_password: str = Field(default="", alias="ADMIN_PASSWORD")

    # 数据保留期（天）
    raw_retention_days: int = Field(default=30, ge=1, alias="RAW_RETENTION_DAYS")
    decision_retention_days: int = Field(default=180, ge=1, alias="DECISION_RETENTION_DAYS")

    # 日志级别
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # QQ 官方机器人凭据（T-102；未配置时适配器拒绝执行动作，应用仍可启动）
    qq_app_id: str = Field(default="", alias="QQ_APP_ID")
    qq_app_secret: str = Field(default="", alias="QQ_APP_SECRET")
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
    ai_prompt_version: str = Field(default="t204-v1", alias="AI_PROMPT_VERSION")

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
            if not self.qq_app_id.strip():
                missing.append("QQ_APP_ID")
            if not self.qq_app_secret.strip():
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
        return self


@lru_cache
def get_settings() -> Settings:
    """返回缓存的配置实例。"""
    return Settings()
