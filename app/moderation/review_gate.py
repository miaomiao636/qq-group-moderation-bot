"""复核门与成本控制（T-203，R-102 整改）。

R-102-7 复核门必须使用与主通道**不同的硬证据**判定，不得重复调用同一规则冒充独立复核；
**弱信号不算硬证据**。硬证据 = 显式黑名单命中（R001）或联系方式提取（R003）。
仅软信号（R002）凑分达到阈值的高置信，复核门必须拦截转人工。

成本控制：全部本地计算，费用恒为0；CostLedger 记录调用次数供日报。
CircuitBreaker：媒体/模型类外部依赖连续失败时熔断，冷却后半开探测。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.core.contracts import StandardMessage
from app.moderation.decision import (
    FORWARD_RECORD_RECALL_RULE_ID,
    GROUP_CARD_RECALL_RULE_ID,
    ModerationDecision,
)

AUTO_PUNISH_THRESHOLD = 0.90
# 内置独立硬证据规则：黑名单词 / 联系方式 / 分享卡片。
_BUILTIN_HARD_EVIDENCE_RULES = frozenset({"R001", "R003", "R006"})
# 结构性确定性规则（负责人 2026-09-18）：合并转发 / 群名片——消息结构本身就是证据，
# 不依赖文本软信号，必须计入硬证据，否则复核门会把"一律撤回"重新降级为转人工。
#
# **只允许加入 decision.py 导出的结构性规则常量**。内置规则的 `R0xx` 编号段绝不能
# 出现在这里：曾把结构性规则误编为 R007，与内置 `contextual_ad_terms` 撞号，导致
# 复核门把该内置规则当成独立硬证据、放宽了自动处罚门槛（2026-09-18 事件）。
_HARD_EVIDENCE_RULES = _BUILTIN_HARD_EVIDENCE_RULES | frozenset(
    {FORWARD_RECORD_RECALL_RULE_ID, GROUP_CARD_RECALL_RULE_ID}
)


def _is_hard_evidence(rule_id: str) -> bool:
    """动态禁止规则是管理员显式发布的结构化规则，也算硬证据。"""
    return rule_id in _HARD_EVIDENCE_RULES or rule_id.startswith("DR_")


class ReviewGate:
    """独立硬证据复核门（R-102-7 重做）。"""

    def __init__(self) -> None:
        self._rejection_reason = "复核门拦截：缺少独立硬证据（黑名单/联系方式），转人工"

    def review(self, msg: StandardMessage, primary: ModerationDecision) -> ModerationDecision:
        """对主通道决策执行独立复核，返回最终决策。"""
        if primary.verdict != "violation_high":
            return primary
        if primary.is_protected_sender or msg.sender.role in ("owner", "admin"):
            return primary.model_copy(
                update={
                    "verdict": "record_only",
                    "recommended_actions": [],
                    "reason": "复核拦截：保护角色不自动处罚",
                }
            )

        # R-102-7：不重复调用主规则，从主决策的命中里判断是否存在硬证据
        has_hard_evidence = any(_is_hard_evidence(hit.rule_id) for hit in primary.rule_hits)
        if has_hard_evidence:
            return primary
        # 主决策仅靠弱信号凑分 → 必须拦截，软信号不算独立硬证据
        return primary.model_copy(
            update={
                "verdict": "record_only",
                "recommended_actions": [],
                "confidence": min(primary.confidence, 0.85),
                "reason": primary.reason + "；" + self._rejection_reason,
            }
        )


@dataclass
class UsageRecord:
    component: str
    calls: int = 0
    failures: int = 0
    cost: float = 0.0


class CostLedger:
    """调用与费用台账（零预算方案下 cost 恒 0）。"""

    def __init__(self) -> None:
        self._records: dict[str, UsageRecord] = {}

    def record(self, component: str, *, failure: bool = False, cost: float = 0.0) -> None:
        rec = self._records.setdefault(component, UsageRecord(component))
        rec.calls += 1
        rec.failures += 1 if failure else 0
        rec.cost += cost

    def report(self) -> dict[str, dict[str, float | int]]:
        return {
            name: {"calls": r.calls, "failures": r.failures, "cost": r.cost}
            for name, r in sorted(self._records.items())
        }

    def total_cost(self) -> float:
        return sum(r.cost for r in self._records.values())

    def reset(self) -> None:
        self._records.clear()


class CircuitBreaker:
    """连续失败熔断器：达到阈值后 OPEN，冷却后半开探测，成功即闭合。"""

    def __init__(self, failure_threshold: int = 5, cooldown_seconds: float = 60.0) -> None:
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._failures = 0
        self._opened_at: float | None = None
        self.state: str = "CLOSED"

    def allow(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if self.state == "OPEN":
            if self._opened_at is not None and now - self._opened_at >= self._cooldown:
                self.state = "HALF_OPEN"
                return True
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self.state = "CLOSED"

    def record_failure(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._failures += 1
        if self._failures >= self._threshold or self.state == "HALF_OPEN":
            self.state = "OPEN"
            self._opened_at = now
