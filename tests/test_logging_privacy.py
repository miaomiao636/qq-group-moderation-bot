"""库的请求日志不能把签名媒体 URL 或模型地址参数泄漏到标准输出。"""

import logging

from app.logging_config import setup_logging


def test_network_debug_loggers_are_suppressed() -> None:
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    network_levels = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    try:
        setup_logging()
        for name in network_levels:
            assert not logging.getLogger(name).isEnabledFor(logging.INFO)
            assert not logging.getLogger(name).isEnabledFor(logging.DEBUG)
    finally:
        root.handlers = handlers
        root.setLevel(level)
        for name, original in network_levels.items():
            logging.getLogger(name).setLevel(original)
