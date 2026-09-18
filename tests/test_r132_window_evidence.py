# ruff: noqa: E402, I001, F401
# Reviewer probe pack (PR #45 / r132), promoted verbatim into the repo test suite.
# Isolation asserts intentionally run BEFORE application imports (E402 is by design).
# This header changes no assertion and no logic.

import json
import os
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

# Stop before importing application state unless the repository fixture created
# a temporary, SHADOW-only database. Never inherit a production .env.
assert os.environ.get("APP_ENV") == "test", "Use -p tests.conftest before importing this module"
assert os.environ.get("ACTION_MODE") == "SHADOW"
for flag in (
    "AI_ENABLED",
    "ONEBOT_ACTIONS_ENABLED",
    "NOTIFICATIONS_ENABLED",
    "NOTIFICATION_QQ_ENABLED",
    "NOTIFICATION_EMAIL_ENABLED",
    "NOTIFICATION_HEARTBEAT_ENABLED",
):
    assert os.environ.get(flag) == "false", flag
database_url = os.environ.get("DATABASE_URL", "")
assert database_url.startswith("sqlite+aiosqlite:///")
database_path = Path(database_url.split(":///", 1)[1]).resolve()
assert database_path.parent.name.startswith("qqbot-test-")
assert database_path.is_relative_to(Path(tempfile.gettempdir()).resolve())

from app.adapters.onebot.parser import OneBotMessageSource
from app.db import SessionLocal
from app.moderation.ai import AIModerationResult, merge_ai_evidence
from app.moderation.image_engine import MediaAnalysis
from app.moderation.rules import TextRuleEngine
from app.runtime import pipeline
from app.runtime.models import ShadowDecision


def ident():
    return 1000000000 + int(uuid.uuid4().hex[:8], 16)


async def source_image(group, user, when):
    sid = str(ident())
    async with SessionLocal() as session:
        session.add(
            ShadowDecision(
                message_id=f"onebot:10000001:{sid}",
                external_message_id=sid,
                provider="onebot",
                external_group_id=str(group),
                external_user_id=str(user),
                group_openid=str(group),
                member_openid=str(user),
                kind="image",
                verdict="allow",
                category="",
                confidence=1.0,
                reason="synthetic source",
                detail_json=json.dumps(
                    {
                        "sent_at": when.isoformat(),
                        "ai_results": [
                            {
                                "source": "vision",
                                "category": None,
                                "needs_review": False,
                                "degraded_reason": "",
                                "evidence": "校园墙白名单|文案:合成校园活动",
                            }
                        ],
                    }
                ),
            )
        )
        await session.commit()


def event(group, user, when, segments, files=()):
    return {
        "self_id": 10000001,
        "post_type": "message",
        "message_type": "group",
        "message_id": ident(),
        "group_id": group,
        "user_id": user,
        "time": int(when.timestamp()),
        "message": segments,
        "sender": {"user_id": user, "role": "member", "nickname": "synthetic"},
        "_downloaded": list(files),
    }


class LocalImageAllow:
    def analyze(self, path):
        return MediaAnalysis("allow", 1.0, reason="synthetic local analyzer")


class SyntheticModels:
    def __init__(self, results=()):
        self.results = list(results)

    async def review_message(self, session, msg, decision, **kwargs):
        return merge_ai_evidence(decision, self.results), self.results


async def execute(payload, models, engine=None):
    async with SessionLocal() as session:
        return await pipeline.run_pipeline(
            payload,
            session,
            message_source=OneBotMessageSource(),
            ai_service=models,
            text_engine=engine,
            image_engine=LocalImageAllow(),
            dedup_key=f"onebot:10000001:{payload['message_id']}",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("ad_confidence, severe_confidence", [(0.99, 0.95), (0.95, 0.99)])
@pytest.mark.parametrize("severe_category", ["porn", "violence"])
async def test_picture_window_must_preserve_any_porn_signal(
    monkeypatch, tmp_path, ad_confidence, severe_confidence, severe_category
):
    """One message contains two images; its most confident category is not its only category."""
    group, user = ident(), ident()
    when = datetime.now(UTC).replace(microsecond=0)
    await source_image(group, user, when - timedelta(seconds=10))
    # No image decoder/network is called; these files only satisfy the pipeline's exists guard.
    (tmp_path / "a.png").touch()
    (tmp_path / "b.png").touch()
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    payload = event(
        group,
        user,
        when,
        [
            {"type": "image", "data": {"file": "a.png"}},
            {"type": "image", "data": {"file": "b.png"}},
        ],
        ("a.png", "b.png"),
    )
    results = [
        AIModerationResult(
            source="vision",
            category="ad",
            confidence=ad_confidence,
            needs_review=False,
            model_id="synthetic",
            review_group="image-a",
        ),
        AIModerationResult(
            source="vision",
            category=severe_category,
            confidence=severe_confidence,
            needs_review=False,
            model_id="synthetic",
            review_group="image-b",
        ),
    ]
    if severe_category == "violence":
        results.append(
            AIModerationResult(
                source="vision",
                category=severe_category,
                confidence=0.99,
                needs_review=False,
                model_id="synthetic-secondary",
                review_group="image-b",
                review_role="secondary",
            )
        )
    record = await execute(payload, SyntheticModels(results))
    assert record is not None
    detail = json.loads(record.detail_json)
    assert any(hit["category"] == severe_category for hit in detail["rule_hits"])
    assert record.verdict == "violation_high", (record.category, record.reason, detail)
    assert "recall" in detail["recommended_actions"]


@pytest.mark.asyncio
async def test_window_must_preserve_real_flood_hit():
    """R005 is generated by the actual tracker, alongside an ad rule."""
    group, user = ident(), ident()
    when = datetime.now(UTC).replace(microsecond=0)
    await source_image(group, user, when - timedelta(seconds=10))
    engine = TextRuleEngine()
    record = None
    for index in range(3):
        payload = event(
            group,
            user,
            when + timedelta(seconds=index),
            [
                {"type": "text", "data": {"text": "合成推广 加我微信 synthetic001"}},
            ],
        )
        record = await execute(payload, SyntheticModels(), engine)
    assert record is not None
    detail = json.loads(record.detail_json)
    assert any(hit["rule_id"] == "R005" for hit in detail["rule_hits"])
    assert record.verdict == "violation_high", (record.category, record.reason, detail)
    assert "recall" in detail["recommended_actions"]
