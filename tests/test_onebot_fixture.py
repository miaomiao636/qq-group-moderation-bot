"""T-305 OneBot 中立 fixture 测试。

证明同一审核/案件链路可由 OneBot fixture 驱动：测试内提供最小
``MessageSource`` 实现（**非** T-306 Adapter，不含 WebSocket/媒体下载/
运行器），OneBot 数字 ID 以字符串进入中立字段，影子模式外部调用数为 0。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from app.adapters.qq_official.contract import Attachment, Provider, Sender, StandardMessage
from app.cases.models import ViolationRecord
from app.core.contracts import ActionResult, MessageSource
from app.db import SessionLocal
from app.runtime.pipeline import run_pipeline
from sqlalchemy import func, select

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "onebot"

_ROLE_MAP = {"owner": "owner", "admin": "admin", "administrator": "admin", "member": "member"}


def load_event(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))["event"]


class OneBotFixtureSource:
    """仅用于测试的 OneBot 11 事件 -> 中立契约最小映射（非 T-306 Adapter）。

    T-306 将提供完整的入站 Adapter（反向WebSocket、验签、去重键、媒体下载）；
    本类只证明审核核心对 OneBot 形态的中立消息可完整驱动。
    """

    provider: Provider = "onebot"

    def parse_group_message(self, payload: Mapping[str, Any], /) -> StandardMessage:
        if not isinstance(payload, dict):
            raise ValueError("事件载荷必须是对象")
        message_id = str(payload.get("message_id") or "")
        group_id = str(payload.get("group_id") or "")
        user_id = str(payload.get("user_id") or "")
        if not message_id:
            raise ValueError("事件缺少 message_id")
        if not group_id:
            raise ValueError("事件缺少 group_id")
        sender_raw = payload.get("sender") or {}
        segments = payload.get("message")
        if not isinstance(segments, list):
            segments = []
        texts: list[str] = []
        attachments: list[Attachment] = []
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            seg_type = str(seg.get("type") or "")
            data = seg.get("data") or {}
            if seg_type == "text":
                texts.append(str(data.get("text") or ""))
            elif seg_type == "image":
                attachments.append(
                    Attachment(
                        content_type="image/jpeg",
                        filename=str(data.get("file") or ""),
                        url=str(data.get("url") or ""),
                    )
                )
            else:
                # 未知消息段保留文本占位并降级人工（与T-306语义一致的最小表现）
                texts.append(f"[未知消息段:{seg_type}]")
        kind = "unknown"
        if attachments:
            kind = "image"
        elif any(t for t in texts):
            kind = "text"
        role = _ROLE_MAP.get(str(sender_raw.get("role") or "member"), "member")
        sent_at = None
        if payload.get("time"):
            sent_at = datetime.fromtimestamp(int(payload["time"]), tz=UTC)
        return StandardMessage(
            message_id=message_id,
            event_type="GROUP_MESSAGE_CREATE",
            provider="onebot",
            external_group_id=group_id,
            external_user_id=user_id,
            sender=Sender(
                member_openid=user_id,
                username=str(sender_raw.get("nickname") or ""),
                role=role,  # type: ignore[arg-type]
            ),
            sent_at=sent_at,
            kind=kind,  # type: ignore[arg-type]
            text="".join(texts),
            attachments=attachments,
        )


def test_fixture_source_satisfies_message_source_protocol() -> None:
    assert isinstance(OneBotFixtureSource(), MessageSource)


@pytest.mark.asyncio
async def test_onebot_text_spam_drives_shadow_pipeline() -> None:
    event = load_event("group_message_text.json")
    marker = f"ONEBOT_TEXT_{uuid.uuid4().hex[:8]}"
    event["message_id"] = marker
    async with SessionLocal() as session:
        record = await run_pipeline(event, session, message_source=OneBotFixtureSource())
        assert record is not None
        assert record.verdict in ("violation_high", "record_only")
        # 中立身份落库：OneBot 数字ID以字符串进入中立字段
        assert record.provider == "onebot"
        assert record.external_group_id == "300000001"
        assert record.external_user_id == "200000001"
        # 旧镜像字段同步填充（expand 阶段兼容读取）
        assert record.group_openid == "300000001"
        assert record.member_openid == "200000001"
        detail = json.loads(record.detail_json)
        assert "kick" not in json.dumps(detail).lower()


@pytest.mark.asyncio
async def test_onebot_image_media_missing_degrades_to_record_only() -> None:
    event = load_event("group_message_image.json")
    marker = f"ONEBOT_IMG_{uuid.uuid4().hex[:8]}"
    event["message_id"] = marker
    async with SessionLocal() as session:
        record = await run_pipeline(event, session, message_source=OneBotFixtureSource())
        assert record is not None
        assert record.kind == "image"
        # 媒体本地不存在（fixture URL为占位）→ record_only，绝不放行
        assert record.verdict == "record_only"
        assert record.provider == "onebot"


@pytest.mark.asyncio
async def test_onebot_duplicate_event_skipped() -> None:
    event = load_event("group_message_text.json")
    marker = f"ONEBOT_DUP_{uuid.uuid4().hex[:8]}"
    event["message_id"] = marker
    async with SessionLocal() as session:
        first = await run_pipeline(event, session, message_source=OneBotFixtureSource())
        second = await run_pipeline(event, session, message_source=OneBotFixtureSource())
    assert first is not None
    assert second is None  # 去重拦截，重连/重放不重复处理


@pytest.mark.asyncio
async def test_onebot_message_never_enters_official_action_path() -> None:
    """OneBot 群消息在影子模式下不产生任何官方动作调用或意图。"""
    calls: list[tuple[str, tuple[object, ...]]] = []

    class CountingOfficialClient:
        async def recall(self, g: str, m: str, *, actor: str = "system") -> ActionResult:
            calls.append(("recall", (g, m)))
            return ActionResult(action="recall", ok=True, attempts=1)

        async def mute(self, g: str, u: str, s: int, *, actor: str = "system") -> ActionResult:
            calls.append(("mute", (g, u, s)))
            return ActionResult(action="mute", ok=True, attempts=1)

        async def warn(self, g: str, r: str, t: str, *, actor: str = "system") -> ActionResult:
            calls.append(("warn", (g, r, t)))
            return ActionResult(action="warn", ok=True, attempts=1)

    from app.actions.orchestrator import ActionIntent

    event = load_event("group_message_text.json")
    marker = f"ONEBOT_NOACT_{uuid.uuid4().hex[:8]}"
    event["message_id"] = marker
    async with SessionLocal() as session:
        record = await run_pipeline(
            event,
            session,
            official_action_client=CountingOfficialClient(),  # type: ignore[arg-type]
            message_source=OneBotFixtureSource(),
        )
        intent_count = (
            await session.execute(
                select(func.count())
                .select_from(ActionIntent)
                .where(ActionIntent.message_id == marker)
            )
        ).scalar_one()
        violation_count = (
            await session.execute(
                select(func.count())
                .select_from(ViolationRecord)
                .where(ViolationRecord.external_group_id == "300000001")
            )
        ).scalar_one()

    assert record is not None
    assert calls == []  # 影子模式：外部调用数为0
    assert intent_count == 0  # 默认SHADOW不落动作意图
    # OneBot消息不得经官方路径进入违规阶梯（违规只能由provider路由后的出口触发）
    assert violation_count == 0
