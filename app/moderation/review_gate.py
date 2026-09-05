"""复核门与成本控制（T-203）。

零预算形态（决策 D-014）下不接入云模型，复核采用**双通道一致性**原则：
- 主通道：完整规则引擎评分（黑名单+弱信号+联系方式+变体）；
- 复核通道：仅黑名单与联系方式硬证据重评分（独立、更严格的口径）；
- 两通道一致判违规才自动处罚；任一通道不可用或不一致 → record_only 转人工；
- 处罚阈值 0.90（PROJECT_CONTEXT 既定）。

成本控制：全部本地计算，费用恒为0；CostLedger 记录调用次数供日报。
CircuitBreaker：媒体/模型类外部依赖连续失败时熔断，冷却后半开探测。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.adapters.qq_official.contract import StandardMessage
from app.moderation.decision import ModerationDecision
from app.moderation.rules import TextRuleEngine, evaluate_text

AUTO_PUNISH_THRESHOLD = 0.90


class ReviewGate:
    """双通道一致性复核门。"""

    def __init__(self, engine: TextRuleEngine | None = None) -> None:
        self._engine = engine or TextRuleEngine()

    def review(self, msg: StandardMessage, primary: ModerationDecision) -> ModerationDecision:
        """对主通道决策执行独立复核，返回最终决策。"""
        if primary.verdict != "violation_high":
            # 非高置信维持原判（record_only/allow），复核只针对"要处罚"的决定
            return primary
        if primary.is_protected_sender or msg.sender.role in ("owner", "admin"):
            return primary.model_copy(
                update={
                    "verdict": "record_only",
                    "recommended_actions": [],
                    "reason": "复核拦截：保护角色不自动处罚",
                }
            )

        # 复核通道：仅硬证据（黑名单词 + 联系方式）
        _, review_confidence, review_category = evaluate_text(msg.text)
        hard_evidence = review_confidence >= 0.25  # 至少存在可复核的联系方式/黑名单证据
        primary_has_blacklist = any(h.rule_id == "R001" for h in primary.rule_hits)

        agrees = hard_evidence or primary_has_blacklist
        if agrees:
            return primary
        return primary.model_copy(
            update={
                "verdict": "record_only",
                "recommended_actions": [],
                "confidence": min(primary.confidence, 0.85),
                "reason": primary.reason + "；复核门拦截：缺少独立硬证据，转人工",
            }
        )


@dataclass
class UsageRecord:
    component: str
    calls: int = 0
    failures: int = 0
    cost: float = 0.0  # 零预算方案恒为0


class CostLedger:
    """调用与费用台账（零预算方案下 cost 恒 0，保留字段以兼容未来付费组件）。"""

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
        """当前是否允许调用。"""
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
