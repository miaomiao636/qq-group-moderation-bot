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
    # 内部事件键：官方通道等于外部消息ID；OneBot为 onebot:self_id:message_id。
    message_id: Mapped[str] = mapped_column(String(128), unique=True)
    external_message_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    group_openid: Mapped[str] = mapped_column(String(64), index=True)  # 旧镜像，T-305后评估移除
    member_openid: Mapped[str] = mapped_column(String(64), index=True)  # 旧镜像
    # T-305 传输中立身份（与镜像字段双写，权威读取口径）
    provider: Mapped[str] = mapped_column(
        String(16), default="qq_official", server_default="qq_official"
    )
    external_group_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    external_user_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    sender_name: Mapped[str] = mapped_column(
        String(64), default="", index=True
    )  # 群昵称（官方事件自带）
    kind: Mapped[str] = mapped_column(String(24), default="unknown")
    verdict: Mapped[str] = mapped_column(String(24), index=True)
    category: Mapped[str] = mapped_column(String(24), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(String(500), default="")
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
