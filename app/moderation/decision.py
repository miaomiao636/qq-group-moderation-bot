"""审核决策契约（T-103）。

硬约束：决策结构中不存在 kick / 踢人 动作（D-001）。允许的动作仅限
recall / mute / warn，且仅在 verdict=violation_high 时给出建议；
record_only 表示中等风险只记录转人工，allow 表示放行。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

Verdict = Literal["allow", "record_only", "violation_high"]
RecommendedAction = Literal["recall", "mute", "warn"]
Category = Literal["ad", "fraud", "porn", "violence", "flood", "other", None]

# Deterministic local policy marker, never inferred from an AI explanation.
CERTIFICATE_AD_ALLOW_RULE_ID = "POLICY_CERTIFICATE_AD_ALLOW"
# 负责人 2026-09-16 口径：群主/管理员分享的卡片完全放行（不处罚、不转人工）；
# AI/动态规则/媒体层不得升级（与办证豁免同样的全链路保护机制）。
PROTECTED_CARD_ALLOW_RULE_ID = "POLICY_PROTECTED_CARD_ALLOW"
# 负责人 2026-09-16（后台可维护的全局白名单）：命中且非严重类别 → 放行。
# **仅豁免广告/无信号类别**；诈骗/色情/暴力/刷屏不豁免（B-2 底线）。
# R-115 W01 整改：白名单不属于"全类别完全放行"政策——后续证据层必须
# 逐次复核类别（见 `is_allowlist_ad_only_hit`），非广告回到既有判定门槛。
ALLOWLIST_ALLOW_RULE_ID = "POLICY_ALLOWLIST_ALLOW"
# 政策放行标记集合（**全类别**完全放行——负责人确认的独立政策）：
# 仅 D-031 办证 / D-032 群主管理员卡片。全局白名单（D-033）**不在此列**：
# 它只压制广告，严重类别证据必须照常升级 / 转人工（R-115 审查 P1）。
POLICY_ALLOW_RULE_IDS = frozenset(
    {
        CERTIFICATE_AD_ALLOW_RULE_ID,
        PROTECTED_CARD_ALLOW_RULE_ID,
    }
)
# 白名单非豁免类别（R-115 W01）：任一出现即视为"非广告证据"，不得沿用放行。
ALLOWLIST_NON_EXEMPT_CATEGORIES = frozenset({"fraud", "porn", "violence", "flood"})


class RuleHit(BaseModel):
    """单条规则命中记录。"""

    rule_id: str
    rule_name: str
    category: Category = None
    confidence_delta: float = 0.0
    evidence_masked: str = ""  # 证据摘要，敏感信息已遮蔽


class ModerationDecision(BaseModel):
    """文字审核决策结构。

    T-305：新增传输中立身份字段 ``provider``/``external_group_id``/
    ``external_user_id``；旧字段 ``group_openid``/``sender_member_openid``
    保留为镜像视图并在构造时双向同步（expand 阶段），后续 contract 阶段移除。
    """

    message_id: str
    group_openid: str = ""
    sender_member_openid: str = ""
    provider: str = "qq_official"
    external_group_id: str = ""
    external_user_id: str = ""
    sender_role: str = "member"
    verdict: Verdict = "allow"
    category: Category = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rule_hits: list[RuleHit] = Field(default_factory=list)
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)
    reason: str = ""
    is_protected_sender: bool = False  # 群主/管理员：只记录不处罚

    @model_validator(mode="before")
    @classmethod
    def _sync_identity(cls, data: Any) -> Any:
        """构造前双向同步旧命名镜像与中立身份字段（只补空，不覆盖显式值）。"""
        if not isinstance(data, dict):
            return data
        group_openid = data.get("group_openid")
        external_group_id = data.get("external_group_id")
        if external_group_id and not group_openid:
            data["group_openid"] = external_group_id
        elif group_openid and not external_group_id:
            data["external_group_id"] = group_openid
        sender_member_openid = data.get("sender_member_openid")
        external_user_id = data.get("external_user_id")
        if external_user_id and not sender_member_openid:
            data["sender_member_openid"] = external_user_id
        elif sender_member_openid and not external_user_id:
            data["external_user_id"] = sender_member_openid
        return data
