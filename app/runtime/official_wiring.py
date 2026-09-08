"""QQ官方入站Adapter的运行时组合根。"""

from app.adapters.qq_official.parser import QQOfficialMessageSource
from app.core.contracts import MessageSource


def build_official_message_source() -> MessageSource:
    """保留现有官方运行入口，不让审核流水线依赖Adapter。"""
    return QQOfficialMessageSource()
