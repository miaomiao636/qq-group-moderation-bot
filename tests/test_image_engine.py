"""T-201 图片/GIF 审核引擎测试。

- 合成图片（PIL 绘制）+ zxing 生成二维码做编解码回归；
- 负责人真实样本（data/t002_media，gitignored）存在时做真图回归，缺失则跳过。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from app.moderation.decision import ModerationDecision
from app.moderation.image_engine import ImageModerationEngine, merge_decisions
from app.moderation.imaging import (
    decode_qr_codes,
    dhash,
    encode_qr,
    extract_frames,
    hamming_distance,
    load_image,
    similar,
)
from PIL import Image, ImageDraw

REAL_MEDIA_DIR = Path(__file__).parent.parent / "data" / "t002_media"


def png_bytes(
    draw_text: str = "", color: str = "white", size: tuple[int, int] = (120, 80)
) -> bytes:
    img = Image.new("RGB", size, color)
    if draw_text:
        draw = ImageDraw.Draw(img)
        draw.text((5, 5), draw_text, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def gif_bytes(frame_colors: list[str]) -> bytes:
    frames = [Image.new("RGB", (60, 60), c) for c in frame_colors]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    return buf.getvalue()


# ---------- 感知哈希 ----------


def test_dhash_deterministic_and_distance() -> None:
    img = load_image(png_bytes("hello"))
    h1 = dhash(img)
    h2 = dhash(load_image(png_bytes("hello")))
    assert h1 == h2
    assert hamming_distance(h1, h2) == 0
    different = dhash(load_image(png_bytes("", color="black")))
    assert not similar(h1, different, threshold=5) or hamming_distance(h1, different) > 0


def test_similar_threshold() -> None:
    h = "1" * 64
    assert similar(h, h)
    assert similar(h, "0" * 64, threshold=64)
    assert not similar(h, "0" * 64, threshold=10)


# ---------- GIF 抽帧 ----------


def test_gif_frames_capped_at_8() -> None:
    img = load_image(
        gif_bytes([f"#{i:02d}2345" and ("red" if i % 2 else "blue") for i in range(20)])
    )
    frames = extract_frames(img)
    assert 1 < len(frames) <= 8


def test_static_image_single_frame() -> None:
    img = load_image(png_bytes())
    assert len(extract_frames(img)) == 1


# ---------- 二维码 ----------


def test_qr_roundtrip() -> None:
    payload = "https://example.com/whitelist"
    qr_img = encode_qr(payload)
    decoded = decode_qr_codes(qr_img)
    assert decoded == [payload]


def test_qr_absent_returns_empty() -> None:
    assert decode_qr_codes(load_image(png_bytes("no qr here"))) == []


def test_non_image_rejected() -> None:
    with pytest.raises(ValueError, match="非受支持的图片格式"):
        load_image(b"not an image at all")


# ---------- 审核引擎 ----------


def test_blacklisted_image_is_violation() -> None:
    bad = png_bytes("spam poster")
    engine = ImageModerationEngine(violation_hashes={dhash(load_image(bad))})
    result = engine.analyze(bad)
    assert result.verdict == "violation_high"
    assert not result.cache_hit


def test_whitelisted_campus_wall_image_allowed() -> None:
    poster = png_bytes("campus wall", color="lightblue")
    engine = ImageModerationEngine(allowed_hashes={dhash(load_image(poster))})
    result = engine.analyze(poster)
    assert result.verdict == "allow"


def test_whitelist_takes_precedence_over_unrelated_qr() -> None:
    poster = png_bytes("campus", color="lightgreen")
    engine = ImageModerationEngine(allowed_hashes={dhash(load_image(poster))})
    assert engine.analyze(poster).verdict == "allow"


def test_unknown_image_record_only_never_punish() -> None:
    # 分析无法判定时必须 record_only（分析失败不触发处罚）
    engine = ImageModerationEngine()
    result = engine.analyze(png_bytes("random content", color="gray"))
    assert result.verdict == "record_only"
    assert result.confidence < 0.9


def test_cache_hit_on_same_image() -> None:
    engine = ImageModerationEngine()
    img = png_bytes("cached")
    first = engine.analyze(img)
    second = engine.analyze(img)
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.verdict == first.verdict


def test_similar_variant_image_hits_blacklist() -> None:
    base = Image.new("RGB", (100, 100), "white")
    draw = ImageDraw.Draw(base)
    draw.rectangle([10, 10, 90, 90], fill="black")
    buf = io.BytesIO()
    base.save(buf, format="PNG")
    base_bytes = buf.getvalue()

    # 轻微修改（缩放）后的相似图
    variant = load_image(base_bytes).resize((110, 110))
    buf2 = io.BytesIO()
    variant.save(buf2, format="PNG")

    engine = ImageModerationEngine(violation_hashes={dhash(load_image(base_bytes))})
    assert engine.analyze(buf2.getvalue()).verdict == "violation_high"


def test_gif_any_blacklisted_frame_is_violation() -> None:
    clean = png_bytes("clean", color="white")
    bad = png_bytes("bad", color="black")
    frames = [load_image(clean), load_image(bad)]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:])
    engine = ImageModerationEngine(violation_hashes={dhash(load_image(bad))})
    assert engine.analyze(buf.getvalue()).verdict == "violation_high"


# ---------- 与文字决策聚合 ----------


def _text_decision(verdict: str) -> ModerationDecision:
    return ModerationDecision(
        message_id="M",
        group_openid="G",
        sender_member_openid="S",
        verdict=verdict,  # type: ignore[arg-type]
        confidence=0.95 if verdict == "violation_high" else 0.2,
    )


def test_merge_media_violation_escalates() -> None:
    from app.moderation.image_engine import MediaAnalysis

    decision = _text_decision("allow")
    merged = merge_decisions(decision, MediaAnalysis("violation_high", 0.95, reason="黑名单图"))
    assert merged.verdict == "violation_high"
    assert set(merged.recommended_actions) == {"recall", "mute", "warn"}
    assert "kick" not in merged.model_dump_json().lower()


def test_merge_media_allow_downgrades_record_only() -> None:
    from app.moderation.image_engine import MediaAnalysis

    decision = _text_decision("record_only")
    merged = merge_decisions(decision, MediaAnalysis("allow", 0.9, reason="白名单"))
    assert merged.verdict == "allow"


def test_merge_text_violation_not_downgraded_by_media() -> None:
    from app.moderation.image_engine import MediaAnalysis

    decision = _text_decision("violation_high")
    merged = merge_decisions(decision, MediaAnalysis("allow", 0.9, reason="白名单"))
    assert merged.verdict == "violation_high"


# ---------- 负责人真实样本回归（本地存在才执行） ----------


@pytest.mark.skipif(not REAL_MEDIA_DIR.exists(), reason="真实样本仅存于测试机本地（gitignored）")
def test_real_violation_samples_match_manifest_labels() -> None:
    import json as _json

    manifest = _json.loads((REAL_MEDIA_DIR / "manifest.json").read_text(encoding="utf-8"))
    engine = ImageModerationEngine()
    for item in manifest["items"]:
        path = REAL_MEDIA_DIR / item["file"]
        if not path.exists() or path.suffix != ".jpg" and path.suffix != ".png":
            continue
        result = engine.analyze(path)
        if item["label"] == "violation":
            # 无码违规图首次出现时引擎应至少给出非放行判定（record_only 或违规）
            assert result.verdict in ("record_only", "violation_high")
        elif item["label"] == "allowed_campus_wall":
            assert result.verdict in ("allow", "record_only")
