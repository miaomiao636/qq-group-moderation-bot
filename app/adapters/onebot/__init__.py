"""NapCat/OneBot 入站Adapter（T-306）。

只包含纯事件解析；反向WebSocket传输层与运行时组合在
``app/runtime/onebot_ws.py`` / ``app/runtime/onebot_wiring.py``。

硬边界：本包不实现任何管理动作（撤回/禁言/警告/踢人），
OneBot 管理动作 Adapter 属于 T-307 并默认影子关闭。
"""
