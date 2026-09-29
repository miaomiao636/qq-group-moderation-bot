"""Validate OneBot notices before crossing into neutral action evidence."""

from datetime import UTC, datetime
from typing import Any

from app.actions.recall_confirmation import RecallNotice


def parse_recall_notice(event: dict[str, Any], *, expected_self_id: str) -> RecallNotice | None:
    if event.get("post_type") != "notice" or event.get("notice_type") != "group_recall":
        return None
    values: dict[str, str] = {}
    for key in ("self_id", "group_id", "user_id", "message_id", "operator_id"):
        value = event.get(key)
        if type(value) is not int or not -(2**63) <= value < 2**63:
            return None
        if key != "message_id" and value <= 0:
            return None
        values[key] = str(value)
    raw_time = event.get("time")
    if type(raw_time) is not int or not 0 < raw_time < 253402300800:
        return None
    if values["self_id"] != expected_self_id:
        return None
    return RecallNotice(
        account_id=values["self_id"],
        group_id=values["group_id"],
        user_id=values["user_id"],
        message_id=values["message_id"],
        operator_id=values["operator_id"],
        occurred_at=datetime.fromtimestamp(raw_time, UTC),
    )
