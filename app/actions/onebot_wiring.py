"""OneBot 动作客户端组合根 seam（T-307）。

与 ``app.actions.official_wiring`` 对称：orchestrator 只在 OneBot 分支内
惰性经本模块构建客户端——这是组合根 seam，不是核心对 Adapter 的依赖。
真实动作必须同时满足：
- ``ONEBOT_WS_ENABLED``（反向WS入站在运行）；
- ``ONEBOT_ACTIONS_ENABLED``（独立第二道开关，默认关闭）。
"""

from __future__ import annotations

from app.config import Settings
from app.core.contracts import ModerationActionClient

__all__ = ["build_onebot_action_client", "onebot_actions_configured"]


def onebot_actions_configured(settings: Settings) -> bool:
    return bool(settings.onebot_actions_enabled and settings.onebot_ws_enabled)


def build_onebot_action_client(settings: Settings) -> ModerationActionClient | None:
    """构建绑定到反向WS hub 的 OneBot 动作客户端；未配置时返回 None。"""
    if not onebot_actions_configured(settings):
        return None
    from app.adapters.onebot.actions import OneBotActionClient
    from app.runtime.onebot_actions import onebot_action_hub

    return OneBotActionClient(onebot_action_hub.call)
