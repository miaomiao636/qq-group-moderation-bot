"""案件与违规记录 ORM 模型（T-104）。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ViolationRecord(Base):
    """有效违规记录：同一成员+同群 30 天窗口内累计。"""

    __tablename__ = "violation_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_openid: Mapped[str] = mapped_column(String(64), index=True)
    member_openid: Mapped[str] = mapped_column(String(64), index=True)
    message_id: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float]
    rule_hits_json: Mapped[str] = mapped_column(Text, default="[]")
    # 原始证据快照：消息文本、附件元数据（脱敏联系方式后存储由调用方负责）
    message_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    action_result_json: Mapped[str] = mapped_column(Text, default="[]")
    case_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    revoke_reason: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )


class Case(Base):
    """人工审核案件：两次违规合并证据后生成，状态机见 `case_sm.py`。"""

    __tablename__ = "cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_no: Mapped[str] = mapped_column(String(32), unique=True)  # 如 R20260905-01
    group_openid: Mapped[str] = mapped_column(String(64), index=True)
    member_openid: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING_REVIEW", index=True)
    violation_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    # 审批链记录：谁批准、确认码、执行出口与结果
    audit_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
