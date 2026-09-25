# CAMPUS-SCOPE-20260922: synthetic source fixtures adapted; see docs/2026-09-22-campus-source-scope.md.
"""R-108 retain timestamp metadata, not message originals, across raw-data cleanup."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.core.contracts import Sender, StandardMessage
from app.db import SessionLocal
from app.moderation.ai import AIModerationResult
from app.moderation.decision import ModerationDecision
from app.moderation.wall_pair import maybe_wall_text_pairing
from app.reports.cleanup import _shadow_metadata
from app.runtime.models import ShadowDecision

from tests.campus_fixtures import campus_evidence

TEXT = "合成校园兼职测试文案，不含任何真实联系方式"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-09-14T12:34:56+08:00", "2026-09-14T04:34:56+00:00"),
        ("2026-09-14T04:34:56", "2026-09-14T04:34:56+00:00"),
    ],
)
def test_cleanup_keeps_only_canonical_timestamp_metadata(value, expected) -> None:
    result = json.loads(
        _shadow_metadata(
            json.dumps(
                {
                    "sent_at": value,
                    "text_preview": "synthetic original must disappear",
                    "media_files": [{"name": "synthetic.jpg"}],
                }
            )
        )
    )
    assert result["sent_at"] == expected
    assert result["purged"] is True
    assert result["text_preview"] == "" and "media_files" not in result
    assert json.loads(_shadow_metadata(json.dumps(result)))["sent_at"] == expected


@pytest.mark.parametrize("value", [None, {}, [], 123, True, "original text", "2026-99-99"])
def test_cleanup_does_not_preserve_invalid_or_content_bearing_time(value) -> None:
    result = json.loads(_shadow_metadata(json.dumps({"sent_at": value})))
    assert "sent_at" not in result


@pytest.mark.parametrize(
    "historical_kind,expected",
    [
        # 口径 C（2026-09-17）：历史行（含清理标记）既不能毒化豁免窗口（阻断），
        # 也不能越过来源结构化校验（此处校园墙图仍为唯一来源）。
        ("legacy-purged", "record_only"),
        ("unpurged-missing", "record_only"),
        ("purged-recent", "record_only"),
    ],
)
async def test_cleanup_markers_do_not_poison_or_bypass_pairing(historical_kind, expected) -> None:
    now = datetime.now(UTC)
    group = "retention-" + uuid.uuid4().hex
    message_id = str(int(uuid.uuid4().hex[:12], 16))
    msg = StandardMessage(
        message_id=message_id,
        provider="onebot",
        external_group_id=group,
        external_user_id="9000000000001",
        sender=Sender(),
        sent_at=now,
        kind="text",
        text=TEXT,
    )
    result = AIModerationResult(
        category=None,
        confidence=1.0,
        source="vision",
        needs_review=False,
        model_id="synthetic-vision",
        campus_wall_source="万能校园墙",
        evidence=campus_evidence("校园墙白名单|文案:" + TEXT),
    )
    image_detail = {
        "sent_at": (now - timedelta(seconds=20)).isoformat(),
        "ai_results": [result.model_dump()],
    }
    old_detail = (
        {}
        if historical_kind == "unpurged-missing"
        else {
            "purged": True,
            "reason": "raw_retention_expired",
        }
    )
    if historical_kind == "purged-recent":
        old_detail["sent_at"] = (now - timedelta(seconds=10)).isoformat()
    async with SessionLocal() as session:
        for kind, detail in [("image", image_detail), ("text", old_detail)]:
            old_id = str(int(uuid.uuid4().hex[:12], 16))
            session.add(
                ShadowDecision(
                    message_id="onebot:10000001:" + old_id,
                    external_message_id=old_id,
                    provider="onebot",
                    external_group_id=group,
                    external_user_id=msg.external_user_id,
                    group_openid=group,
                    member_openid=msg.external_user_id,
                    kind=kind,
                    verdict="allow",
                    detail_json=json.dumps(detail),
                )
            )
        await session.commit()
        high = ModerationDecision(
            message_id=message_id,
            provider="onebot",
            external_group_id=group,
            external_user_id=msg.external_user_id,
            verdict="violation_high",
            category="ad",
            confidence=0.99,
            recommended_actions=["recall"],
        )
        decision = await maybe_wall_text_pairing(
            session,
            msg,
            high,
            event_key="onebot:10000001:" + message_id,
        )
        await session.commit()
    assert decision.verdict == expected
