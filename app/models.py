"""ORM 模型定义。

T-102 新增：事件去重表（processed_events）与动作审计表（action_logs）。
后续任务（T-104 案件证据等）在此基础上扩展。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, String, Text
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
    """已处理事件登记表：带租约的幂等领取记录。"""

    __tablename__ = "processed_events"

    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), default="GROUP_MESSAGE_CREATE")
    # T-305：传输通道（dedup 键在 contract 阶段演进为 provider+message_id 组合）
    provider: Mapped[str] = mapped_column(
        String(16), default="qq_official", server_default="qq_official"
    )
    external_group_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    external_user_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    status: Mapped[str] = mapped_column(
        String(16), default="PROCESSED", index=True
    )  # PROCESSED / PROCESSING / FAILED / DEAD
    error_message: Mapped[str] = mapped_column(String(500), default="")
    error_kind: Mapped[str] = mapped_column(String(24), default="")
    lease_token: Mapped[str] = mapped_column(String(64), default="", index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class MemberAlias(Base):
    """Retained legacy manual mapping; not an automatic cross-provider identity."""

    __tablename__ = "member_aliases"

    member_openid: Mapped[str] = mapped_column(String(64), primary_key=True)
    qq_number: Mapped[str] = mapped_column(String(16), default="")
    note: Mapped[str] = mapped_column(String(128), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class GroupAlias(Base):
    """群人工备注映射：官方群OpenID -> 群名称（管理员人工核对，非平台保证）。"""

    __tablename__ = "group_aliases"

    group_openid: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class GroupSettings(Base):
    """Legacy settings retained for recovery only; runtime uses ProviderGroupSettings."""

    __tablename__ = "group_settings"

    group_openid: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    moderation_enabled: Mapped[bool] = mapped_column(default=True, server_default="1")
    action_enabled: Mapped[bool] = mapped_column(default=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ProviderGroupSettings(Base):
    """Provider-qualified settings; legacy rows never enable runtime actions."""

    __tablename__ = "provider_group_settings"

    provider: Mapped[str] = mapped_column(String(16), primary_key=True)
    external_group_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    moderation_enabled: Mapped[bool] = mapped_column(default=True, server_default="1")
    action_enabled: Mapped[bool] = mapped_column(default=False, server_default="0")
    version: Mapped[int] = mapped_column(default=1, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class GroupActionOwner(Base):
    """Conservative single owner for a group ID seen across transports."""

    __tablename__ = "group_action_owners"
    external_group_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider: Mapped[str] = mapped_column(String(16), default="")


class HiddenGroup(Base):
    """后台隐藏的群（软删除）：仅影响群管理列表显示，不删除任何数据。"""

    __tablename__ = "hidden_groups"

    provider: Mapped[str] = mapped_column(String(16), primary_key=True)
    external_group_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    reason: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AdminChangePlan(Base):
    """Immutable, short-lived management plan approved by a logged-in human."""

    __tablename__ = "admin_change_plans"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action: Mapped[str] = mapped_column(String(32))
    requestor: Mapped[str] = mapped_column(String(64))
    params_json: Mapped[str] = mapped_column(Text)
    expected_state_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    approved_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SystemSetting(Base):
    """系统级设置：保留期、清理开关等（管理后台可视化配置）。"""

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ActionLog(Base):
    """动作审计表：每次撤回/禁言/警告调用都记录结果（可审计要求）。"""

    __tablename__ = "action_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(16))  # recall / mute / unmute / warn
    group_openid: Mapped[str] = mapped_column(String(64))  # 旧镜像，T-305后评估移除
    target_member_openid: Mapped[str] = mapped_column(String(64), default="")  # 旧镜像
    message_id: Mapped[str] = mapped_column(String(128), default="")
    # T-305 传输中立身份（与镜像字段双写，权威读取口径）
    provider: Mapped[str] = mapped_column(
        String(16), default="qq_official", server_default="qq_official"
    )
    external_group_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    external_user_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    external_message_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    ok: Mapped[bool]
    status_code: Mapped[int | None]
    err_code: Mapped[int | None]
    err_message: Mapped[str] = mapped_column(String(500), default="")
    attempts: Mapped[int] = mapped_column(default=0)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AdminAudit(Base):
    """管理后台审计表：记录所有持久化状态修改。"""

    __tablename__ = "admin_audits"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    operator: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(64), default="")
    target_id: Mapped[str] = mapped_column(String(128), default="")
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
