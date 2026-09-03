"""ORM 模型定义。

脚手架阶段仅定义最小占位模型，业务模型（消息、案件、证据、动作、审计）在
T-102/T-104 等任务中逐步补充。
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
