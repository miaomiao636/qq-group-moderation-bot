"""R-102 整改回归测试：extra_blacklist、复核门软信号拦截、GIF多帧缓存键、
媒体缺失=record_only、流水线按类型分发、案件审计from/to、并发幂等立案。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from app.adapters.qq_official.contract import Sender, StandardMessage
from app.db import SessionLocal
from app.moderation.decision import ModerationDecision, RuleHit
from app.moderation.image_engine import ImageModerationEngine
from app.moderation.review_gate import ReviewGate
from app.moderation.rules import TextRuleEngine
from app.runtime.pipeline import run_pipeline

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "qq_official"


# ---------- R-102-6 extra_blacklist 参与判断 ----------


def test_extra_blacklist_participates_in_scoring() -> None:
    """自定义黑名单词应进入R001评分，而非仅用默认表。"""
    text = "雪糕招募"  # "雪糕招募" 不在默认表；"招募"是弱信号
    base = TextRuleEngine()
    assert base.evaluate(_msg(text)).verdict != "violation_high"  # 仅招募弱信号0.30，不达标

    engine = TextRuleEngine(extra_blacklist=("雪糕招募",))
    decision = engine.evaluate(_msg("雪糕招募"))
    # R001(自定义0.70) + R002(招募0.30) → 1.0
    assert decision.verdict == "violation_high"
    assert any(h.rule_id == "R001" for h in decision.rule_hits)


def _msg(text: str, role: str = "member") -> StandardMessage:
    return StandardMessage(
        message_id=f"R102_{uuid.uuid4().hex[:6]}",
        group_openid="G_R102",
        sender=Sender(member_openid="M_R102", role=role),
        text=text,
    )


# ---------- R-102-7 复核门：软信号不算硬证据 ----------


def test_review_gate_blocks_soft_only_primary() -> None:
    """主决策仅靠弱信号（R002）凑到阈值 → 复核门必须拦截转人工。"""
    primary = ModerationDecision(
        message_id="R102G",
        group_openid="G",
        sender_member_openid="M",
        verdict="violation_high",
        confidence=0.95,
        rule_hits=[
            RuleHit(
                rule_id="R002",
                rule_name="soft_signals",
                category="ad",
                confidence_delta=0.95,
                evidence_masked="x",
            )
        ],
        recommended_actions=["recall", "mute", "warn"],
    )
    decision = ReviewGate().review(_msg("仅弱信号"), primary)
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []
    assert "复核门拦截" in decision.reason


def test_review_gate_passes_when_hard_evidence_present() -> None:
    primary = ModerationDecision(
        message_id="R102G",
        group_openid="G",
        sender_member_openid="M",
        verdict="violation_high",
        confidence=0.95,
        rule_hits=[
            RuleHit(
                rule_id="R002",
                rule_name="soft_signals",
                category="ad",
                confidence_delta=0.50,
                evidence_masked="",
            ),
            RuleHit(
                rule_id="R001",
                rule_name="explicit_blacklist",
                category="ad",
                confidence_delta=0.70,
                evidence_masked="刷单",
            ),
        ],
        recommended_actions=["recall", "mute", "warn"],
    )
    decision = ReviewGate().review(_msg("有黑名单"), primary)
    assert decision.verdict == "violation_high"


# ---------- R-102-5 GIF 缓存键用完整帧集合 ----------


def _gif_bytes(patterns: list[int]) -> bytes:
    import io

    from PIL import Image, ImageDraw

    def frame(pid: int) -> Image.Image:
        img = Image.new("RGB", (32, 32), "black")
        d = ImageDraw.Draw(img)
        if pid == 0:
            d.rectangle([4, 4, 28, 28], fill="white")
        elif pid == 1:
            d.ellipse([4, 4, 28, 28], fill="red")
        elif pid == 2:
            d.line([0, 0, 31, 31], fill="blue", width=3)
            d.line([0, 31, 31, 0], fill="blue", width=3)
        return img

    frames = [frame(p) for p in patterns]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    return buf.getvalue()


def test_gif_cache_key_uses_all_frames_not_first() -> None:
    """两个首帧相同但后续帧不同的GIF，缓存键不同（用缓存条目数证明）。"""
    gif_a = _gif_bytes([0, 1])  # 首帧pattern0
    gif_b = _gif_bytes([0, 2])  # 首帧同为pattern0，第二帧不同

    engine = ImageModerationEngine()
    a = engine.analyze(gif_a)
    engine.analyze(gif_b)
    # 关键断言：两个GIF各自有独立缓存条目（若仅用首帧作键，B会覆盖A，只剩1条）
    assert len(engine._cache) == 2
    # 再次分析A：命中自己的缓存，判定与首次一致
    a2 = engine.analyze(gif_a)
    assert a2.cache_hit is True
    assert a2.verdict == a.verdict


# ---------- R-102-3 媒体缺失/下载失败 → record_only ----------


@pytest.mark.asyncio
async def test_pipeline_media_missing_is_record_only() -> None:
    payload = _image_payload(filename="definitely_missing.jpg")
    async with SessionLocal() as session:
        record = await run_pipeline(payload, session)
        assert record is not None
        assert record.verdict == "record_only"
        assert "媒体缺失" in record.reason or "转人工" in record.reason


@pytest.mark.asyncio
async def test_pipeline_parse_failure_is_record_only_and_retryable() -> None:
    """解析失败落 record_only 记录且事件标记 FAILED 可重试。"""
    from app.adapters.qq_official.dedup import begin_processing

    payload = json.loads(
        (FIXTURE_DIR / "group_message_create_text_plain.json").read_text(encoding="utf-8")
    )["data"]
    payload["id"] = f"R102_PARSE_{uuid.uuid4().hex[:6]}"
    payload["author"] = None  # 触发解析失败
    async with SessionLocal() as session:
        record = await run_pipeline(payload, session)
        assert record is not None
        assert record.verdict == "record_only"
        assert "解析失败" in record.reason
    # 失败后可重试
    async with SessionLocal() as session:
        assert await begin_processing(session, payload["id"]) is True


def _image_payload(*, filename: str) -> dict:
    return {
        "id": f"R102_IMG_{uuid.uuid4().hex[:6]}",
        "group_openid": "G_R102",
        "group_id": "G_R102",
        "author": {
            "member_openid": "M_R102",
            "member_role": "member",
            "bot": False,
            "username": "tester",
        },
        "content": "",
        "attachments": [
            {
                "content_type": "image/jpeg",
                "filename": filename,
                "size": 12345,
                "url": "https://expired.invalid/x.jpg",
            }
        ],
        "timestamp": "2026-09-06T10:00:00+08:00",
    }


# ---------- R-102-1 按类型分发 ----------


@pytest.mark.asyncio
async def test_pipeline_dispatches_voice_to_text_rules(tmp_path: Path, monkeypatch) -> None:
    """语音附件含官方转写违规文本 → 高置信违规。"""
    monkeypatch.setattr("app.runtime.pipeline.MEDIA_DIR", tmp_path)

    amr = tmp_path / "r102_voice.amr"
    amr.write_bytes(b"#!AMR\x00\x00\x00")
    try:
        payload = {
            "id": f"R102_V_{uuid.uuid4().hex[:6]}",
            "group_openid": "G_R102",
            "author": {
                "member_openid": "M_R102",
                "member_role": "member",
                "bot": False,
                "username": "tester",
            },
            "content": "",
            "attachments": [
                {
                    "content_type": "voice",
                    "filename": amr.name,
                    "size": 8,
                    "url": "x",
                    "asr_refer_text": "招募兼职刷单，日结，加我微信 abc12345",
                }
            ],
            "timestamp": "2026-09-06T10:00:00+08:00",
        }
        async with SessionLocal() as session:
            record = await run_pipeline(payload, session)
            assert record is not None
            assert record.verdict == "violation_high"
    finally:
        amr.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_pipeline_dispatches_file_to_text_rules(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.runtime.pipeline.MEDIA_DIR", tmp_path)

    txt = tmp_path / "r102_spam.txt"
    txt.write_text("招募兼职刷单，日结，加我微信 abc12345", encoding="utf-8")
    try:
        payload = {
            "id": f"R102_F_{uuid.uuid4().hex[:6]}",
            "group_openid": "G_R102",
            "author": {
                "member_openid": "M_R102",
                "member_role": "member",
                "bot": False,
                "username": "tester",
            },
            "content": "",
            "attachments": [{"content_type": "file", "filename": txt.name, "size": 40, "url": "x"}],
            "timestamp": "2026-09-06T10:00:00+08:00",
        }
        async with SessionLocal() as session:
            record = await run_pipeline(payload, session)
            assert record is not None
            assert record.verdict == "violation_high"
    finally:
        txt.unlink(missing_ok=True)


# ---------- R-102-8 案件审计 from/to + 并发幂等立案 ----------


@pytest.mark.asyncio
async def test_case_audit_records_correct_from_to() -> None:
    from app.cases.service import transition_case

    group, member = f"G_AUDIT_{uuid.uuid4().hex[:6]}", f"M_AUDIT_{uuid.uuid4().hex[:6]}"
    case_id = await _make_case(group, member)
    async with SessionLocal() as session:
        case = await transition_case(session, case_id, "APPROVED_MANUAL", operator="tester")
        audit = json.loads(case.audit_json)
        transitions = audit["transitions"]
        assert transitions[-1]["from"] == "PENDING_REVIEW"
        assert transitions[-1]["to"] == "APPROVED_MANUAL"


@pytest.mark.asyncio
async def test_concurrent_violations_produce_one_case() -> None:
    """同群同成员多次违规只产生一个 PENDING_REVIEW 案件（幂等）。"""
    from app.cases.models import Case
    from sqlalchemy import select

    group, member = f"G_CONC_{uuid.uuid4().hex[:6]}", f"M_CONC_{uuid.uuid4().hex[:6]}"
    # 第1次违规（strike1）
    await _record(group, member, f"MSG_C1_{uuid.uuid4().hex[:6]}")
    # 第2次违规（strike2 → 立案）
    await _record(group, member, f"MSG_C2_{uuid.uuid4().hex[:6]}")
    # 第3次违规（strike3 → _create_case 幂等返回已有案件）
    await _record(group, member, f"MSG_C3_{uuid.uuid4().hex[:6]}")

    async with SessionLocal() as session:
        cases = (
            (await session.execute(select(Case).where(Case.group_openid == group))).scalars().all()
        )
    assert len(cases) == 1


async def _record(group: str, member: str, message_id: str) -> None:
    from app.cases.service import record_violation

    async with SessionLocal() as session:
        await record_violation(
            session,
            StandardMessage(
                message_id=message_id,
                group_openid=group,
                sender=Sender(member_openid=member),
                text="违规内容",
            ),
            ModerationDecision(
                message_id=message_id,
                group_openid=group,
                sender_member_openid=member,
                verdict="violation_high",
                category="ad",
                confidence=0.95,
                rule_hits=[
                    RuleHit(
                        rule_id="R001",
                        rule_name="explicit_blacklist",
                        category="ad",
                        confidence_delta=0.70,
                        evidence_masked="刷单",
                    )
                ],
            ),
        )


async def _make_case(group: str, member: str) -> int:
    await _record(group, member, f"MSG_M1_{uuid.uuid4().hex[:6]}")
    outcome = await _record_outcome(group, member, f"MSG_M2_{uuid.uuid4().hex[:6]}")
    assert outcome.case is not None
    return outcome.case.id


async def _record_outcome(group: str, member: str, message_id: str):
    from app.cases.service import record_violation

    async with SessionLocal() as session:
        return await record_violation(
            session,
            StandardMessage(
                message_id=message_id,
                group_openid=group,
                sender=Sender(member_openid=member),
                text="违规内容",
            ),
            ModerationDecision(
                message_id=message_id,
                group_openid=group,
                sender_member_openid=member,
                verdict="violation_high",
                category="ad",
                confidence=0.95,
                rule_hits=[
                    RuleHit(
                        rule_id="R001",
                        rule_name="explicit_blacklist",
                        category="ad",
                        confidence_delta=0.70,
                        evidence_masked="刷单",
                    )
                ],
            ),
        )
