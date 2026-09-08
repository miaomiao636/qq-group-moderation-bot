"""官方事件载荷 -> 统一消息契约解析器（T-102）。

只处理实测确认过的 `GROUP_MESSAGE_CREATE` 事件形态（决策 D-012/D-013）：
- 纯文本 / @提及 / 表情（<faceType=...> 富文本标记）
- 图片 / GIF / 语音（含 asr_refer_text）/ 视频 / 文件附件
- 转发记录（"[群聊的聊天记录]" 文本骨架）
- 小程序分享卡片（"[卡片消息]" 文本骨架 + ark_data 结构化字段）

解析失败抛 `EventParseError`，由上层记录审计，不允许静默丢弃。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.adapters.qq_official.contract import (
    Attachment,
    MessageKind,
    Provider,
    Sender,
    SenderRole,
    ShareCardInfo,
    StandardMessage,
)

_KNOWN_EVENT = "GROUP_MESSAGE_CREATE"

_FACE_PATTERN = re.compile(r"<faceType=\d+[^>]*>")
_MENTION_IN_TEXT = re.compile(r"<@([0-9A-F]+)>")

_CONTENT_TYPE_KIND: dict[str, MessageKind] = {
    "image/gif": "gif",
    "voice": "voice",
    "video/mp4": "video",
    "video/quicktime": "video",
    "file": "file",
}
_CARD_PREFIX = "[卡片消息]"
_FORWARD_PREFIX = "[群聊的聊天记录]"

_ROLE_MAP: dict[str, SenderRole] = {
    "owner": "owner",
    "admin": "admin",
    "administrator": "admin",
    "member": "member",
}


class EventParseError(ValueError):
    """事件载荷不符合已知契约时抛出。"""


def _parse_sender(raw: dict[str, Any]) -> Sender:
    author = raw.get("author")
    if not isinstance(author, dict):
        raise EventParseError("事件缺少 author 字段")
    member_openid = str(author.get("member_openid") or "")
    if not member_openid:
        raise EventParseError("author 缺少 member_openid")
    role_raw = str(author.get("member_role") or "member").lower()
    return Sender(
        member_openid=member_openid,
        union_openid=str(author.get("union_openid") or ""),
        username=str(author.get("username") or ""),
        role=_ROLE_MAP.get(role_raw, "unknown"),
        is_bot=bool(author.get("bot", False)),
    )


def _parse_sent_at(raw: dict[str, Any]) -> datetime | None:
    ts = raw.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts))
    except ValueError:
        return None


def _parse_attachments(raw: dict[str, Any]) -> list[Attachment]:
    result: list[Attachment] = []
    raw_attachments = raw.get("attachments")
    if not isinstance(raw_attachments, list):
        return result
    for item in raw_attachments:
        if not isinstance(item, dict):
            continue
        result.append(
            Attachment(
                content_type=str(item.get("content_type") or ""),
                filename=str(item.get("filename") or ""),
                size=item.get("size"),
                url=str(item.get("url") or ""),
                width=item.get("width"),
                height=item.get("height"),
                asr_refer_text=str(item.get("asr_refer_text") or ""),
                voice_wav_url=str(item.get("voice_wav_url") or ""),
            )
        )
    return result


def _parse_share_card(raw: dict[str, Any], content: str) -> ShareCardInfo | None:
    ark = raw.get("ark_data")
    if isinstance(ark, dict):
        fields = ark.get("fields")
        fields_dict = fields if isinstance(fields, dict) else {}
        return ShareCardInfo(
            source=str(fields_dict.get("source") or ""),
            title=str(fields_dict.get("title") or ""),
            prompt=str(ark.get("prompt") or ""),
            tag=str(fields_dict.get("tag") or ""),
            preview_url=str(fields_dict.get("preview") or ""),
            source_logo_url=str(fields_dict.get("source_logo") or ""),
        )
    if content.startswith(_CARD_PREFIX):
        # 无 ark_data 时从文本骨架粗提取 source
        for line in content.splitlines():
            if line.startswith("source: "):
                return ShareCardInfo(source=line.removeprefix("source: ").strip())
    return None


def _decide_kind(
    raw: dict[str, Any],
    content: str,
    attachments: list[Attachment],
) -> MessageKind:
    if content.startswith(_CARD_PREFIX) or raw.get("ark_data"):
        return "share_card"
    if content.startswith(_FORWARD_PREFIX):
        return "forward_record"
    if attachments:
        kinds = {
            _CONTENT_TYPE_KIND.get(
                a.content_type, "image" if a.content_type.startswith("image/") else "file"
            )
            for a in attachments
        }
        has_text = bool(content.strip())
        if len(kinds) == 1 and not has_text:
            return kinds.pop()
        return "mixed"
    if content.strip():
        return "text"
    return "unknown"


def parse_group_message(payload: dict[str, Any]) -> StandardMessage:
    """把 GROUP_MESSAGE_CREATE 事件载荷解析为统一消息契约。"""
    if not isinstance(payload, dict):
        raise EventParseError("事件载荷必须是对象")
    message_id = str(payload.get("id") or "")
    if not message_id:
        raise EventParseError("事件缺少消息 id")
    group_openid = str(payload.get("group_openid") or "")
    if not group_openid:
        raise EventParseError("事件缺少 group_openid")

    content = str(payload.get("content") or "")
    mentions = [
        str(m.get("member_openid"))
        for m in payload.get("mentions") or []
        if isinstance(m, dict) and m.get("member_openid")
    ]
    if not mentions:
        mentions = _MENTION_IN_TEXT.findall(content)

    attachments = _parse_attachments(payload)
    return StandardMessage(
        message_id=message_id,
        event_type=_KNOWN_EVENT,
        group_openid=group_openid,
        group_id=str(payload.get("group_id") or ""),
        sender=_parse_sender(payload),
        sent_at=_parse_sent_at(payload),
        kind=_decide_kind(payload, content, attachments),
        text=content,
        mentions=mentions,
        face_count=len(_FACE_PATTERN.findall(content)),
        attachments=attachments,
        share_card=_parse_share_card(payload, content),
    )


class QQOfficialMessageSource:
    """官方 Adapter 的 ``MessageSource`` 实现（T-305 seam）。

    供审核链路以 ``MessageSource`` 协议注入；provider 固定为 ``qq_official``，
    解析行为与 ``parse_group_message`` 完全一致。
    """

    provider: Provider = "qq_official"

    def parse_group_message(self, payload: Mapping[str, Any], /) -> StandardMessage:
        if not isinstance(payload, dict):
            raise EventParseError("事件载荷必须是对象")
        return parse_group_message(payload)
