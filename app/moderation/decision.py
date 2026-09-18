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
# 负责人 2026-09-18（成员白名单 / 按 QQ 号）：命中成员的全部消息**完全放行**，
# 优先级最高（高于关键词白名单与保护角色分支）。负责人明确选择"不守 B-2 底线"——
# 诈骗/色情/暴力/刷屏同样放行；代价是白名单账号被盗或转手即等于完全敞开，
# 因此必须能一键停用、变更留审计，并把本标记纳入 POLICY_ALLOW_RULE_IDS 全链路保护
# （否则 R-113 式事故会再次发生：AI/动态规则/媒体层把它升级成真实撤回）。
ALLOWLIST_MEMBER_ALLOW_RULE_ID = "POLICY_ALLOWLIST_MEMBER_ALLOW"
# 负责人 2026-09-18：**图片含微信小程序二维码 → 一律通过**（不撤回、不转人工）。
# 由视觉模型的结构化字段 `has_miniprogram_code` 触发（见 ai.py）；
# **两个例外保留**：①**色情 / 暴力违禁品**仍按 B-2 处理（**诈骗不再例外**——负责人
# 2026-09-18 晚修订：带码的诈骗内容同样放行）；②本地硬证据
# （R001 黑名单词 / R003 联系方式 / R006 分享卡片 / DR_ 动态规则）不被图片外观覆盖。
# 刻意**不加入** `POLICY_ALLOW_RULE_IDS`：那是"全类别完全放行"集合，
# 本政策的例外条件必须在合并层判断，不能变成无条件豁免。
MINIPROGRAM_QR_ALLOW_RULE_ID = "POLICY_MINIPROGRAM_QR_ALLOW"
# 政策放行标记集合（**全类别**完全放行——负责人确认的独立政策）：
# D-031 办证 / D-032 群主管理员卡片 / D-037 成员白名单。全局白名单（D-033）
# **不在此列**：它只压制广告，严重类别证据必须照常升级 / 转人工（R-115 审查 P1）。
POLICY_ALLOW_RULE_IDS = frozenset(
    {
        CERTIFICATE_AD_ALLOW_RULE_ID,
        PROTECTED_CARD_ALLOW_RULE_ID,
        ALLOWLIST_MEMBER_ALLOW_RULE_ID,
    }
)
# 合并行/动态规则合并时的"完全放行、任何证据层不得升级"集合。
# 刻意**不含** D-031 办证：办证的动态规则豁免另有"仅办证类 DR 才豁免"的严格条件
# （非办证类显式 DR 照常生效，R-113 整改），不能放宽成无条件豁免。
FULL_ALLOW_NO_UPGRADE_RULE_IDS = frozenset(
    {
        PROTECTED_CARD_ALLOW_RULE_ID,
        ALLOWLIST_MEMBER_ALLOW_RULE_ID,
    }
)
# 结构性确定性规则（负责人 2026-09-18）：合并转发、群名片一律撤回。
# 保护角色（群主/管理员）与成员白名单命中时不撤回（分支顺序保证）。
#
# **编号必须避开内置规则的 `R0xx` 段**：内置规则已占用 R001–R007。新规则曾误用
# R007（与内置 `contextual_ad_terms` 撞号），而撞号会让**内置规则**被复核门当成
# "独立硬证据"，从而放宽自动处罚门槛（2026-09-18 事件：3 条判定被放宽；因当时处于
# 急停窗口，未造成真实动作）。改用具语义前缀的编号，从根上避免再次撞号。
FORWARD_RECORD_RECALL_RULE_ID = "R_FORWARD_RECORD"
GROUP_CARD_RECALL_RULE_ID = "R_GROUP_CARD"
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
