"""图片/GIF 审核引擎（T-201）。

判定逻辑（与 `docs/group-rules.md` 负责人确认的规则一致）：
1. 感知哈希命中**违规图黑名单**（负责人确认的无码广告图）→ 高置信违规；
2. 感知哈希命中**校园墙允许白名单**（负责人确认的带码海报）→ 放行；
3. 图中解码出二维码且载荷在二维码白名单 → 放行信号；
4. 解码出二维码但载荷不在白名单 → 广告引流信号（中等风险，不自动处罚）；
5. 存在"长按识别小程序码"等校园墙分享文案 → 来源放行信号；
6. 以上均无法判定（OCR/解码不可用、证据不足）→ **record_only，绝不自动处罚**（分析失败不触发处罚）。

聚合：GIF 抽最多 8 帧，任一帧命中黑名单即违规；全部帧白名单才放行。
缓存：以 dHash 为键复用判定结果，避免重复计算。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.core.contracts import StandardMessage
from app.moderation.imaging import (
    decode_qr_codes,
    dhash,
    image_frames_from_source,
    similar,
)

MediaVerdict = Literal["allow", "record_only", "violation_high"]

_CAMPUS_WALL_MARKERS = ("长按识别小程序码", "一起看吧")

# 与文字规则一致的处置动作（永无 kick）
_HIGH_ACTIONS: tuple[str, ...] = ("recall", "mute", "warn")


@dataclass
class MediaAnalysis:
    verdict: MediaVerdict
    confidence: float
    hashes: list[str] = field(default_factory=list)
    qr_payloads: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    cache_hit: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "confidence": self.confidence,
            "hashes": self.hashes,
            "qr_payloads_masked": [p[:8] + "..." for p in self.qr_payloads],
            "signals": self.signals,
            "cache_hit": self.cache_hit,
            "reason": self.reason,
        }


class ImageModerationEngine:
    """图片/GIF 审核引擎。

    Args:
        violation_hashes: 负责人确认的违规图哈希黑名单。
        allowed_hashes: 负责人确认的允许图（校园墙海报）哈希白名单。
        qr_whitelist: 允许的二维码载荷精确匹配集合。
    """

    def __init__(
        self,
        violation_hashes: set[str] | None = None,
        allowed_hashes: set[str] | None = None,
        qr_whitelist: set[str] | None = None,
    ) -> None:
        self._violation_hashes = violation_hashes or set()
        self._allowed_hashes = allowed_hashes or set()
        self._qr_whitelist = qr_whitelist or set()
        self._cache: dict[str, MediaAnalysis] = {}

    @property
    def violation_hashes(self) -> set[str]:
        return set(self._violation_hashes)

    @property
    def allowed_hashes(self) -> set[str]:
        return set(self._allowed_hashes)

    def add_violation_hash(self, image_hash: str) -> None:
        self._violation_hashes.add(image_hash)

    def add_allowed_hash(self, image_hash: str) -> None:
        self._allowed_hashes.add(image_hash)

    def _hash_verdict(self, image_hash: str) -> MediaVerdict | None:
        """精确/相似匹配黑名单与白名单哈希。"""
        if image_hash in self._violation_hashes or any(
            similar(image_hash, vh) for vh in self._violation_hashes
        ):
            return "violation_high"
        if image_hash in self._allowed_hashes or any(
            similar(image_hash, ah) for ah in self._allowed_hashes
        ):
            return "allow"
        return None

    def analyze(self, source: str | bytes | Path) -> MediaAnalysis:
        """分析单张图片/GIF 文件或字节。

        R-102-5：缓存键使用**完整帧哈希集合**，避免 GIF 仅以第一帧为准导致
        不同动图因首帧相同而误命中。
        """
        import hashlib

        frames = image_frames_from_source(source)
        hashes = [dhash(f) for f in frames]
        cache_key = hashlib.sha256("|".join(hashes).encode("utf-8")).hexdigest()
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            return MediaAnalysis(
                verdict=cached.verdict,
                confidence=cached.confidence,
                hashes=hashes,
                qr_payloads=cached.qr_payloads,
                signals=cached.signals,
                cache_hit=True,
                reason=cached.reason + "（缓存命中）",
            )

        qr_payloads: list[str] = []
        for frame in frames:
            qr_payloads.extend(decode_qr_codes(frame))
        qr_payloads = list(dict.fromkeys(qr_payloads))

        signals: list[str] = []
        # 逐帧哈希判定：任一帧黑名单即违规；全帧白名单才直接放行
        hash_verdicts = [self._hash_verdict(h) for h in hashes]
        if any(v == "violation_high" for v in hash_verdicts):
            analysis = MediaAnalysis(
                "violation_high",
                0.95,
                hashes,
                qr_payloads,
                ["黑名单图哈希命中"],
                reason="命中负责人确认的违规图黑名单",
            )
            self._cache[cache_key] = analysis
            return analysis
        if hashes and all(v == "allow" for v in hash_verdicts):
            analysis = MediaAnalysis(
                "allow",
                0.90,
                hashes,
                qr_payloads,
                ["白名单图哈希命中"],
                reason="命中负责人确认的允许图白名单",
            )
            self._cache[cache_key] = analysis
            return analysis

        # 二维码信号
        if qr_payloads:
            whitelisted = [p for p in qr_payloads if p in self._qr_whitelist]
            unknown = [p for p in qr_payloads if p not in self._qr_whitelist]
            if whitelisted and not unknown:
                signals.append("二维码载荷在白名单")
            elif unknown:
                signals.append(f"存在不在白名单的二维码{len(unknown)}个")

        # 不可解析码（如微信小程序码）：无法判定载荷，来源未知
        verdict: MediaVerdict = "record_only"
        confidence = 0.30 if signals else 0.10
        reason = "；".join(signals) if signals else "未命中黑/白名单，证据不足，转人工"
        analysis = MediaAnalysis(verdict, confidence, hashes, qr_payloads, signals, reason=reason)
        self._cache[cache_key] = analysis
        return analysis


def merge_decisions(text_decision: Any, media: MediaAnalysis | None) -> Any:
    """合并文字决策与媒体判定（T-201 聚合层）。

    规则：
    - 任一层 violation_high => violation_high（建议 recall/mute/warn）；
    - 媒体 allow 且文字无信号 => allow；
    - 其余 => record_only（分析失败或证据不足绝不触发处罚）。
    """
    if media is None or media.verdict == "record_only":
        if text_decision.verdict == "violation_high":
            return text_decision
        return text_decision  # record_only/allow 维持
    if media.verdict == "violation_high":
        text_decision.verdict = "violation_high"
        text_decision.confidence = max(text_decision.confidence, media.confidence)
        text_decision.recommended_actions = list(_HIGH_ACTIONS)
        text_decision.reason = (
            text_decision.reason + "；" if text_decision.reason else ""
        ) + media.reason
        return text_decision
    # media allow：若文字已是违规则维持违规，否则放行
    if text_decision.verdict == "violation_high":
        return text_decision
    text_decision.verdict = "allow"
    text_decision.reason = (
        text_decision.reason + "；" if text_decision.reason else ""
    ) + "媒体判定放行"
    return text_decision


def evaluate_message_media(
    engine: ImageModerationEngine, msg: StandardMessage, media_dir: Path
) -> MediaAnalysis | None:
    """对带附件的统一消息做媒体审核：逐附件分析并聚合。

    附件需已由调用方即时下载到 media_dir（D-013 URL时效约束）。
    任一附件违规即整体违规；全部无法判定则返回 record_only 汇总。
    """
    results: list[MediaAnalysis] = []
    for att in msg.attachments:
        if not att.content_type.startswith(("image/", "voice", "video")):
            continue
        # 文件名被遮蔽时无法对应本地文件，跳过（由下载层提供映射）
        path = media_dir / att.filename
        if not path.exists():
            continue
        results.append(engine.analyze(path))
    if not results:
        return None
    if any(r.verdict == "violation_high" for r in results):
        return MediaAnalysis(
            "violation_high", max(r.confidence for r in results), reason="多附件聚合：存在违规图"
        )
    if all(r.verdict == "allow" for r in results):
        return MediaAnalysis("allow", 0.90, reason="多附件聚合：全部放行")
    return MediaAnalysis("record_only", 0.30, reason="多附件聚合：证据不足转人工")
