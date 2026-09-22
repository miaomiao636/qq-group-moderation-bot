"""视频/语音/文件审核（T-202）。

零预算方案（决策 D-014）：
- 语音：优先使用官方自带 `asr_refer_text` 转写（D-013），复用文字规则引擎判定；
- 视频：本地 ffmpeg 抽帧（≤16帧，PROJECT_CONTEXT 要求），逐帧复用图片引擎；
  超过 10 分钟或 200MB 只告警不审核（record_only）；ffmpeg 不可用时不判定；
- 文件：只提取 PDF（pypdf）与纯文本，不执行任何文件；其他类型仅元数据记录；
- 一切失败路径均为 record_only（分析失败不触发处罚）。
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from app.core.contracts import Attachment
from app.moderation.decision import ModerationDecision, RuleHit
from app.moderation.image_engine import ImageModerationEngine
from app.moderation.rules import TextRuleEngine

MAX_VIDEO_BYTES = 200 * 1024 * 1024
MAX_VIDEO_SECONDS = 10 * 60
MAX_VIDEO_FRAMES = 16
FFMPEG_TIMEOUT = 60


def _decision(
    message_id: str,
    group_openid: str,
    member_openid: str,
    verdict: str,
    confidence: float,
    reason: str,
    category: str | None = None,
    rule_hits: list[RuleHit] | None = None,
) -> ModerationDecision:
    actions: list[str] = ["recall", "mute", "warn"] if verdict == "violation_high" else []
    return ModerationDecision(
        message_id=message_id,
        group_openid=group_openid,
        sender_member_openid=member_openid,
        verdict=verdict,
        category=category,
        confidence=confidence,
        reason=reason,
        rule_hits=list(rule_hits or ()),
        recommended_actions=actions,
    )


def evaluate_voice(
    msg_message_id: str,
    group_openid: str,
    member_openid: str,
    attachment: Attachment,
    text_engine: TextRuleEngine,
) -> ModerationDecision:
    """语音审核：官方转写文本复用文字规则（D-013 asr_refer_text）。"""
    transcribed = attachment.asr_refer_text.strip()
    if not transcribed:
        return _decision(
            msg_message_id,
            group_openid,
            member_openid,
            "record_only",
            0.10,
            "语音无官方转写文本（asr_refer_text为空），只记录不判定",
        )
    # 以转写文本构造轻量判定：直接复用规则评分（黑名单+弱信号+联系方式）
    from app.moderation.rules import evaluate_text

    hits, confidence, category = evaluate_text(transcribed)
    verdict = "violation_high" if confidence >= 0.90 else "record_only"
    reason = f"语音官方转写判定（转写{len(transcribed)}字）"
    return _decision(
        msg_message_id,
        group_openid,
        member_openid,
        verdict,
        round(confidence, 2),
        reason,
        category,
        rule_hits=hits,
    )


def _probe_duration_seconds(path: Path) -> float | None:
    """ffprobe 读取视频时长；失败返回 None。"""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def _extract_video_frames(
    path: Path, out_dir: Path, max_frames: int = MAX_VIDEO_FRAMES
) -> list[Path]:
    """ffmpeg 抽帧（≤max_frames）到临时目录，返回帧文件列表。

    v1 采用顺序取前 N 帧：正常审核场景下视频开头代表性最强，且避免长视频
    全量解码；后续可在 T-203 优化为场景切分抽样。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    output_pattern = out_dir / "frame_%02d.png"
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(path),
                "-vf",
                "scale=320:-2",
                "-frames:v",
                str(max_frames),
                str(output_pattern),
            ],
            capture_output=True,
            timeout=FFMPEG_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return sorted(out_dir.glob("frame_*.png"))


def evaluate_video(
    msg_message_id: str,
    group_openid: str,
    member_openid: str,
    path: Path,
    image_engine: ImageModerationEngine,
    tmp_dir: Path,
) -> ModerationDecision:
    """视频审核：超大视频只告警；抽帧≤16帧后复用图片引擎聚合。"""
    size = path.stat().st_size if path.exists() else 0
    if size > MAX_VIDEO_BYTES:
        return _decision(
            msg_message_id,
            group_openid,
            member_openid,
            "record_only",
            0.10,
            f"视频{size}字节超过200MB上限，只告警不审核",
        )
    duration = _probe_duration_seconds(path)
    if duration is not None and duration > MAX_VIDEO_SECONDS:
        return _decision(
            msg_message_id,
            group_openid,
            member_openid,
            "record_only",
            0.10,
            f"视频时长{duration:.0f}秒超过10分钟上限，只告警不审核",
        )

    # Each video owns only its fresh child directory. Never reuse or delete the
    # caller's legacy shared frames; cleanup also runs after analysis exceptions.
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="video-", dir=tmp_dir) as frames_dir:
        frames = _extract_video_frames(path, Path(frames_dir))
        if not frames:
            return _decision(
                msg_message_id,
                group_openid,
                member_openid,
                "record_only",
                0.10,
                "视频抽帧失败（ffmpeg不可用或无输出），只记录不判定",
            )

        verdicts = [image_engine.analyze(frame).verdict for frame in frames]
        if any(v == "violation_high" for v in verdicts):
            return _decision(
                msg_message_id,
                group_openid,
                member_openid,
                "violation_high",
                0.95,
                f"视频{len(frames)}帧中存在违规帧",
                "ad",
            )
        # 抽帧未命中任何黑名单即放行（正常视频的帧大多无先验标签，不能因"未知"而阻塞）
        return _decision(
            msg_message_id,
            group_openid,
            member_openid,
            "allow",
            0.80,
            f"视频{len(frames)}帧未命中违规黑名单",
        )


def evaluate_file(
    msg_message_id: str,
    group_openid: str,
    member_openid: str,
    path: Path,
    text_engine: TextRuleEngine,
) -> ModerationDecision:
    """文件审核：只提取 PDF/纯文本内容走文字规则；其他类型仅元数据记录，绝不执行文件。"""
    suffix = path.suffix.lower()
    text = ""
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:20])
        elif suffix in (".txt", ".md", ".csv", ".log"):
            text = path.read_text(encoding="utf-8", errors="ignore")[:50_000]
        else:
            return _decision(
                msg_message_id,
                group_openid,
                member_openid,
                "record_only",
                0.10,
                f"文件类型{suffix}不提取内容，仅记录元数据",
            )
    except Exception as exc:  # noqa: BLE001 - 解析失败不处罚
        return _decision(
            msg_message_id,
            group_openid,
            member_openid,
            "record_only",
            0.10,
            f"文件内容提取失败: {type(exc).__name__}，只记录不判定",
        )

    if not text.strip():
        return _decision(
            msg_message_id,
            group_openid,
            member_openid,
            "record_only",
            0.10,
            "文件无可提取文本，只记录",
        )
    hits, confidence, category = _evaluate_rules_on_text(text)
    verdict = "violation_high" if confidence >= 0.90 else "record_only"
    return _decision(
        msg_message_id,
        group_openid,
        member_openid,
        verdict,
        round(confidence, 2),
        f"文件文本判定（提取{len(text)}字）",
        category,
        rule_hits=hits,
    )


def _evaluate_rules_on_text(
    text: str,
) -> tuple[list[RuleHit], float, str | None]:
    from app.moderation.rules import evaluate_text

    return evaluate_text(text)
