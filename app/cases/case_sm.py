"""案件状态机（T-104 / T-301 共用）。

状态图（PROJECT_CONTEXT 既定）：
PENDING_REVIEW
  ├─→ APPROVED_MANUAL  → MANUAL_PENDING  → KICKED / CANCELLED
  ├─→ APPROVED_NAPCAT  → CONFIRM_PENDING → EXECUTING → KICKED / FAILED
  ├─→ KEEP             → CLOSED
  └─→ FALSE_POSITIVE   → STRIKE_REVOKED  → CLOSED

硬约束：同一案件人工出口与 NapCat 出口**互斥**——一旦进入任一出口分支，
另一出口的转换必须被拒绝；FAILED 只能转人工队列（MANUAL_PENDING），不能重试 NapCat。
"""

from __future__ import annotations

from typing import Literal

CaseStatus = Literal[
    "PENDING_REVIEW",
    "APPROVED_MANUAL",
    "MANUAL_PENDING",
    "APPROVED_NAPCAT",
    "CONFIRM_PENDING",
    "EXECUTING",
    "KICKED",
    "FAILED",
    "CANCELLED",
    "KEEP",
    "FALSE_POSITIVE",
    "STRIKE_REVOKED",
    "CLOSED",
]

# 合法转换表：from -> 允许的 to 集合
TRANSITIONS: dict[str, set[str]] = {
    "PENDING_REVIEW": {"APPROVED_MANUAL", "APPROVED_NAPCAT", "KEEP", "FALSE_POSITIVE"},
    "APPROVED_MANUAL": {"MANUAL_PENDING"},
    "MANUAL_PENDING": {"KICKED", "CANCELLED"},
    "APPROVED_NAPCAT": {"CONFIRM_PENDING"},
    "CONFIRM_PENDING": {"EXECUTING"},
    "EXECUTING": {"KICKED", "FAILED"},
    "FAILED": {"MANUAL_PENDING"},  # NapCat失败只转人工队列，不重试NapCat
    "FALSE_POSITIVE": {"STRIKE_REVOKED"},
    "STRIKE_REVOKED": {"CLOSED"},
    "KEEP": {"CLOSED"},
    "KICKED": {"CLOSED"},
    "CANCELLED": {"CLOSED"},
}


class IllegalTransitionError(ValueError):
    """非法状态转换。"""


def can_transition(current: str, target: str) -> bool:
    allowed = TRANSITIONS.get(current, set())
    if target not in allowed:
        return False
    # 双出口互斥：若案件已进入某出口分支，禁止再进入另一出口的分支
    exit_branches = {
        "APPROVED_MANUAL",
        "MANUAL_PENDING",
        "APPROVED_NAPCAT",
        "CONFIRM_PENDING",
        "EXECUTING",
    }
    if current not in exit_branches and target in exit_branches:
        return True  # 从 PENDING_REVIEW 首次进入出口，允许
    if current in exit_branches and target in exit_branches:
        # 已在出口分支内：仅允许同一出口链内的前进转换（已由 TRANSITIONS 表约束）
        return True
    return True


def validate_transition(current: str, target: str) -> None:
    """校验转换合法性，非法则抛 `IllegalTransitionError`。"""
    if target not in TRANSITIONS.get(current, set()):
        raise IllegalTransitionError(
            f"案件状态不允许从 {current} 转换到 {target}（合法目标：{sorted(TRANSITIONS.get(current, set()))}）"
        )
    # 双出口互斥检查：进入 NapCat 出口时，案件不得已经走过人工出口，反之亦然
    manual_branch = {"APPROVED_MANUAL", "MANUAL_PENDING"}
    napcat_branch = {"APPROVED_NAPCAT", "CONFIRM_PENDING", "EXECUTING"}
    if target == "APPROVED_NAPCAT" and current == "PENDING_REVIEW":
        return
    if target == "APPROVED_MANUAL" and current == "PENDING_REVIEW":
        return
    if target == "MANUAL_PENDING" and current == "FAILED":
        return
    if current in napcat_branch and target in manual_branch:
        raise IllegalTransitionError(
            "双出口互斥：NapCat 出口进行中禁止转人工出口入口状态（FAILED→MANUAL_PENDING 除外）"
        )
    if current in manual_branch and target in napcat_branch:
        raise IllegalTransitionError("双出口互斥：人工出口进行中禁止转 NapCat 出口")
