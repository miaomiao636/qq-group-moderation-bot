"""T-306 OneBot 11 入站解析测试：事件→中立契约，未知段降级，身份不伪装。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from app.adapters.onebot.parser import OneBotMessageSource
from app.core.contracts import MessageParseError, MessageSource

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "onebot"

EXPECTED: dict[str, dict[str, Any]] = {
    "group_message_text.json": {"kind": "text"},
    "group_message_image.json": {"kind": "image"},
    "group_message_gif.json": {"kind": "gif"},
    "group_message_voice.json": {"kind": "voice"},
    "group_message_video.json": {"kind": "video"},
    "group_message_file.json": {"kind": "file"},
    "group_message_reply.json": {"kind": "text"},
    "group_message_forward.json": {"kind": "forward_record"},
    "group_message_card.json": {"kind": "share_card"},
    "group_message_unknown_segment.json": {"kind": "text"},
}


def _load_event(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))["event"]


def test_source_satisfies_message_source_protocol() -> None:
    assert isinstance(OneBotMessageSource(), MessageSource)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_fixture_parses_to_neutral_contract(name: str) -> None:
    msg = OneBotMessageSource().parse_group_message(_load_event(name))
    expected = EXPECTED[name]
    # 中立身份：数字ID字符串 + onebot provider
    assert msg.provider == "onebot"
    assert msg.external_group_id == str(_load_event(name)["group_id"])
    assert msg.external_user_id == str(_load_event(name)["user_id"])
    assert msg.external_message_id == str(_load_event(name)["message_id"])
    assert msg.message_id == msg.external_message_id
    # 镜像视图同步填充（expand阶段兼容），但不改变provider语义
    assert msg.group_openid == msg.external_group_id
    assert msg.sender.member_openid == msg.external_user_id
    assert msg.kind == expected["kind"]
    assert msg.event_type == "GROUP_MESSAGE_CREATE"
    assert msg.sent_at is not None


def test_reply_keeps_referenced_metadata() -> None:
    msg = OneBotMessageSource().parse_group_message(_load_event("group_message_reply.json"))
    assert "[回复消息:909000999]" in msg.text
    assert msg.mentions == ["200000008"]
    assert msg.sender.role == "admin"
    assert msg.sender.username == "SANITIZED_CARD"


def test_forward_record_marked_for_human_review() -> None:
    msg = OneBotMessageSource().parse_group_message(_load_event("group_message_forward.json"))
    assert msg.kind == "forward_record"
    assert any(s.kind == "forward_record" for s in msg.segments)
    assert "[合并转发]" in msg.text


def test_card_extracts_neutral_share_info() -> None:
    msg = OneBotMessageSource().parse_group_message(_load_event("group_message_card.json"))
    assert msg.share_card is not None
    assert msg.share_card.source == "SANITIZED_APP"
    assert "SANITIZED_TITLE" in msg.share_card.prompt


def test_unknown_segment_keeps_metadata_and_flags_review() -> None:
    msg = OneBotMessageSource().parse_group_message(
        _load_event("group_message_unknown_segment.json")
    )
    unknown = [s for s in msg.segments if s.kind == "unknown"]
    assert len(unknown) == 1
    # 保留必要元数据：段类型名 + 数据键名
    assert unknown[0].text.startswith("weather[")
    assert "city" in unknown[0].text
    assert "[未知消息段:weather]" in msg.text
    # 未知段绝不直接判定为正常：由流水线守卫强制转人工（见 test_unknown_segment_degrades）


def test_voice_and_video_attachment_metadata() -> None:
    voice = OneBotMessageSource().parse_group_message(_load_event("group_message_voice.json"))
    assert voice.attachments[0].content_type == "voice"
    assert voice.attachments[0].filename == "voice_fixture.amr"
    video = OneBotMessageSource().parse_group_message(_load_event("group_message_video.json"))
    assert video.attachments[0].content_type == "video/mp4"


def test_downloaded_local_filenames_injected_by_sequence() -> None:
    payload = _load_event("group_message_image.json")
    payload["_downloaded"] = ["abc123_0.jpg"]
    msg = OneBotMessageSource().parse_group_message(payload)
    assert msg.attachments[0].filename == "abc123_0.jpg"


def test_role_mapping_owner_admin_member() -> None:
    for raw_role, expected in (
        ("owner", "owner"),
        ("administrator", "admin"),
        ("member", "member"),
    ):
        payload = _load_event("group_message_text.json")
        payload["sender"]["role"] = raw_role
        msg = OneBotMessageSource().parse_group_message(payload)
        assert msg.sender.role == expected


def test_anonymous_message_marked() -> None:
    payload = _load_event("group_message_text.json")
    payload["anonymous"] = {"id": "anon", "name": "SANITIZED_ANON", "flag": "f"}
    msg = OneBotMessageSource().parse_group_message(payload)
    assert "[匿名消息]" in msg.text


def test_string_cq_form_degrades_to_human_review() -> None:
    """CQ码字符串形态无法可靠还原媒体结构，必须保留并降级人工。"""
    payload = _load_event("group_message_image.json")
    payload["message"] = "[CQ:image,file=abc.jpg,url=https://invalid.example/a.jpg]"
    msg = OneBotMessageSource().parse_group_message(payload)
    assert any(s.kind == "unknown" for s in msg.segments)


def test_downloaded_files_survive_empty_list() -> None:
    payload = _load_event("group_message_image.json")
    payload["_downloaded"] = [""]  # 下载失败：保持原始文件名，filename 由流水线判空
    msg = OneBotMessageSource().parse_group_message(payload)
    assert msg.attachments[0].filename == "onebot_image.fixture.jpg"


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda p: p.__setitem__("post_type", "meta_event"), "post_type"),
        (lambda p: p.__setitem__("message_type", "private"), "message_type"),
        (lambda p: p.__setitem__("message_id", None), "message_id"),
        (lambda p: p.__setitem__("group_id", None), "group_id"),
        (lambda p: p.__setitem__("user_id", None), "user_id"),
    ],
)
def test_invalid_structures_rejected(mutate: Any, match: str) -> None:
    payload = _load_event("group_message_text.json")
    mutate(payload)
    with pytest.raises(MessageParseError, match=match):
        OneBotMessageSource().parse_group_message(payload)


def test_non_dict_payload_rejected() -> None:
    with pytest.raises(MessageParseError):
        OneBotMessageSource().parse_group_message(["not", "a", "dict"])  # type: ignore[arg-type]
