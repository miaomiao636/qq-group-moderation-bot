"""OneBot 11 群消息入站解析（T-306）。

把 NapCat 通过反向 WebSocket 推送的 OneBot 11 事件转换为 T-305 传输中立
契约（``app.core.contracts.StandardMessage``），不暴露任何 CQ 段或供应商
原始结构。

硬边界（AGENTS / D-019 / D-020）：
- OneBot 数字 ``group_id``/``user_id``/``message_id`` 以**字符串**进入
  ``external_*`` 中立字段，与 ``provider="onebot"`` 绑定；绝不伪装成
  QQ 官方 ``group_openid``/``member_openid`` 语义，也不得进入官方 API。
- 未知消息段（本解析器不认识段类型）保留**段类型名与数据键名元数据**，
  转为 ``kind="unknown"`` 中立段并附加文本标记，由流水线强制降级人工复核，
  绝不判定为正常。
- 解析失败抛 ``MessageParseError``，由流水线兜底为 record_only 人工记录。

本模块只做纯转换，不产生网络/磁盘 I/O；媒体下载由运行时组合根
（``app.runtime.onebot_wiring``）复用统一媒体安全检查完成。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.core.contracts import (
    Attachment,
    MessageKind,
    MessageParseError,
    MessageSegment,
    Provider,
    Sender,
    ShareCardInfo,
    StandardMessage,
)

# OneBot role -> 中立角色（ anonymous 消息没有角色信息，保持 member 并计匿名标记）
_ROLE_MAP: dict[str, str] = {
    "owner": "owner",
    "admin": "admin",
    "administrator": "admin",
    "member": "member",
}

# 保留元数据时最多展示的未知段数据键数量
_MAX_UNKNOWN_KEYS = 8


def _require_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise MessageParseError(f"事件缺少 {key}")
    return str(value)


def _gif_content_type(data: Mapping[str, Any]) -> str:
    file_name = str(data.get("file") or data.get("url") or "").lower()
    if file_name.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"


def _share_card_from_json(raw: str) -> ShareCardInfo | None:
    """从 OneBot json 卡片段提取中立分享卡片摘要；解析失败返回 None。"""
    try:
        card = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(card, dict):
        return None
    source = str(card.get("source") or card.get("appName") or card.get("app") or "")
    title = str(card.get("title") or card.get("prompt") or "")
    prompt = str(card.get("prompt") or card.get("desc") or "")
    tag = str(card.get("tag") or "")
    preview = str(card.get("preview") or card.get("url") or card.get("jump_url") or "")
    if not (title or prompt or source or preview):
        return None
    return ShareCardInfo(
        source=source[:120],
        title=title[:200],
        prompt=prompt[:200],
        tag=tag[:60],
        preview_url=preview[:500],
    )


def _share_card_from_share(data: Mapping[str, Any]) -> ShareCardInfo | None:
    """OneBot ``share`` 段（链接分享）转中立卡片。"""
    title = str(data.get("title") or "")
    url = str(data.get("url") or "")
    if not (title or url):
        return None
    return ShareCardInfo(
        source=str(data.get("content_source") or "share")[:120],
        title=title[:200],
        prompt=str(data.get("content") or "")[:200],
        preview_url=url[:500],
    )


def _unknown_segment(seg_type: str, data: Mapping[str, Any]) -> tuple[MessageSegment, str]:
    """未知段：保留类型名与数据键名元数据，返回 (中立段, 文本标记)。"""
    keys = ",".join(sorted(str(k) for k in data)[:_MAX_UNKNOWN_KEYS])
    text = f"{seg_type}[{keys}]" if keys else seg_type
    return MessageSegment(kind="unknown", text=text[:120]), f"[未知消息段:{seg_type}]"


class OneBotMessageSource:
    """OneBot 11 入站 seam 实现（T-306）。"""

    provider: Provider = "onebot"

    def parse_group_message(self, payload: Mapping[str, Any], /) -> StandardMessage:
        if not isinstance(payload, dict):
            raise MessageParseError("事件载荷必须是JSON对象")
        if payload.get("post_type") != "message":
            raise MessageParseError(f"post_type 必须为 message，收到 {payload.get('post_type')!r}")
        if payload.get("message_type") != "group":
            raise MessageParseError(
                f"message_type 必须为 group，收到 {payload.get('message_type')!r}"
            )
        message_id = _require_str(payload, "message_id")
        group_id = _require_str(payload, "group_id")
        user_id = _require_str(payload, "user_id")

        sender_raw = payload.get("sender")
        if not isinstance(sender_raw, dict):
            sender_raw = {}
        role = _ROLE_MAP.get(str(sender_raw.get("role") or "member"), "member")
        # 匿名消息：无角色信息，保持 member；原始 anonymous 字段不进入中立契约，
        # 匿名状态以文本标记保留供人工复核
        texts_marker = "[匿名消息]" if payload.get("anonymous") is not None else ""

        segments_raw = payload.get("message")
        string_form = isinstance(segments_raw, str)
        if string_form:
            segments_raw = [{"type": "text", "data": {"text": segments_raw}}]
        if not isinstance(segments_raw, list):
            raise MessageParseError("message 字段必须是段数组或字符串")

        # 下载器（运行时组合根）按附件产生顺序注入的本地安全文件名列表。
        # 该键一旦存在，空值就明确表示下载失败，不得回退到不可信原始路径。
        downloaded_raw = payload.get("_downloaded")
        if isinstance(downloaded_raw, list):
            has_download_results = True
            downloaded: list[Any] = downloaded_raw
        else:
            has_download_results = False
            downloaded = []
        att_seq = 0

        def _local_name(data: Mapping[str, Any]) -> str:
            """只保留单层文件名；下载失败绝不回退到供应商原始路径。"""
            if has_download_results:
                candidate = str(downloaded[att_seq]) if att_seq < len(downloaded) else ""
            else:
                candidate = str(data.get("file") or data.get("name") or "")[:200]
            if not candidate or candidate in (".", ".."):
                return ""
            if "/" in candidate or "\\" in candidate:
                return ""
            return candidate[:200]

        texts: list[str] = []
        attachments: list[Attachment] = []
        neutral_segments: list[MessageSegment] = []
        mentions: list[str] = []
        face_count = 0
        share_card: ShareCardInfo | None = None
        is_forward = False
        has_unknown = False
        # 真实文本段（不含 [语音]/[文件] 等合成标记）——kind 判定只看它
        has_text_segment = False

        for seg in segments_raw:
            if not isinstance(seg, dict):
                has_unknown = True
                neutral_segments.append(MessageSegment(kind="unknown", text="malformed"))
                texts.append("[未知消息段:malformed]")
                continue
            seg_type = str(seg.get("type") or "")
            data = seg.get("data")
            if not isinstance(data, dict):
                data = {}

            if seg_type == "text":
                text = str(data.get("text") or "")
                if text:
                    texts.append(text)
                    has_text_segment = True
                    neutral_segments.append(MessageSegment(kind="text", text=text[:200]))
            elif seg_type == "at":
                qq = str(data.get("qq") or "")
                if qq:
                    mentions.append(qq)
            elif seg_type == "face":
                face_count += 1
            elif seg_type == "image":
                att = Attachment(
                    content_type=_gif_content_type(data),
                    filename=_local_name(data),
                    url=str(data.get("url") or "")[:800],
                    size=int(data["file_size"])
                    if str(data.get("file_size") or "").isdigit()
                    else None,
                )
                att_seq += 1
                attachments.append(att)
                idx = len(attachments) - 1
                kind: MessageKind = "gif" if att.content_type == "image/gif" else "image"
                neutral_segments.append(MessageSegment(kind=kind, attachment_index=idx))
            elif seg_type == "mface":
                # 表情商城/GIF表情：走图片附件通道（通常为 GIF）
                local_name = _local_name(data)
                if not local_name and not has_download_results:
                    local_name = str(data.get("emoji_id") or "mface")[:200]
                att = Attachment(
                    content_type="image/gif",
                    filename=local_name,
                    url=str(data.get("url") or "")[:800],
                )
                att_seq += 1
                attachments.append(att)
                neutral_segments.append(
                    MessageSegment(kind="gif", attachment_index=len(attachments) - 1)
                )
            elif seg_type == "record":
                attachments.append(
                    Attachment(
                        content_type="voice",
                        filename=_local_name(data),
                        url=str(data.get("url") or "")[:800],
                    )
                )
                att_seq += 1
                idx = len(attachments) - 1
                neutral_segments.append(MessageSegment(kind="voice", attachment_index=idx))
                texts.append("[语音]")
            elif seg_type == "video":
                attachments.append(
                    Attachment(
                        content_type="video/mp4",
                        filename=_local_name(data),
                        url=str(data.get("url") or "")[:800],
                    )
                )
                att_seq += 1
                idx = len(attachments) - 1
                neutral_segments.append(MessageSegment(kind="video", attachment_index=idx))
                texts.append("[视频]")
            elif seg_type == "file":
                size_raw = data.get("size")
                attachments.append(
                    Attachment(
                        content_type="file",
                        filename=_local_name(data),
                        url=str(data.get("url") or "")[:800],
                        size=int(size_raw) if isinstance(size_raw, int) else None,
                    )
                )
                att_seq += 1
                idx = len(attachments) - 1
                neutral_segments.append(MessageSegment(kind="file", attachment_index=idx))
                texts.append(f"[文件:{attachments[-1].filename[:60]}]")
            elif seg_type == "reply":
                # 回复/引用：本事件不含被引用内容（需API查询，T-303后评估），保留引用目标元数据
                reply_id = str(data.get("id") or "")
                marker = f"[回复消息:{reply_id[:40]}]" if reply_id else "[回复消息]"
                texts.append(marker)
                neutral_segments.append(MessageSegment(kind="text", text=marker[:200]))
            elif seg_type == "forward":
                # 合并转发：内容无法从本事件取得，必须人工复核
                is_forward = True
                texts.append("[合并转发]")
                neutral_segments.append(
                    MessageSegment(kind="forward_record", text=str(data.get("id") or "")[:60])
                )
            elif seg_type == "json":
                card = _share_card_from_json(str(data.get("data") or ""))
                if card is None:
                    has_unknown = True
                    seg_unknown, marker = _unknown_segment(seg_type, data)
                    neutral_segments.append(seg_unknown)
                    texts.append(marker)
                else:
                    share_card = share_card or card
                    neutral_segments.append(MessageSegment(kind="share_card"))
                    texts.append(f"[卡片:{card.title[:40]}]")
            elif seg_type == "share":
                card = _share_card_from_share(data)
                if card is None:
                    has_unknown = True
                    seg_unknown, marker = _unknown_segment(seg_type, data)
                    neutral_segments.append(seg_unknown)
                    texts.append(marker)
                else:
                    share_card = share_card or card
                    neutral_segments.append(MessageSegment(kind="share_card"))
                    texts.append(f"[卡片:{card.title[:40]}]")
            else:
                has_unknown = True
                seg_unknown, marker = _unknown_segment(seg_type, data)
                neutral_segments.append(seg_unknown)
                texts.append(marker)

        if string_form and any("[CQ:" in str(s.text) for s in neutral_segments if s.kind == "text"):
            # CQ 码字符串形态：媒体等结构无法可靠还原，保留文本但强制人工复核
            has_unknown = True
            neutral_segments.append(MessageSegment(kind="unknown", text="cq_string_form"))

        text = texts_marker + "".join(texts)
        kind = self._resolve_kind(
            is_forward=is_forward,
            share_card=share_card,
            attachments=attachments,
            has_text=has_text_segment,
            has_unknown=has_unknown,
        )

        sent_at: datetime | None = None
        raw_time = payload.get("time")
        if isinstance(raw_time, (int, float)):
            sent_at = datetime.fromtimestamp(float(raw_time), tz=UTC)

        return StandardMessage(
            message_id=message_id,
            event_type="GROUP_MESSAGE_CREATE",
            provider="onebot",
            external_group_id=group_id,
            external_user_id=user_id,
            external_message_id=message_id,
            sender=Sender(
                member_openid=user_id,
                username=str(sender_raw.get("card") or sender_raw.get("nickname") or "")[:64],
                role=role,
            ),
            sent_at=sent_at,
            kind=kind,
            text=text,
            mentions=mentions,
            face_count=face_count,
            segments=neutral_segments,
            attachments=attachments,
            share_card=share_card,
        )

    @staticmethod
    def _resolve_kind(
        *,
        is_forward: bool,
        share_card: ShareCardInfo | None,
        attachments: list[Attachment],
        has_text: bool,
        has_unknown: bool,
    ) -> MessageKind:
        if is_forward:
            return "forward_record"
        if share_card is not None and not attachments:
            return "share_card" if not has_text else "mixed"
        if not attachments and not has_text and not share_card:
            return "unknown"
        media_kinds = {a.content_type for a in attachments}
        if not attachments:
            media_kind: MessageKind | None = None
        elif media_kinds <= {"image/gif"} and len(attachments) == 1:
            media_kind = "gif"
        elif media_kinds <= {"image/jpeg", "image/gif", "image/png"}:
            media_kind = "image"
        elif media_kinds == {"voice"}:
            media_kind = "voice"
        elif media_kinds == {"video/mp4"}:
            media_kind = "video"
        elif media_kinds == {"file"}:
            media_kind = "file"
        else:
            media_kind = "mixed"
        if media_kind is not None and has_text:
            return "mixed"
        if media_kind is not None:
            return media_kind
        return "text"
