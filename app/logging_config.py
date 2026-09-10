"""日志配置：结构化 JSON 输出，按级别与模块过滤。"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.config import get_settings


class JsonFormatter(logging.Formatter):
    """输出单行 JSON 日志，便于采集与分析。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if hasattr(record, "extra"):
            payload["extra"] = record.extra
        return json.dumps(payload, ensure_ascii=False)


def setup_logging() -> None:
    """配置根日志器。"""
    settings = get_settings()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
    # HTTPX 的 INFO 请求日志含完整 URL；媒体链接可能带签名，禁止进入日志。
    # 项目自身只记录安全事件名、失败类别和不含内容的关联标识。
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
