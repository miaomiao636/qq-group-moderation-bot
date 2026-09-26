"""CAMPUS-TEMPLATE-20260922: visual brand/template evidence, not mini-app identity.

Unknown/legacy metadata never grants immunity. This is recognition of an
approved brand/template, not authentication or proof against forgery.
"""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

CAMPUS_WALL_SOURCE = "万能校园墙"
CAMPUS_EVIDENCE_PREFIX = "校园墙白名单|文案:"


class CampusShareCard(BaseModel):
    """Visual observations of an owner-approved template, never AppID authentication."""

    model_config = ConfigDict(extra="forbid")

    template_id: Literal["approved_share_card_v1"]
    status: Literal["matched", "uncertain", "not_matched"]
    logo_matches: StrictBool
    footer_text: str = Field(max_length=200, strict=True)
    layout_matches: StrictBool
    footer_miniprogram_code: StrictBool


def share_card_matches(card: Any) -> bool:
    """All observations must describe one footer in one attachment."""
    if not isinstance(card, dict):
        return False
    text = card.get("footer_text")
    # Only punctuation/whitespace varies. A short generic phrase is insufficient.
    footer = re.sub(r"[\s，,！!。.~～]+", "", text) if isinstance(text, str) else ""
    return (
        card.get("template_id") == "approved_share_card_v1"
        and card.get("status") == "matched"
        and card.get("logo_matches") is True
        and card.get("layout_matches") is True
        and card.get("footer_miniprogram_code") is True
        and footer == "我正在看这个觉得不错长按识别小程序一起看吧"
    )


def uncertain_campus_template(result: dict[str, Any]) -> bool:
    card = result.get("campus_share_card")
    return isinstance(card, dict) and (
        card.get("status") == "uncertain"
        or (
            card.get("status") == "matched"
            and (not share_card_matches(card) or result.get("has_miniprogram_code") is not True)
        )
    )


def confirmed_campus_source(result: dict[str, Any]) -> bool:
    """Confirm visible brand OR approved share template in the same vision result."""
    if result.get("source") != "vision" or uncertain_campus_template(result):
        return False
    if share_card_matches(result.get("campus_share_card")):
        return True
    evidence = result.get("evidence")
    return (
        result.get("source") == "vision"
        and result.get("campus_wall_source") == CAMPUS_WALL_SOURCE
        and isinstance(evidence, str)
        and evidence.strip().startswith(CAMPUS_EVIDENCE_PREFIX)
        and CAMPUS_WALL_SOURCE in evidence.strip()[len(CAMPUS_EVIDENCE_PREFIX) :]
    )


def confirmed_campus_qr(result: dict[str, Any]) -> bool:
    """Bind the source QR to its footer, not an unrelated code in the same image."""
    if not confirmed_campus_source(result):
        return False
    return result.get("has_miniprogram_code") is True
