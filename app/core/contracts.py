"""传输中立的消息、身份与动作契约（T-305）。

设计要点（D-019 / T-305）：
- 统一身份键为 ``provider + external_group_id + external_user_id + external_message_id``；
  对 QQ 官方 Adapter 即 ``group_openid``/``member_openid``，对 OneBot Adapter 即数字
  ``group_id``/``user_id``。两者没有公开保证的转换，本模块不做任何跨通道映射。
- ``StandardMessage`` 保留 ``group_openid``/``sender.member_openid`` 等旧字段作为
  **已弃用的镜像视图**（expand/migrate/contract 的 expand 阶段）：构造时与中立字段
  双向同步，保证旧调用方（含官方 Adapter 与既有测试）继续可用；contract 阶段将在
  独立任务中移除镜像字段。
- 镜像字段不改变 ``provider`` 语义：``provider="onebot"`` 的消息即使镜像字段里是
  数字 ID 字符串，也绝不允许进入 QQ 官方 Adapter 的 API 调用（动作路由按群
  显式选择 provider，见 ``app/core/routing.py``）。**不得把 OneBot 数字 ID 伪装成
  OpenID**——镜像只是过渡期的读取兼容，不代表 OpenID 语义。
- 本模块不得在顶层导入任何供应商 Adapter；供应商数据结构只能在各自 Adapter 内
  转换为这里的契约。

暴露的小而稳定接口（seam）：
- ``MessageSource``：入站 Adapter 实现，把传输层原始事件转换为 ``StandardMessage``。
- ``ModerationActionClient``：出站动作 Adapter 实现，按中立 ID 执行撤回/禁言/警告。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, model_validator

# 传输通道提供方：官方机器人 / OneBot(NapCat)。
# 审核核心只识别该字面量集合，不感知任何一方的原始事件结构。
Provider = Literal["qq_official", "onebot"]
PROVIDERS: tuple[str, ...] = ("qq_official", "onebot")

MessageKind = Literal[
    "text",
    "image",
    "gif",
    "voice",
    "video",
    "file",
    "forward_record",
    "share_card",
    "mixed",
    "unknown",
]

SenderRole = Literal["owner", "admin", "member", "unknown"]

# 中立动作名（动作结构中永不存在 kick；unmute 供运维解除禁言）。
ActionName = Literal["recall", "mute", "unmute", "warn"]


class Sender(BaseModel):
    """消息发送者。``member_openid`` 为旧官方命名镜像，见模块 docstring。"""

    member_openid: str
    union_openid: str = ""
    username: str = ""
    role: SenderRole = "member"
    is_bot: bool = False


class Attachment(BaseModel):
    """附件元数据（URL 仅作即时下载用途，不含任何供应商专有结构）。"""

    content_type: str = ""
    filename: str = ""
    size: int | None = None
    url: str = ""
    width: int | None = None
    height: int | None = None
    asr_refer_text: str = ""
    voice_wav_url: str = ""


class ShareCardInfo(BaseModel):
    """分享卡片摘要。"""

    source: str = ""
    title: str = ""
    prompt: str = ""
    tag: str = ""
    preview_url: str = ""
    source_logo_url: str = ""


class StandardMessage(BaseModel):
    """传输中立的统一消息契约。

    中立身份字段（权威）：
    - ``provider``：消息来源通道；
    - ``external_group_id``：供应商侧群标识（官方=group_openid，OneBot=数字群号字符串）；
    - ``external_user_id``：供应商侧成员标识（官方=member_openid，OneBot=数字QQ字符串）；
    - ``external_message_id``：供应商侧消息标识。

    旧字段 ``group_openid``/``group_id`` 与 ``sender.member_openid`` 在 expand 阶段
    保留并与中立字段双向同步（镜像视图），既有官方 Adapter、审核、案件、报告与
    测试无需改动即可继续工作。镜像字段不得被解释为跨通道身份等价。
    """

    message_id: str = Field(min_length=1)
    event_type: str = "GROUP_MESSAGE_CREATE"
    group_openid: str = Field(min_length=1)  # 已弃用镜像：= external_group_id
    group_id: str = ""  # 官方事件的不透明群串（D-012），非数字群号
    provider: Provider = "qq_official"
    external_group_id: str = ""
    external_user_id: str = ""
    external_message_id: str = ""
    sender: Sender
    sent_at: datetime | None = None
    received_at: datetime | None = None
    kind: MessageKind = "unknown"
    text: str = ""
    mentions: list[str] = Field(default_factory=list)
    face_count: int = 0
    attachments: list[Attachment] = Field(default_factory=list)
    share_card: ShareCardInfo | None = None

    @model_validator(mode="before")
    @classmethod
    def _sync_legacy_identity(cls, data: Any) -> Any:
        """构造前双向同步旧命名镜像与中立身份字段（只补空，不覆盖显式值）。"""
        if not isinstance(data, dict):
            return data
        _fill_both(data, "group_openid", "external_group_id")
        _fill_both(data, "message_id", "external_message_id")
        sender = data.get("sender")
        if isinstance(sender, dict):
            member_openid = sender.get("member_openid")
            external_user_id = data.get("external_user_id")
            if external_user_id and not member_openid:
                sender["member_openid"] = external_user_id
            elif member_openid and not external_user_id:
                data["external_user_id"] = member_openid
        return data

    @model_validator(mode="after")
    def _fill_user_id_from_sender(self) -> StandardMessage:
        """sender 以模型实例传入时，从其镜像字段补齐中立用户 ID。"""
        if not self.external_user_id:
            self.external_user_id = self.sender.member_openid
        return self

    @property
    def has_media(self) -> bool:
        return bool(self.attachments)


def _fill_both(data: dict[str, Any], legacy_key: str, neutral_key: str) -> None:
    """在旧命名与中立命名之间互相补空（不覆盖任一侧显式提供的值）。"""
    legacy = data.get(legacy_key)
    neutral = data.get(neutral_key)
    if neutral and not legacy:
        data[legacy_key] = neutral
    elif legacy and not neutral:
        data[neutral_key] = legacy


class ActionResult(BaseModel):
    """动作执行结果，可审计（原官方 actions 定义上移至中立契约）。"""

    action: ActionName
    ok: bool
    status_code: int | None = None
    err_code: int | None = None
    err_message: str = ""
    attempts: int = 0

    @property
    def is_permission_error(self) -> bool:
        """权限不足类错误（官方实测：撤回40062003；禁言保护角色40103004）。"""
        return self.err_code in (40062003, 40103004)


@runtime_checkable
class MessageSource(Protocol):
    """入站 Adapter seam：把传输层原始事件转换为中立消息。

    官方 Adapter 与未来的 NapCat/OneBot Adapter（T-306）都实现本接口；
    审核链路只依赖该协议，不导入任何 Adapter 模块。
    """

    provider: Provider

    def parse_group_message(self, payload: Mapping[str, Any], /) -> StandardMessage:
        """把原始事件载荷解析为 ``StandardMessage``；契约错误必须抛出异常。"""


@runtime_checkable
class ModerationActionClient(Protocol):
    """出站动作 Adapter seam：按中立 ID 执行撤回/禁言/警告。

    位置限定参数（``/``）使实现方自由命名形参；协议不含踢人，任何实现
    都不得添加自动踢人路径（踢人必须人工审批，T-304）。
    """

    async def recall(
        self, external_group_id: str, external_message_id: str, /, *, actor: str = "system"
    ) -> ActionResult:
        """撤回一条消息。"""
        ...

    async def mute(
        self,
        external_group_id: str,
        external_user_id: str,
        seconds: int,
        /,
        *,
        actor: str = "system",
    ) -> ActionResult:
        """禁言一名成员。"""
        ...

    async def warn(
        self,
        external_group_id: str,
        reply_to_message_id: str,
        text: str,
        /,
        *,
        msg_seq: int = 1,
        actor: str = "system",
    ) -> ActionResult:
        """发送一次被动警告回复。"""
        ...
