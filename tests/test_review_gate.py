"""T-203 复核门、成本台账、熔断器测试。"""

from __future__ import annotations

from app.adapters.qq_official.contract import Sender, StandardMessage
from app.moderation.decision import ModerationDecision
from app.moderation.review_gate import CircuitBreaker, CostLedger, ReviewGate

GROUP = "G_REVIEW"
MEMBER = "M_REVIEW"


def make_msg(text: str) -> StandardMessage:
    return StandardMessage(
        message_id="RV_MSG",
        group_openid=GROUP,
        sender=Sender(member_openid=MEMBER),
        text=text,
    )


def primary_high(text: str, hits: list[str] | None = None) -> ModerationDecision:
    from app.moderation.decision import RuleHit

    return ModerationDecision(
        message_id="RV_MSG",
        group_openid=GROUP,
        sender_member_openid=MEMBER,
        verdict="violation_high",
        category="ad",
        confidence=0.95,
        rule_hits=[
            RuleHit(
                rule_id="R002",
                rule_name="soft_signals",
                category="ad",
                confidence_delta=0.95,
                evidence_masked=",".join(hits or []),
            )
        ],
        reason="主通道判定",
        recommended_actions=["recall", "mute", "warn"],
    )


def test_review_gate_blocks_soft_only_evidence() -> None:
    """仅弱信号（无黑名单/联系方式硬证据）→ 复核门拦截转人工。"""
    gate = ReviewGate()
    decision = gate.review(make_msg("今天想找人聊天"), primary_high("今天想找人聊天", ["聊天"]))
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []
    assert "复核门拦截" in decision.reason


def test_review_gate_passes_blacklist_evidence() -> None:
    gate = ReviewGate()
    text = "招募兼职刷单，日结，加我微信 abc12345"
    decision = gate.review(make_msg(text), primary_high(text, ["刷单"]))
    assert decision.verdict == "violation_high"
    assert set(decision.recommended_actions) == {"recall", "mute", "warn"}


def test_review_gate_passes_contact_evidence() -> None:
    text = "加我微信 abc12345，随时联系"
    gate = ReviewGate()
    decision = gate.review(make_msg(text), primary_high(text, ["加我微信"]))
    # 有联系方式硬证据，复核放行
    assert decision.verdict in ("violation_high", "record_only")
    if decision.verdict == "violation_high":
        assert "复核门拦截" not in decision.reason


def test_review_gate_protects_owner() -> None:
    gate = ReviewGate()
    msg = make_msg("招募兼职刷单，日结")
    msg.sender.role = "owner"
    decision = gate.review(msg, primary_high("招募兼职刷单，日结"))
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []


def test_review_gate_leaves_non_high_untouched() -> None:
    gate = ReviewGate()
    primary = ModerationDecision(
        message_id="RV_MSG",
        group_openid=GROUP,
        sender_member_openid=MEMBER,
        verdict="record_only",
        confidence=0.4,
    )
    decision = gate.review(make_msg("正常内容"), primary)
    assert decision.verdict == "record_only"
    assert decision.reason == primary.reason


# ---------- 成本台账 ----------


def test_cost_ledger_zero_budget() -> None:
    ledger = CostLedger()
    ledger.record("ocr")
    ledger.record("ocr", failure=True)
    ledger.record("media_download")
    report = ledger.report()
    assert report["ocr"]["calls"] == 2
    assert report["ocr"]["failures"] == 1
    assert ledger.total_cost() == 0.0  # 零预算：全部本地组件无费用
    ledger.reset()
    assert ledger.report() == {}


# ---------- 熔断器 ----------


def test_circuit_breaker_opens_after_threshold() -> None:
    breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
    now = 1000.0
    for _ in range(3):
        assert breaker.allow(now)
        breaker.record_failure(now)
    assert breaker.state == "OPEN"
    assert breaker.allow(now + 10) is False  # 冷却期内拒绝


def test_circuit_breaker_half_open_then_close() -> None:
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=60)
    now = 1000.0
    breaker.record_failure(now)
    breaker.record_failure(now)
    assert breaker.state == "OPEN"
    assert breaker.allow(now + 61) is True  # 冷却后半开探测
    assert breaker.state == "HALF_OPEN"
    breaker.record_success()
    assert breaker.state == "CLOSED"


def test_circuit_breaker_half_open_failure_reopens() -> None:
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=60)
    now = 1000.0
    breaker.record_failure(now)
    breaker.record_failure(now)
    breaker.allow(now + 61)
    breaker.record_failure(now + 61)
    assert breaker.state == "OPEN"
