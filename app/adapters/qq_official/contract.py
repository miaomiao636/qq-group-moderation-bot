"""统一消息契约（T-102）。

所有上游事件（QQ 官方、未来可能的 NapCat 镜像等）统一转换为本契约，
核心审核逻辑只依赖本模块，不直接接触供应商数据结构。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

MessageKind = Literal[
    "text",
    "image",
    "gif",
    "voice",
    "video",
    "file",
    "forward_record",
    "share_card",
    "mixed",
    "unknown",
]

SenderRole = Literal["owner", "admin", "member", "unknown"]


class Sender(BaseModel):
    """消息发送者（官方身份，OpenID 体系）。"""

    member_openid: str
    union_openid: str = ""
    username: str = ""
    role: SenderRole = "member"
    is_bot: bool = False


class Attachment(BaseModel):
    """消息附件元数据（url 为时效性签名地址，必须即时下载）。"""

    content_type: str = ""
    filename: str = ""
    size: int | None = None
    url: str = ""
    width: int | None = None
    height: int | None = None
    # 语音附件官方自带字段（D-013）
    asr_refer_text: str = ""
    voice_wav_url: str = ""


class ShareCardInfo(BaseModel):
    """小程序/卡片消息结构化信息（来自 ark_data）。"""

    source: str = ""
    title: str = ""
    prompt: str = ""
    tag: str = ""
    preview_url: str = ""
    source_logo_url: str = ""


class StandardMessage(BaseModel):
    """统一消息封装：来源、官方群/成员标识、消息ID、时间、内容、附件与上下文。"""

    message_id: str = Field(min_length=1)
    event_type: str = "GROUP_MESSAGE_CREATE"
    group_openid: str = Field(min_length=1)
    group_id: str = ""
    sender: Sender
    sent_at: datetime | None = None
    received_at: datetime | None = None
    kind: MessageKind = "unknown"
    text: str = ""
    mentions: list[str] = Field(default_factory=list)
    face_count: int = 0
    attachments: list[Attachment] = Field(default_factory=list)
    share_card: ShareCardInfo | None = None

    @property
    def has_media(self) -> bool:
        return bool(self.attachments)
