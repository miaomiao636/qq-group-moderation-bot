"""T-102 契约测试：12 份真实脱敏样本必须全部稳定解析为统一消息契约。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from app.adapters.qq_official.parser import EventParseError, parse_group_message

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "qq_official"

EXPECTED_KINDS: dict[str, str] = {
    "group_message_create_text_at.json": "text",
    "group_message_create_text_plain.json": "text",
    "group_message_create_text_spam_1.json": "text",
    "group_message_create_text_spam_2.json": "text",
    "group_message_create_face.json": "text",
    "group_message_create_image.json": "image",
    "group_message_create_gif.json": "mixed",
    "group_message_create_voice.json": "voice",
    "group_message_create_video.json": "video",
    "group_message_create_file_pdf.json": "file",
    "group_message_create_forward_record.json": "forward_record",
    "group_message_create_share_card.json": "share_card",
}


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_fixture_inventory_matches_expectations() -> None:
    """样本文件与预期清单一致，防止漏测。"""
    actual = {p.name for p in FIXTURE_DIR.glob("*.json")}
    assert actual == set(EXPECTED_KINDS)


@pytest.mark.parametrize("name", sorted(EXPECTED_KINDS))
def test_fixture_parses_to_contract(name: str) -> None:
    payload = _load(name)["data"]
    msg = parse_group_message(payload)
    assert msg.message_id
    assert msg.group_openid
    assert msg.sender.member_openid
    assert msg.event_type == "GROUP_MESSAGE_CREATE"
    assert msg.kind == EXPECTED_KINDS[name]


def test_text_at_message_extracts_mentions() -> None:
    msg = parse_group_message(_load("group_message_create_text_at.json")["data"])
    assert msg.mentions, "@消息必须解析出被提及人"
    assert msg.face_count == 0


def test_face_message_counts_faces() -> None:
    msg = parse_group_message(_load("group_message_create_face.json")["data"])
    assert msg.face_count >= 1


def test_image_message_carries_attachment_metadata() -> None:
    msg = parse_group_message(_load("group_message_create_image.json")["data"])
    att = msg.attachments[0]
    assert att.content_type == "image/jpeg"
    assert att.size == 152130
    assert att.url


def test_voice_message_carries_official_asr_fields() -> None:
    msg = parse_group_message(_load("group_message_create_voice.json")["data"])
    att = msg.attachments[0]
    assert att.content_type == "voice"
    # D-013：语音附件必须保留官方转写字段（样本中已遮蔽也可能为空，仅验证字段存在）
    assert hasattr(att, "asr_refer_text")
    assert hasattr(att, "voice_wav_url")


def test_share_card_message_structured() -> None:
    msg = parse_group_message(_load("group_message_create_share_card.json")["data"])
    assert msg.share_card is not None
    assert msg.share_card.source


def test_sender_role_normalization() -> None:
    msg = parse_group_message(_load("group_message_create_text_plain.json")["data"])
    assert msg.sender.role in ("owner", "admin", "member", "unknown")


def test_missing_message_id_rejected() -> None:
    payload = _load("group_message_create_text_plain.json")["data"]
    payload["id"] = ""
    with pytest.raises(EventParseError):
        parse_group_message(payload)


def test_missing_group_openid_rejected() -> None:
    payload = _load("group_message_create_text_plain.json")["data"]
    payload["group_openid"] = ""
    with pytest.raises(EventParseError):
        parse_group_message(payload)


def test_missing_author_rejected() -> None:
    payload = _load("group_message_create_text_plain.json")["data"]
    payload["author"] = None
    with pytest.raises(EventParseError):
        parse_group_message(payload)


def test_sent_at_parsed() -> None:
    msg = parse_group_message(_load("group_message_create_text_plain.json")["data"])
    assert msg.sent_at is not None
