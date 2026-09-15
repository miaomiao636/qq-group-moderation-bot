"""R-111 P2 回归：人工反馈「最新写入」口径（主审 bd5402c 复验整改）。

口径：列表、详情、学习侧统一按 FeedbackRecord.id 最大写入取最新；
不按 created_at（同秒/时钟回拨不得改变「最新」）。追加历史与审计保留。

覆盖：两次纠正回显最新（列表 + 详情）、只改原因不回退真值、
最新 label/reason 回显、同时间戳/时钟回拨、无反馈默认值、缺 CSRF 不写库。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient


def unique_ids() -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:8]
    return f"GROUP_R111_{suffix}", f"MEMBER_R111_{suffix}"


def make_shadow(mid: str, category: str, group: str, member: str) -> None:
    from app.db import SessionLocal
    from app.runtime.models import ShadowDecision

    async def _run() -> None:
        async with SessionLocal() as session:
            session.add(
                ShadowDecision(
                    message_id=mid,
                    provider="onebot",
                    external_group_id=group,
                    group_openid=group,
                    member_openid=member,
                    kind="text",
                    verdict="record_only",
                    category=category,
                    confidence=0.40,
                    reason="AI疑似严重类别（低置信），保留类别转人工核对",
                )
            )
            await session.commit()

    asyncio.run(_run())


def _insert_feedback(
    mid: str,
    label: str,
    category: str,
    reason: str,
    created_at: datetime | None = None,
) -> int:
    """直接插入一条反馈（返回 id）：用于同秒/时钟回拨等时间不可控场景。"""
    from app.db import SessionLocal
    from app.moderation.feedback import FeedbackRecord

    async def _run() -> int:
        async with SessionLocal() as session:
            row = FeedbackRecord(
                message_id=mid,
                label=label,
                category=category,
                operator="tester",
                reason=reason,
            )
            if created_at is not None:
                row.created_at = created_at
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row.id

    return asyncio.run(_run())


def _categories(mid: str) -> list[str]:
    from app.db import SessionLocal
    from app.moderation.feedback import FeedbackRecord
    from sqlalchemy import select

    async def _run() -> list[str]:
        async with SessionLocal() as session:
            rows = (
                (
                    await session.execute(
                        select(FeedbackRecord)
                        .where(FeedbackRecord.message_id == mid)
                        .order_by(FeedbackRecord.id)
                    )
                )
                .scalars()
                .all()
            )
            return [r.category for r in rows]

    return asyncio.run(_run())


def extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "页面应包含CSRF令牌"
    return match.group(1)


def _feedback_block(html: str, mid: str) -> str:
    anchor = html.find(f'name=message_id value="{mid}"')
    assert anchor != -1, f"页面应包含 {mid} 的反馈表单"
    return html[anchor : anchor + 1500]


def _page_selected_category(block: str) -> str:
    m = re.search(r"<option value=(\w+) selected>", block)
    return m.group(1) if m else "other"


def _submit_feedback(
    logged_in: TestClient,
    csrf: str,
    mid: str,
    label: str,
    category: str,
    reason: str,
) -> None:
    resp = logged_in.post(
        "/admin/feedback",
        data={
            "message_id": mid,
            "label": label,
            "category": category,
            "reason": reason,
            "csrf": csrf,
        },
    )
    assert resp.status_code == 200


def _two_corrections(logged_in: TestClient) -> tuple[str, str]:
    """建影子记录并人工纠正两次（ad→fraud→porn），返回 (mid, csrf)。"""
    group, member = unique_ids()
    mid = f"MSG_R111_{uuid.uuid4().hex[:8]}"
    make_shadow(mid, "ad", group, member)
    csrf = extract_csrf(logged_in.get("/admin/shadow").text)
    _submit_feedback(logged_in, csrf, mid, "confirmed_violation", "fraud", "第一次")
    _submit_feedback(logged_in, csrf, mid, "confirmed_violation", "porn", "第二次纠正")
    assert _categories(mid) == ["fraud", "porn"], "追加历史应保留"
    return mid, csrf


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def logged_in(client: TestClient) -> TestClient:
    resp = client.post(
        "/admin/login",
        data={"username": "admin", "password": "test-admin-pass"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return client


def test_list_reflects_latest_correction(logged_in: TestClient) -> None:
    """二次纠正后，列表回显必须是最新类别。"""
    mid, _csrf = _two_corrections(logged_in)
    block = _feedback_block(logged_in.get("/admin/shadow").text, mid)
    assert "<option value=porn selected>" in block, "列表应回显最新类别 porn"


def test_detail_reflects_latest_correction(logged_in: TestClient) -> None:
    """二次纠正后，详情回显必须是最新类别。"""
    mid, _csrf = _two_corrections(logged_in)
    detail = logged_in.get("/admin/shadow/detail", params={"message_id": mid})
    assert detail.status_code == 200
    block = _feedback_block(detail.text, mid)
    assert "<option value=porn selected>" in block, "详情应回显最新类别 porn"


def test_resubmit_page_value_does_not_revert_truth(logged_in: TestClient) -> None:
    """照页面实际值、只改原因再次提交：不得把旧类别写成最新真值。"""
    mid, csrf = _two_corrections(logged_in)
    block = _feedback_block(logged_in.get("/admin/shadow").text, mid)
    page_value = _page_selected_category(block)
    _submit_feedback(logged_in, csrf, mid, "confirmed_violation", page_value, "只改原因")
    assert _categories(mid)[-1] == "porn", "最新真值不得被页面旧值反转"


def test_latest_label_and_reason_reflected(logged_in: TestClient) -> None:
    """回显需同时反映最新 label 与最新 reason（同一最新行，不拼凑不同记录）。"""
    group, member = unique_ids()
    mid = f"MSG_R111_{uuid.uuid4().hex[:8]}"
    make_shadow(mid, "ad", group, member)
    csrf = extract_csrf(logged_in.get("/admin/shadow").text)
    _submit_feedback(logged_in, csrf, mid, "confirmed_violation", "fraud", "原因A")
    _submit_feedback(logged_in, csrf, mid, "confirmed_normal", "ad", "原因B")

    block = _feedback_block(logged_in.get("/admin/shadow").text, mid)
    assert "<option value=confirmed_normal selected>" in block, "label 应回显最新"
    assert 'value="原因B"' in block, "reason 应回显最新"

    # 只改原因再次提交：label/类别保持最新行，不回退
    _submit_feedback(logged_in, csrf, mid, "confirmed_normal", "ad", "原因C")
    block = _feedback_block(logged_in.get("/admin/shadow").text, mid)
    assert "<option value=confirmed_normal selected>" in block
    assert 'value="原因C"' in block


def test_same_timestamp_uses_max_id(logged_in: TestClient) -> None:
    """同秒写入：回显与学习侧一致取最大 ID（不得按时间猜先后）。"""
    group, member = unique_ids()
    mid = f"MSG_R111_{uuid.uuid4().hex[:8]}"
    make_shadow(mid, "ad", group, member)
    ts = datetime.now(UTC)
    _insert_feedback(mid, "confirmed_violation", "fraud", "同秒A", created_at=ts)
    _insert_feedback(mid, "confirmed_violation", "porn", "同秒B", created_at=ts)

    block = _feedback_block(logged_in.get("/admin/shadow").text, mid)
    assert "<option value=porn selected>" in block, "同秒时应取最大 ID 的类别"
    assert 'value="同秒B"' in block


def test_clock_skew_uses_max_id(logged_in: TestClient) -> None:
    """时钟回拨：后写记录时间更早，仍按最大 ID 取最新。"""
    group, member = unique_ids()
    mid = f"MSG_R111_{uuid.uuid4().hex[:8]}"
    make_shadow(mid, "ad", group, member)
    later = datetime.now(UTC) + timedelta(minutes=5)
    _insert_feedback(mid, "confirmed_violation", "porn", "先写但时间晚", created_at=later)
    _insert_feedback(
        mid,
        "confirmed_violation",
        "violence",
        "后写但时间早（回拨）",
        created_at=later - timedelta(minutes=10),
    )

    block = _feedback_block(logged_in.get("/admin/shadow").text, mid)
    assert "<option value=violence selected>" in block, "回拨时应取最大 ID 的类别"


def test_no_feedback_defaults_to_system_category(logged_in: TestClient) -> None:
    """新建且无任何反馈：默认选系统判定类别。"""
    group, member = unique_ids()
    mid = f"MSG_R111_{uuid.uuid4().hex[:8]}"
    make_shadow(mid, "violence", group, member)
    block = _feedback_block(logged_in.get("/admin/shadow").text, mid)
    assert "<option value=violence selected>" in block


def test_feedback_post_without_csrf_rejected_and_not_written(logged_in: TestClient) -> None:
    """缺 CSRF：拒绝且不写库。"""
    group, member = unique_ids()
    mid = f"MSG_R111_{uuid.uuid4().hex[:8]}"
    make_shadow(mid, "ad", group, member)
    resp = logged_in.post(
        "/admin/feedback",
        data={"message_id": mid, "label": "confirmed_violation", "category": "fraud"},
    )
    assert resp.status_code == 403
    assert _categories(mid) == [], "缺 CSRF 不得写入反馈"
