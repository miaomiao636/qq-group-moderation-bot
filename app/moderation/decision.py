"""审核决策契约（T-103）。

硬约束：决策结构中不存在 kick / 踢人 动作（D-001）。允许的动作仅限
recall / mute / warn，且仅在 verdict=violation_high 时给出建议；
record_only 表示中等风险只记录转人工，allow 表示放行。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Verdict = Literal["allow", "record_only", "violation_high"]
RecommendedAction = Literal["recall", "mute", "warn"]
Category = Literal["ad", "fraud", "porn", "violence", "flood", "other", None]


class RuleHit(BaseModel):
    """单条规则命中记录。"""

    rule_id: str
    rule_name: str
    category: Category = None
    confidence_delta: float = 0.0
    evidence_masked: str = ""  # 证据摘要，敏感信息已遮蔽


class ModerationDecision(BaseModel):
    """文字审核决策结构。"""

    message_id: str
    group_openid: str
    sender_member_openid: str
    sender_role: str = "member"
    verdict: Verdict = "allow"
    category: Category = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rule_hits: list[RuleHit] = Field(default_factory=list)
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)
    reason: str = ""
    is_protected_sender: bool = False  # 群主/管理员：只记录不处罚
