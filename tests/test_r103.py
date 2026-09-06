"""R-103 correctness regressions."""

from __future__ import annotations

import uuid

import pytest
from app.adapters.qq_official.dedup import begin_processing, reset_memory_cache
from app.db import SessionLocal
from app.runtime.pipeline import run_pipeline


@pytest.fixture(autouse=True)
def _clean_dedup_cache() -> None:
    reset_memory_cache()
    yield
    reset_memory_cache()


@pytest.mark.asyncio
async def test_processing_lease_blocks_second_claim() -> None:
    mid = f"LEASE_{uuid.uuid4().hex}"
    async with SessionLocal() as s1, SessionLocal() as s2:
        first = await begin_processing(s1, mid, lease_seconds=300)
        second = await begin_processing(s2, mid, lease_seconds=300)

    assert first.accepted is True
    assert first.token
    assert first.status == "PROCESSING"
    assert second.accepted is False
    assert second.status == "PROCESSING"


@pytest.mark.asyncio
async def test_permanent_parse_error_is_not_retried_or_duplicated() -> None:
    payload = {
        "id": f"BAD_{uuid.uuid4().hex}",
        "author": {"member_openid": "M_BAD", "username": "bad"},
    }
    async with SessionLocal() as session:
        first = await run_pipeline(payload, session)
        second = await run_pipeline(payload, session)

    assert first is not None
    assert first.verdict == "record_only"
    assert second is None
