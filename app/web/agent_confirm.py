"""P0-2: Agent 高风险操作二阶段确认。

自然语言 Agent 的写操作（开启动作/切OFFICIAL/改路由/发规则/急停）
必须经过"预览计划 → 用户明确确认 → 执行"三步。确认凭据短时有效、
单次使用、绑定具体变更摘要（不能确认A后执行B）。

进程内存存储——重启后待确认项自动失效（安全侧合理，需重新发起）。
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any

_CONFIRM_TTL_SECONDS = 300  # 5分钟有效
_PENDING: dict[str, PendingConfirmation] = {}


@dataclass
class PendingConfirmation:
    token: str
    action: str
    summary: str
    params: dict[str, Any]
    created_at: float
    expires_at: float
    used: bool = False


# 需要二阶段确认的高风险操作类型
HIGH_RISK_ACTIONS = {
    "group_action_enable",
    "group_route_change",
    "action_mode_change",
    "emergency_stop_toggle",
    "rule_publish",
    "rule_rollback",
    "threshold_expand",
}


def create_confirmation(action: str, summary: str, params: dict[str, Any]) -> str:
    """创建待确认操作，返回确认令牌。"""
    token = secrets.token_urlsafe(32)
    now = time.monotonic()
    _PENDING[token] = PendingConfirmation(
        token=token,
        action=action,
        summary=summary,
        params=dict(params),
        created_at=now,
        expires_at=now + _CONFIRM_TTL_SECONDS,
    )
    return token


def validate_and_consume(token: str, action: str, summary: str) -> PendingConfirmation | None:
    """验证并消费确认令牌。令牌必须匹配 action + summary 且未过期未使用。"""
    pending = _PENDING.get(token)
    if pending is None or pending.used:
        return None
    if time.monotonic() > pending.expires_at:
        _PENDING.pop(token, None)
        return None
    if pending.action != action or pending.summary != summary:
        return None  # 令牌绑定了不同的变更内容
    pending.used = True
    _PENDING.pop(token, None)
    return pending


def cleanup_expired() -> int:
    """清理过期确认，返回清理数量。"""
    now = time.monotonic()
    expired = [t for t, p in _PENDING.items() if now > p.expires_at]
    for t in expired:
        _PENDING.pop(t, None)
    return len(expired)
