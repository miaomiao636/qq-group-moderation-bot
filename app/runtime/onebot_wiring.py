"""NapCat/OneBot 入站事件的运行时组合根（T-306）。

职责：
- 计算持久化去重键 ``onebot:{self_id}:{message_id}``（覆盖重复推送、
  断线重连与进程重启）；
- 复用统一媒体安全检查（``app.adapters.qq_official.media``，R-102-4）：
  大小上限、类型嗅探、磁盘配额、超时、安全文件名；**不执行**任何
  群成员发送的文件，媒体仅作为审核证据下载；
- 调用影子流水线（只记录拟执行动作，外部处罚调用数恒为 0）。

本模块是组合根（runtime 层），允许导入 Adapter；审核核心不允许。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.onebot.parser import OneBotMessageSource
from app.adapters.qq_official.media import download_attachment
from app.core.contracts import MessageParseError, MessageSource, StandardMessage
from app.moderation.image_engine import ImageModerationEngine
from app.moderation.rules import TextRuleEngine
from app.runtime.models import ShadowDecision
from app.runtime.pipeline import MEDIA_DIR, run_pipeline

logger = logging.getLogger(__name__)


def build_onebot_message_source() -> MessageSource:
    """OneBot 入站 seam 组合根（供运行器与测试使用）。"""
    return OneBotMessageSource()


def dedup_key_for(payload: dict[str, Any], message_id: str) -> str:
    """``provider + self_id + message_id`` 持久化去重键。

    缺失 ``self_id`` 或消息ID时拒绝处理，避免不同账号落入同一个
    ``unknown`` 命名空间并互相覆盖。
    """
    self_id = str(payload.get("self_id") or "")
    if not self_id:
        raise MessageParseError("事件缺少 self_id，无法建立跨账号去重键")
    if not message_id:
        raise MessageParseError("事件缺少 message_id，无法建立去重键")
    return f"onebot:{self_id}:{message_id}"


def parse_onebot_event(payload: dict[str, Any]) -> StandardMessage:
    """解析事件为中立消息；失败抛 ``MessageParseError``。"""
    return OneBotMessageSource().parse_group_message(payload)


async def download_onebot_media(
    payload: dict[str, Any],
    msg: StandardMessage,
    dedup_key: str,
    client: httpx.AsyncClient,
) -> None:
    """按附件顺序下载媒体到 data/media/（复用统一媒体安全检查）。

    - 仅接受 ``http(s)://`` URL；本地路径/其他 scheme 一律视为不可信，
      下载结果记为失败 → 流水线判 record_only（不处罚、不放行）；
    - 下载失败/超限/超配额同样记录原因；
    - 下载结果以 ``_downloaded`` 列表按附件序号注入 payload，供流水线
      二次解析时回填本地安全文件名。
    """
    downloaded: list[str] = []
    for att in msg.attachments:
        url = att.url
        if not url.startswith(("http://", "https://")):
            downloaded.append("")
            logger.info("附件无可用下载URL，记为下载失败（转人工）")
            continue
        name, _ext, reason = await download_attachment(
            client, url, MEDIA_DIR, dedup_key, len(downloaded), att.content_type
        )
        if name:
            downloaded.append(name)
        else:
            downloaded.append("")
            logger.info("附件下载失败：%s", reason or "未知原因")
    if downloaded:
        payload["_downloaded"] = downloaded


async def process_onebot_event(
    payload: dict[str, Any],
    session: AsyncSession,
    *,
    text_engine: TextRuleEngine,
    image_engine: ImageModerationEngine,
    dl_client: httpx.AsyncClient,
) -> ShadowDecision | None:
    """处理一条 OneBot 群消息事件：去重认领 → 下载媒体 → 影子流水线。"""
    key = dedup_key_for(payload, str(payload.get("message_id") or ""))
    # The WebSocket intake committed the durable inbox before worker dispatch.
    # Pipeline ownership remains with the existing persistent moderation lease.

    async def _prepare(current_payload: dict[str, Any]) -> None:
        msg = parse_onebot_event(current_payload)
        await download_onebot_media(current_payload, msg, key, dl_client)

    return await run_pipeline(
        payload,
        session,
        message_source=OneBotMessageSource(),
        dedup_key=key,
        text_engine=text_engine,
        image_engine=image_engine,
        prepare_payload=_prepare,
    )
