"""一次性确认码（T-301）：5分钟有效、用后即焚；处罚操作必须凭码确认。"""

from __future__ import annotations

import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

CODE_TTL_SECONDS = 300  # 5分钟（PROJECT_CONTEXT 既定）


@dataclass
class PendingCode:
    code: str
    expires_at: float
    purpose: str
    reserved: bool = False


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
    with reserve_code(case_id, code) as accepted:
        return accepted


@contextmanager
def reserve_code(case_id: int, code: str) -> Iterator[bool]:
    """Reserve until transaction exit; a failed transaction may retry within TTL."""
    pending = _pending.get(case_id)
    if pending is None:
        yield False
        return
    if time.monotonic() > pending.expires_at:
        del _pending[case_id]
        yield False
        return
    if pending.reserved or not secrets.compare_digest(
        pending.code.encode("utf-8"), code.strip().encode("utf-8")
    ):
        yield False
        return
    pending.reserved = True
    try:
        yield True
    except BaseException:
        if _pending.get(case_id) is pending:
            if time.monotonic() > pending.expires_at:
                del _pending[case_id]
            else:
                pending.reserved = False
        raise
    else:
        if _pending.get(case_id) is pending:
            del _pending[case_id]  # Successful use consumes only this code generation.


def clear(case_id: int) -> None:
    _pending.pop(case_id, None)


def reset_all() -> None:
    """仅供测试。"""
    _pending.clear()
