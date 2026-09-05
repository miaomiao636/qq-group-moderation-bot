"""一次性确认码（T-301）：5分钟有效、用后即焚；处罚操作必须凭码确认。"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

CODE_TTL_SECONDS = 300  # 5分钟（PROJECT_CONTEXT 既定）


@dataclass
class PendingCode:
    code: str
    expires_at: float
    purpose: str


_pending: dict[int, PendingCode] = {}  # case_id -> code


def generate(case_id: int, purpose: str = "kick_confirm") -> str:
    """为案件生成一次性确认码，覆盖旧码。"""
    code = f"{secrets.randbelow(1_000_000):06d}"
    _pending[case_id] = PendingCode(
        code=code, expires_at=time.monotonic() + CODE_TTL_SECONDS, purpose=purpose
    )
    return code


def verify_and_consume(case_id: int, code: str) -> bool:
    """校验并消费确认码；过期/错误均返回 False。"""
    pending = _pending.get(case_id)
    if pending is None:
        return False
    if time.monotonic() > pending.expires_at:
        del _pending[case_id]
        return False
    if not secrets.compare_digest(pending.code, code.strip()):
        return False
    del _pending[case_id]  # 一次性：用后即焚
    return True


def clear(case_id: int) -> None:
    _pending.pop(case_id, None)


def reset_all() -> None:
    """仅供测试。"""
    _pending.clear()
