"""QQ官方动作客户端的组合根 wiring（T-305）。

这是官方 Adapter 与动作编排之间**唯一的**组合点：审核核心
（``app.actions.orchestrator``）不在模块顶层导入任何供应商 Adapter，
只在 OFFICIAL 分支内调用本模块。未来 NapCat 动作 Adapter（T-307）
将在 ``app.actions.onebot_wiring``（或等价模块）提供对称入口。

本模块不实现任何审核逻辑，也不含踢人路径。
"""

from __future__ import annotations

from app.adapters.qq_official.actions import ActionNotConfiguredError, OfficialActionAdapter
from app.adapters.qq_official.auth import TokenManager
from app.config import Settings
from app.core.contracts import ModerationActionClient

__all__ = [
    "ActionNotConfiguredError",
    "build_official_action_client",
    "official_client_configured",
]


def official_client_configured(settings: Settings) -> bool:
    """官方动作出口是否具备最小凭据配置。"""
    return bool(settings.qq_app_id.strip() and settings.qq_app_secret.strip())


def build_official_action_client(settings: Settings) -> ModerationActionClient:
    """构建官方动作客户端；缺少凭据时抛 ``ActionNotConfiguredError``。"""
    if not official_client_configured(settings):
        raise ActionNotConfiguredError("缺少 QQ_APP_ID / QQ_APP_SECRET，官方动作出口未配置")
    token_manager = TokenManager(settings.qq_app_id, settings.qq_app_secret)
    return OfficialActionAdapter(token_manager, api_base=settings.qq_api_base)
