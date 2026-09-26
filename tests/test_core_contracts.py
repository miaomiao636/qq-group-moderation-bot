"""T-305 传输中立契约测试：身份同步、seam协议、官方Adapter兼容。"""

from __future__ import annotations

import ast
from pathlib import Path

from app.adapters.qq_official.actions import ActionResult as ActionResultFromAdapter
from app.adapters.qq_official.actions import OfficialActionAdapter
from app.adapters.qq_official.auth import TokenManager
from app.adapters.qq_official.contract import Sender, StandardMessage
from app.adapters.qq_official.parser import QQOfficialMessageSource
from app.core.contracts import (
    ActionResult,
    MessageSegment,
    MessageSource,
    ModerationActionClient,
)
from app.core.contracts import (
    StandardMessage as NeutralStandardMessage,
)
from app.moderation.decision import ModerationDecision


def test_legacy_construction_backfills_neutral_fields() -> None:
    msg = StandardMessage(
        message_id="MSG_1",
        group_openid="GROUP_OPENID_A",
        sender=Sender(member_openid="MEMBER_OPENID_A"),
    )
    assert msg.provider == "qq_official"
    assert msg.external_group_id == "GROUP_OPENID_A"
    assert msg.external_user_id == "MEMBER_OPENID_A"
    assert msg.external_message_id == "MSG_1"


def test_neutral_construction_backfills_legacy_mirrors() -> None:
    """provider=onebot 时镜像字段仅是读取兼容视图，不代表 OpenID 语义。"""
    msg = NeutralStandardMessage(
        message_id="910000001",
        provider="onebot",
        external_group_id="300000001",
        external_user_id="200000001",
        sender=Sender(member_openid="200000001"),
    )
    assert msg.provider == "onebot"
    assert msg.external_group_id == "300000001"
    assert msg.external_user_id == "200000001"
    # 镜像字段被填充以维持旧读取方兼容（expand 阶段），contract 阶段移除
    assert msg.group_openid == "300000001"
    assert msg.sender.member_openid == "200000001"
    # 镜像不改变通道语义：provider 仍为 onebot，绝不允许进入官方Adapter调用
    assert msg.provider != "qq_official"


def test_neutral_construction_with_sender_instance() -> None:
    msg = NeutralStandardMessage(
        message_id="M2",
        provider="onebot",
        external_group_id="300000002",
        sender=Sender(member_openid="200000009"),
    )
    assert msg.external_user_id == "200000009"


def test_explicit_values_not_overwritten() -> None:
    msg = NeutralStandardMessage(
        message_id="M3",
        group_openid="LEGACY_G",
        external_group_id="NEUTRAL_G",
        sender=Sender(member_openid="LEGACY_U"),
        external_user_id="NEUTRAL_U",
    )
    # 显式提供的两侧值都不被覆盖
    assert msg.group_openid == "LEGACY_G"
    assert msg.external_group_id == "NEUTRAL_G"
    assert msg.sender.member_openid == "LEGACY_U"
    assert msg.external_user_id == "NEUTRAL_U"


def test_decision_identity_sync() -> None:
    d1 = ModerationDecision(message_id="D1", group_openid="G1", sender_member_openid="U1")
    assert d1.provider == "qq_official"
    assert d1.external_group_id == "G1"
    assert d1.external_user_id == "U1"

    d2 = ModerationDecision(
        message_id="D2",
        provider="onebot",
        external_group_id="300000001",
        external_user_id="200000001",
    )
    assert d2.group_openid == "300000001"
    assert d2.sender_member_openid == "200000001"


def test_seam_protocols_are_runtime_checkable() -> None:
    source = QQOfficialMessageSource()
    assert isinstance(source, MessageSource)
    assert source.provider == "qq_official"


def test_official_action_adapter_satisfies_neutral_protocol() -> None:
    adapter = OfficialActionAdapter(
        TokenManager("APP", "SECRET"), api_base="https://api.bot.qq.com"
    )
    assert isinstance(adapter, ModerationActionClient)


def test_action_result_parity_between_core_and_adapter_reexport() -> None:
    assert ActionResult is ActionResultFromAdapter
    result = ActionResult(action="recall", ok=True, status_code=200, attempts=1)
    assert result.is_permission_error is False


def test_neutral_automatic_action_protocol_is_recall_only() -> None:
    """自动审核的动作 seam 仅包含撤回。"""
    protocol_methods = {name for name in ModerationActionClient.__protocol_attrs__}
    assert protocol_methods == {"recall"}


def test_neutral_message_has_transport_agnostic_segments() -> None:
    msg = NeutralStandardMessage(
        external_message_id="M_SEG",
        provider="onebot",
        external_group_id="300000001",
        external_user_id="200000001",
        sender=Sender(),
        segments=[
            MessageSegment(kind="text", text="hello"),
            MessageSegment(kind="image", attachment_index=0),
        ],
    )
    assert msg.message_id == "M_SEG"
    assert [segment.kind for segment in msg.segments] == ["text", "image"]


def test_core_business_modules_do_not_import_provider_adapters() -> None:
    root = Path(__file__).resolve().parents[1]
    files = [
        *sorted((root / "app" / "core").glob("*.py")),
        *sorted((root / "app" / "moderation").glob("*.py")),
        root / "app" / "cases" / "service.py",
        root / "app" / "actions" / "orchestrator.py",
        root / "app" / "runtime" / "pipeline.py",
    ]
    violations: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = ""
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                module = ",".join(alias.name for alias in node.names)
            if "app.adapters." in module:
                violations.append(f"{path.relative_to(root)}:{node.lineno}")
    assert violations == []
