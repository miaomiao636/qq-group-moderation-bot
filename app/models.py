"""ORM 模型定义。

T-102 新增：事件去重表（processed_events）与动作审计表（action_logs）。
后续任务（T-104 案件证据等）在此基础上扩展。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint
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


class AllowlistTerm(Base):
    """全局白名单词（负责人 2026-09-16）：命中且非严重类别 → 放行。

    normalized 为变体归一化后的匹配形式（apply_variants：谐音/大小写）；
    运行时每条消息直读本表（跨进程立即生效）；变更经 AdminAudit 审计。

    R-115 W03/W04：``sqlite_autoincrement`` 保证删除后 ID 不复用（旧表单不能
    操作替代对象）；``normalized`` 唯一约束防止并发等价词绕过去重。
    """

    __tablename__ = "allowlist_terms"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    term: Mapped[str] = mapped_column(String(64), unique=True)
    normalized: Mapped[str] = mapped_column(String(64), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class AllowlistMember(Base):
    """成员白名单（负责人 2026-09-18）：命中成员的全部消息放行，优先级最高。

    - 身份：``provider + external_user_id``。NapCat 主通道的 ``external_user_id``
      就是 OneBot 数字 QQ 号，本表按**精确相等**匹配，绝不做归一化（数字与关键词
      的变体归一化语义完全不同）；官方通道用 openid，不匹配 QQ 号，两通道隔离。
    - 政策：负责人 2026-09-18 明确选择"不守 B-2 底线"——成员白名单为**全类别完全
      放行**（诈骗/色情/暴力/刷屏同样放行）。开关语义由规则引擎与
      ``POLICY_ALLOW_RULE_IDS`` 全链路保护共同保证，AI/动态规则/媒体层不得升级。
    - 生效：运行时每条消息直读本表（跨进程立即生效，参照 ``AllowlistTerm``）。
    ``sqlite_autoincrement``：删除后 ID 不复用（旧表单不能操作替代对象）；
    ``(provider, external_user_id)`` 唯一约束防止并发重复写入。
    """

    __tablename__ = "allowlist_members"
    __table_args__ = (
        UniqueConstraint("provider", "external_user_id", name="uq_allowlist_member_identity"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(16), default="onebot", server_default="onebot")
    external_user_id: Mapped[str] = mapped_column(String(32))
    note: Mapped[str] = mapped_column(String(128), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


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


class ImageAllowlist(Base):
    """图片感知哈希白名单（负责人 2026-09-19）：命中即视为「负责人认可的图」。

    与 ``allowlist_members`` 同为"人工认可的放行来源"，但**作用域不同**：本表只描述
    **图片外观**，且**不豁免**色情/暴力与本地硬证据（由调用方按既有例外口径处理）。
    ``phash`` 存 64 位 dHash 的十六进制字符串；(phash) 唯一，重复导入不会产生重复行。
    """

    __tablename__ = "image_allowlist"
    __table_args__ = (
        UniqueConstraint("phash", name="uq_image_allowlist_phash"),
        Index("ix_image_allowlist_decision_state", "decision_state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phash: Mapped[str] = mapped_column(String(16))
    note: Mapped[str] = mapped_column(String(64), default="")
    source: Mapped[str] = mapped_column(String(16), default="")
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    created_by: Mapped[str] = mapped_column(String(64), default="")

    decision_state: Mapped[str] = mapped_column(String(16), default="", server_default="")
    decision_source: Mapped[str] = mapped_column(String(96), default="", server_default="")
    decision_operator: Mapped[str] = mapped_column(String(64), default="", server_default="")
    decision_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    history_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")


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
