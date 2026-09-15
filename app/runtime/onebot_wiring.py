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

import asyncio
import logging
import time
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.onebot.parser import OneBotMessageSource
from app.adapters.qq_official.media import download_attachment
from app.core.contracts import MessageParseError, MessageSource, StandardMessage
from app.db import SessionLocal
from app.moderation.image_engine import ImageModerationEngine
from app.moderation.rules import TextRuleEngine
from app.runtime.models import ShadowDecision
from app.runtime.pipeline import MEDIA_DIR, run_pipeline

logger = logging.getLogger(__name__)

# 进程内去重：每个群只尝试一次自动备注（避免新群刷屏时反复调 API）
_alias_attempted: set[str] = set()
# S10：失败后有限退避——避免瞬时失败被永久锁定，也避免失败风暴反复打 API
_ALIAS_RETRY_COOLDOWN_SECONDS = 600.0
_alias_retry_after: dict[str, float] = {}


async def _autoname_group_task(group_id: str) -> bool:
    """首次见到群时自动从 NapCat 拉取群名写入备注（人工备注永不覆盖）。

    设计为后台任务：不阻塞消息审核。返回 True 表示已终结（成功/无需再试），
    False 表示可重试失败（由调用方按退避策略放行重试）。
    """
    try:
        async with SessionLocal() as session:
            from app.models import GroupAlias
            from app.runtime.onebot_actions import onebot_action_hub

            if await session.get(GroupAlias, group_id) is not None:
                return True  # 人工已备注，绝不覆盖
            resp = await onebot_action_hub.call("get_group_info", {"group_id": int(group_id)})
            # S10：Hub 返回完整 status/retcode/data 响应，群名在 data.group_name；
            # 直接读顶层 group_name 会永远取不到值（成功响应也不保存）。
            payload = resp if isinstance(resp, dict) else {}
            if payload.get("status") != "ok" or payload.get("retcode") != 0:
                logger.debug("自动备注群 %s：非成功响应 %s", group_id, payload.get("retcode"))
                return False
            data = payload.get("data")
            name = str(data.get("group_name") or "").strip() if isinstance(data, dict) else ""
            if not name:
                return True  # 接口正常但无群名，无需重试
            existing = await session.get(GroupAlias, group_id)
            if existing is None:
                existing = GroupAlias(group_openid=group_id)
                session.add(existing)
            existing.name = name[:64]
            await session.commit()
            logger.info("自动备注群 %s -> %s", group_id, name[:64])
            return True
    except Exception:  # noqa: BLE001 - 便利功能，任何失败都不影响审核主链
        logger.debug("自动备注群 %s 失败（忽略）", group_id, exc_info=True)
        return False


async def _autoname_with_retry_scope(group_id: str) -> None:
    """S10：成功终结则记住，失败则允许退避后重试一次以上（有限退避）。"""
    ok = await _autoname_group_task(group_id)
    if ok:
        _alias_retry_after.pop(group_id, None)
    else:
        _alias_attempted.discard(group_id)
        _alias_retry_after[group_id] = time.monotonic() + _ALIAS_RETRY_COOLDOWN_SECONDS


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

    result = await run_pipeline(
        payload,
        session,
        message_source=OneBotMessageSource(),
        dedup_key=key,
        text_engine=text_engine,
        image_engine=image_engine,
        prepare_payload=_prepare,
    )
    # 100+ 群场景：首次见到群时自动拉取群名作备注（后台任务，不阻塞审核）
    group_id = str(payload.get("group_id") or "")
    if (
        group_id
        and group_id not in _alias_attempted
        and time.monotonic() >= _alias_retry_after.get(group_id, 0.0)
    ):
        _alias_attempted.add(group_id)
        asyncio.create_task(_autoname_with_retry_scope(group_id))
    return result
