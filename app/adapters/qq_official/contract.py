"""统一消息契约（T-102；T-305 起为兼容再导出）。

T-305 把传输中立的契约定义上移到 ``app.core.contracts``；本模块保留为
QQ 官方 Adapter 与既有调用方的兼容入口（expand 阶段不改任何导入方）。
官方 Adapter 专有语义（OpenID 体系）只属于本目录，核心模块不得反向依赖。
"""

from __future__ import annotations

from app.core.contracts import (
    PROVIDERS,
    ActionName,
    ActionResult,
    Attachment,
    MessageKind,
    MessageSource,
    ModerationActionClient,
    Provider,
    Sender,
    SenderRole,
    ShareCardInfo,
    StandardMessage,
)

__all__ = [
    "ActionName",
    "ActionResult",
    "Attachment",
    "MessageKind",
    "MessageSource",
    "ModerationActionClient",
    "PROVIDERS",
    "Provider",
    "Sender",
    "SenderRole",
    "ShareCardInfo",
    "StandardMessage",
]
