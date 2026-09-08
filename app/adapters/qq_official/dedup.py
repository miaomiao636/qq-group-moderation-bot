"""事件去重兼容导出。

T-305起通用租约实现位于``app.core.dedup``；保留旧导入路径供
QQ官方Adapter和既有测试使用。
"""

from app.core.dedup import (
    DEFAULT_LEASE_SECONDS,
    ProcessingClaim,
    begin_processing,
    check_and_mark,
    mark_failed,
    mark_processed,
    reset_memory_cache,
)

__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "ProcessingClaim",
    "begin_processing",
    "check_and_mark",
    "mark_failed",
    "mark_processed",
    "reset_memory_cache",
]
