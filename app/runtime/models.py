"""影子模式判定记录 ORM（T-403 影子阶段）。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ShadowDecision(Base):
    """影子模式判定记录：每条消息的判定结果（不执行动作）。"""

    __tablename__ = "shadow_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(String(128), unique=True)
    group_openid: Mapped[str] = mapped_column(String(64), index=True)
    member_openid: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(24), default="unknown")
    verdict: Mapped[str] = mapped_column(String(24), index=True)
    category: Mapped[str] = mapped_column(String(24), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(String(500), default="")
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
