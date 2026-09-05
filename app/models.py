"""ORM 模型定义。

T-102 新增：事件去重表（processed_events）与动作审计表（action_logs）。
后续任务（T-104 案件证据等）在此基础上扩展。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SystemMeta(Base):
    """系统元信息占位表，用于验证数据库连接与迁移链路。"""

    __tablename__ = "system_meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ProcessedEvent(Base):
    """已处理事件登记表：message_id 主键即幂等去重的最终防线。"""

    __tablename__ = "processed_events"

    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), default="GROUP_MESSAGE_CREATE")
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class MemberAlias(Base):
    """成员人工备注映射：官方OpenID -> 管理员人工核对的QQ号/备注（非官方保证，仅供人工核对）。"""

    __tablename__ = "member_aliases"

    member_openid: Mapped[str] = mapped_column(String(64), primary_key=True)
    qq_number: Mapped[str] = mapped_column(String(16), default="")
    note: Mapped[str] = mapped_column(String(128), default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ActionLog(Base):
    """动作审计表：每次撤回/禁言/警告调用都记录结果（可审计要求）。"""

    __tablename__ = "action_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(16))  # recall / mute / unmute / warn
    group_openid: Mapped[str] = mapped_column(String(64))
    target_member_openid: Mapped[str] = mapped_column(String(64), default="")
    message_id: Mapped[str] = mapped_column(String(128), default="")
    ok: Mapped[bool]
    status_code: Mapped[int | None]
    err_code: Mapped[int | None]
    err_message: Mapped[str] = mapped_column(String(500), default="")
    attempts: Mapped[int] = mapped_column(default=0)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
