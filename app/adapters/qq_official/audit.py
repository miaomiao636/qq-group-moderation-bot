"""动作审计持久化（T-102）：把 ActionResult 写入 action_logs 表。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.qq_official.actions import ActionResult
from app.models import ActionLog


async def log_action(
    session: AsyncSession,
    result: ActionResult,
    *,
    group_openid: str,
    target_member_openid: str = "",
    message_id: str = "",
    actor: str = "system",
) -> None:
    """把动作结果写入审计表并提交。失败向上抛出，由调用方决定降级策略。"""
    session.add(
        ActionLog(
            action=result.action,
            group_openid=group_openid,
            target_member_openid=target_member_openid,
            message_id=message_id,
            ok=result.ok,
            status_code=result.status_code,
            err_code=result.err_code,
            err_message=result.err_message[:500],
            attempts=result.attempts,
            actor=actor,
        )
    )
    await session.commit()
