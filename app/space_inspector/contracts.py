"""Small contracts shared by the directory, browser, and local task store."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Keep the two observed system templates paired; do not normalize arbitrary text.
NOTICE = "您访问的空间存在违规信息,已被多名用户举报,暂时无法查看！"
RESTRICTION_TEMPLATES = (
    ("温馨提示:", NOTICE, "返回我的空间"),
    ("温馨提示：", "您访问的空间存在违规信息,已被多名用户举报,暂时无法查看。", "返回我的空间"),
)
RESTRICTION_NOTICES = tuple(template[1] for template in RESTRICTION_TEMPLATES)
NONFRIEND_NOTICE = "很抱歉,QQ空间相关功能升级维护,暂不支持非好友访问,敬请理解！"


class InspectionError(RuntimeError):
    """A safe, user-facing failure. Never include remote payloads or credentials."""


class PlatformAccessBlocked(InspectionError):
    """The platform blocked the visit; require manual browser-state confirmation."""


def numeric_id(value: object) -> str:
    if type(value) not in (str, int):
        raise InspectionError("群或成员编号格式无效。")
    result = str(value)
    if not re.fullmatch(r"[1-9][0-9]{4,11}", result):
        raise InspectionError("群或成员编号格式无效。")
    return result


@dataclass(frozen=True)
class Group:
    group_id: str
    name: str
    member_count: int


@dataclass(frozen=True)
class Observation:
    qq: str
    status: str
    reason: str
    checked_at: str
    evidence: dict[str, object]


RESTRICTED = "RESTRICTION_OBSERVED"
UNCONFIRMED = "UNCONFIRMED"
BLOCKED = "BLOCKED"
PENDING = "PENDING"
LABELS = {
    RESTRICTED: "观察到 QQ 空间违规限制提示",
    UNCONFIRMED: "未观察到该提示，待确认",
    BLOCKED: "访问未完成，待确认",
    PENDING: "尚未检查",
}

REASONS = {
    "qzone_restriction_notice_observed": "页面明确显示空间违规限制提示",
    "no_restriction_notice_observed": "资料页已打开，未观察到该提示；仍需人工确认",
    "login_required": "需要重新登录 QQ 空间",
    "navigation_failed": "页面访问未完成，请检查网络或浏览器",
    "viewer_missing_or_changed": "登录账号未确认或已切换",
    "page_incomplete": "页面尚未完整加载",
    "unexpected_location": "页面地址与待检查成员不一致",
    "unrecognized_page": "页面格式无法确认，请人工查看",
    "space_access_permission_required": "主人设置了访问权限，待确认；继续检查其他成员",
    "space_not_opened": "对方未开通空间，待确认；继续检查其他成员",
    "space_nonfriend_access_unavailable": "空间暂不支持非好友访问，待确认；继续检查其他成员",
    "platform_access_blocked": "QQ 空间访问被腾讯安全防护拦截；请停止重试，待正常访问恢复后再确认",
}
