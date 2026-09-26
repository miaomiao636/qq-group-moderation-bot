"""T-202 视频/语音/文件审核测试。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from app.adapters.qq_official.contract import Attachment
from app.moderation.image_engine import ImageModerationEngine
from app.moderation.media_engine import (
    evaluate_file,
    evaluate_video,
    evaluate_voice,
)
from app.moderation.rules import TextRuleEngine

MSG_ID = "MEDIA_MSG_1"
GROUP = "GROUP_MEDIA"
MEMBER = "MEMBER_MEDIA"


def attachment(**overrides: object) -> Attachment:
    fields: dict[str, object] = {"content_type": "voice", "filename": "a.amr", "size": 100}
    fields.update(overrides)
    return Attachment(**fields)  # type: ignore[arg-type]


# ---------- 语音 ----------


def test_voice_spam_transcription_flagged() -> None:
    att = attachment(asr_refer_text="招募兼职刷单，日结，加我微信 abc12345")
    decision = evaluate_voice(MSG_ID, GROUP, MEMBER, att, TextRuleEngine())
    assert decision.verdict == "violation_high"
    assert decision.recommended_actions == ["recall"]
    assert "kick" not in decision.model_dump_json().lower()


def test_voice_clean_transcription_record_only() -> None:
    att = attachment(asr_refer_text="今天天气不错")
    decision = evaluate_voice(MSG_ID, GROUP, MEMBER, att, TextRuleEngine())
    assert decision.verdict == "record_only"


def test_voice_no_transcription_record_only_never_punish() -> None:
    att = attachment(asr_refer_text="")
    decision = evaluate_voice(MSG_ID, GROUP, MEMBER, att, TextRuleEngine())
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []


# ---------- 视频 ----------


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def make_test_video(path: Path, color: str, frames: int = 10) -> bool:
    """用 ffmpeg 生成测试视频；失败返回 False。"""
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=64x64:d=1",
                "-frames:v",
                str(frames),
                str(path),
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
        return result.returncode == 0 and path.exists()
    except OSError:
        return False


@pytest.mark.skipif(not has_ffmpeg(), reason="本机无 ffmpeg")
def test_video_clean_frames_allow(tmp_path: Path) -> None:
    video = tmp_path / "v.mp4"
    assert make_test_video(video, "white")
    decision = evaluate_video(
        MSG_ID, GROUP, MEMBER, video, ImageModerationEngine(), tmp_path / "frames"
    )
    assert decision.verdict == "allow"


@pytest.mark.skipif(not has_ffmpeg(), reason="本机无 ffmpeg")
def test_video_blacklisted_frame_violation(tmp_path: Path) -> None:
    video = tmp_path / "bad.mp4"
    if not make_test_video(video, "black"):
        pytest.skip("ffmpeg生成失败")
    # 构造黑名单：以第一帧的哈希为基准

    # 用视频抽出的第一帧哈希做黑名单，模拟"帧内容违规"
    frames_dir = tmp_path / "probe"
    extracted = evaluate_probe(video, frames_dir)
    assert extracted, "抽帧应成功"
    engine = ImageModerationEngine(violation_hashes={extracted})
    decision = evaluate_video(MSG_ID, GROUP, MEMBER, video, engine, tmp_path / "frames2")
    assert decision.verdict == "violation_high"


def evaluate_probe(video: Path, frames_dir: Path) -> str:
    from app.moderation.imaging import dhash, image_frames_from_source
    from app.moderation.media_engine import _extract_video_frames

    frames = _extract_video_frames(video, frames_dir)
    assert frames, "抽帧失败"
    return dhash(image_frames_from_source(frames[0])[0])


def test_video_oversize_record_only(tmp_path: Path) -> None:
    big = tmp_path / "big.mp4"
    big.write_bytes(b"\x00" * (200 * 1024 * 1024 + 1))
    decision = evaluate_video(MSG_ID, GROUP, MEMBER, big, ImageModerationEngine(), tmp_path)
    assert decision.verdict == "record_only"
    assert "200MB" in decision.reason
    big.unlink()


def test_video_missing_file_record_only(tmp_path: Path) -> None:
    decision = evaluate_video(
        MSG_ID, GROUP, MEMBER, tmp_path / "nope.mp4", ImageModerationEngine(), tmp_path
    )
    assert decision.verdict == "record_only"


# ---------- 文件 ----------


def test_pdf_with_spam_text_flagged(tmp_path: Path) -> None:
    from pypdf import PdfWriter
    from pypdf.generic import NameObject, TextStringObject

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    page = writer.pages[0]
    # pypdf 不便写中文文本流，改用最简文本对象注入测试关键词
    content = b"BT /F1 12 Tf 10 100 Td (zhao piandan rijia) Tj ET"
    page[NameObject("/Contents")] = TextStringObject(content.decode("latin-1"))
    writer.write(tmp_path / "t.pdf")

    # 该 PDF 无真实可读文本，预期 record_only（不虚构结果）
    decision = evaluate_file(MSG_ID, GROUP, MEMBER, tmp_path / "t.pdf", TextRuleEngine())
    assert decision.verdict == "record_only"


def test_text_file_spam_flagged(tmp_path: Path) -> None:
    f = tmp_path / "spam.txt"
    f.write_text("招募兼职刷单，日结，加我微信 abc12345", encoding="utf-8")
    decision = evaluate_file(MSG_ID, GROUP, MEMBER, f, TextRuleEngine())
    assert decision.verdict == "violation_high"


def test_text_file_clean_record_only(tmp_path: Path) -> None:
    f = tmp_path / "ok.txt"
    f.write_text("随便写点正常内容", encoding="utf-8")
    decision = evaluate_file(MSG_ID, GROUP, MEMBER, f, TextRuleEngine())
    assert decision.verdict == "record_only"


def test_executable_file_never_executed_only_recorded(tmp_path: Path) -> None:
    f = tmp_path / "evil.exe"
    f.write_bytes(b"MZ pretend binary - spam content here")
    # 即使内容含违规词，可执行类型也绝不解析执行，仅元数据记录
    decision = evaluate_file(MSG_ID, GROUP, MEMBER, f, TextRuleEngine())
    assert decision.verdict == "record_only"
    assert "不提取内容" in decision.reason


def test_broken_pdf_record_only(tmp_path: Path) -> None:
    f = tmp_path / "broken.pdf"
    f.write_bytes(b"%PDF-1.4 broken content")
    decision = evaluate_file(MSG_ID, GROUP, MEMBER, f, TextRuleEngine())
    assert decision.verdict == "record_only"
