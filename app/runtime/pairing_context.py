"""Normalize unfinished OneBot events for ordering, without copying raw content onward."""

from __future__ import annotations

import json
from datetime import UTC

from sqlalchemy import String, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.onebot.parser import OneBotMessageSource
from app.core.contracts import MessageParseError, Sender, StandardMessage
from app.moderation.wall_pair import WALL_PAIR_WINDOW_SECONDS
from app.runtime.inbox import InboxEvent

_ACTIVE_MESSAGES: dict[object, tuple[str, StandardMessage]] = {}


def _minimal_message(msg: StandardMessage, event_key: str) -> StandardMessage:
    return StandardMessage(
        message_id=event_key,
        external_message_id=msg.external_message_id,
        provider=msg.provider,
        external_group_id=msg.external_group_id,
        external_user_id=msg.external_user_id,
        sender=Sender(),
        sent_at=msg.sent_at,
        kind=msg.kind,
    )


def register_active_pairing_message(token: object, msg: StandardMessage, event_key: str) -> None:
    """Single-runtime, worker-lifetime supplement; never a durable inbox substitute."""
    _ACTIVE_MESSAGES[token] = (event_key, _minimal_message(msg, event_key))


def discard_active_pairing_message(token: object) -> None:
    _ACTIVE_MESSAGES.pop(token, None)


async def load_pending_pairing_messages(
    session: AsyncSession, msg: StandardMessage, *, event_key: str
) -> tuple[StandardMessage, ...]:
    """Only inspect this account/group/member's unfinished predecessor window.

    The caller must gate this query to a high-confidence ad text decision. An
    unknown account is never interpreted as permission to search other accounts.
    """
    if msg.provider != "onebot" or msg.sent_at is None:
        return ()
    key_parts = event_key.split(":")
    if (
        len(key_parts) != 3
        or key_parts[0] != "onebot"
        or not key_parts[1].isascii()
        or not key_parts[1].isdigit()
        or int(key_parts[1]) <= 0
        or key_parts[2] != msg.external_message_id
    ):
        return ()
    account_prefix = f"onebot:{key_parts[1]}:"
    active = {
        key: candidate
        for key, candidate in _ACTIVE_MESSAGES.values()
        if key != event_key
        and key.startswith(account_prefix)
        and candidate.provider == msg.provider
        and candidate.external_group_id == msg.external_group_id
        and candidate.external_user_id == msg.external_user_id
    }
    sent = msg.sent_at.replace(tzinfo=UTC) if msg.sent_at.tzinfo is None else msg.sent_at
    timestamp = sent.timestamp()
    valid = func.json_valid(InboxEvent.payload_json)
    user = case((valid, func.json_extract(InboxEvent.payload_json, "$.user_id")), else_=None)
    event_time = case((valid, func.json_extract(InboxEvent.payload_json, "$.time")), else_=None)
    time_kind = case((valid, func.json_type(InboxEvent.payload_json, "$.time")), else_=None)
    rows = (
        await session.execute(
            select(InboxEvent.event_key, InboxEvent.payload_json).where(
                InboxEvent.group_id == msg.external_group_id,
                InboxEvent.self_id == key_parts[1],
                InboxEvent.status.in_(("PENDING", "PROCESSING")),
                InboxEvent.event_key != event_key,
                cast(user, String) == msg.external_user_id,
                or_(
                    time_kind.is_(None),
                    time_kind.not_in(("integer", "real")),
                    (event_time >= timestamp - WALL_PAIR_WINDOW_SECONDS)
                    & (event_time <= timestamp),
                ),
            )
        )
    ).all()
    source = OneBotMessageSource()
    normalized: dict[str, StandardMessage] = dict(active)
    for key, raw in rows:
        try:
            parsed = source.parse_group_message(json.loads(raw))
        except (MessageParseError, ValueError, OverflowError, OSError):
            # Identity was already constrained by durable admission and the SQL
            # scope. Preserve unknown chronology as unknown, not as an exemption.
            normalized[key] = StandardMessage(
                message_id=key,
                external_message_id=key.rsplit(":", 1)[-1],
                provider="onebot",
                external_group_id=msg.external_group_id,
                external_user_id=msg.external_user_id,
                sender=Sender(),
                kind="unknown",
            )
            continue
        normalized[key] = _minimal_message(parsed, key)
    return tuple(normalized.values())
