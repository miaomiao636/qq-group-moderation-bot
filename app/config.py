"""应用配置：通过环境变量加载，真实凭据不进入仓库。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（app/ 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """应用配置。

    所有敏感字段（密钥、Token、密码）只从环境变量读取，绝不写入源码或仓库。
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 运行环境：local / test / prod
    app_env: str = Field(default="local", alias="APP_ENV")

    # 运行模式：SAFE（安全模式，默认）/ CONVENIENT（便捷模式）
    run_mode: str = Field(default="SAFE", alias="RUN_MODE")

    # 数据库
    database_url: str = Field(
        default=f"sqlite+aiosqlite:///{PROJECT_ROOT / 'data' / 'moderation.db'}",
        alias="DATABASE_URL",
    )

    # 管理后台监听地址：默认仅本机，禁止无保护暴露公网
    web_host: str = Field(default="127.0.0.1", alias="WEB_HOST")
    web_port: int = Field(default=8000, alias="WEB_PORT")

    # 管理后台鉴权（生产环境必须设置强口令）
    admin_username: str = Field(default="admin", alias="ADMIN_USERNAME")
    admin_password: str = Field(default="", alias="ADMIN_PASSWORD")

    # 数据保留期（天）
    raw_retention_days: int = Field(default=30, alias="RAW_RETENTION_DAYS")
    decision_retention_days: int = Field(default=180, alias="DECISION_RETENTION_DAYS")

    # 日志级别
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache
def get_settings() -> Settings:
    """返回缓存的配置实例。"""
    return Settings()
