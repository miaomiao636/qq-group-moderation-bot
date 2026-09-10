"""Minimal provider-independent delivery seam; never carries raw group content."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol

DeliveryStatus = Literal["PENDING", "SENDING", "SENT", "FAILED", "UNKNOWN", "SKIPPED"]
ResultStatus = Literal["SENT", "FAILED", "UNKNOWN", "SKIPPED"]
Severity = Literal["page", "ticket"]


@dataclass(frozen=True)
class DeliveryMessage:
    """Only collector-controlled summaries may cross this notification boundary."""

    notice_id: int
    subject: str
    body: str


@dataclass(frozen=True)
class DeliveryResult:
    """FAILED/retryable means positively known not to have been sent."""

    status: ResultStatus
    error_code: str = ""
    retryable: bool = False

    def __post_init__(self) -> None:
        if self.status not in ("SENT", "FAILED", "UNKNOWN", "SKIPPED"):
            raise ValueError("unsupported delivery status")
        if self.retryable and self.status != "FAILED":
            raise ValueError("retryable is only valid for confirmed FAILED deliveries")
        if self.error_code and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.error_code):
            raise ValueError("error_code must be a fixed safe identifier")


class NotificationSender(Protocol):
    async def send(self, message: DeliveryMessage, audience: str = "primary") -> DeliveryResult: ...
