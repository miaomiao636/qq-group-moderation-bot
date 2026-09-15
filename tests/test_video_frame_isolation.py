"""R-107: each video's frames are isolated and removed on every exit path."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.moderation import media_engine


class FrameEngine:
    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.seen: list[Path] = []

    def analyze(self, frame: Path):
        self.seen.append(frame)
        if self.fails:
            raise RuntimeError("synthetic analyzer failure")
        content = frame.read_bytes()
        return SimpleNamespace(verdict="violation_high" if content == b"ad" else "allow")


def _video(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"synthetic video; subprocess is always replaced")
    return path


def _fake_process(monkeypatch, outcomes):
    """Replace both ffprobe and ffmpeg; outputs are synthetic local frame bytes."""
    directories: list[Path] = []
    outcome_iter = iter(outcomes)

    def run(args, **kwargs):
        if args[0] == "ffprobe":
            return subprocess.CompletedProcess(args, 0, stdout="1.0", stderr="")
        assert args[0] == "ffmpeg"
        pattern = Path(args[-1])
        directories.append(pattern.parent)
        frames, result = next(outcome_iter)
        for index, content in enumerate(frames, start=1):
            (pattern.parent / (pattern.name % index)).write_bytes(content)
        if isinstance(result, BaseException):
            raise result
        return subprocess.CompletedProcess(args, result, stdout=b"", stderr=b"")

    monkeypatch.setattr(media_engine.subprocess, "run", run)
    return directories


def _evaluate(path: Path, engine: FrameEngine, frames_root: Path):
    return media_engine.evaluate_video(
        "synthetic-message", "synthetic-group", "synthetic-member", path, engine, frames_root
    )


def test_shorter_second_video_does_not_inherit_first_video_ad_frame(tmp_path, monkeypatch):
    directories = _fake_process(monkeypatch, [([b"normal", b"ad"], 0), ([b"normal"], 0)])
    frames_root = tmp_path / "_frames"
    first = _evaluate(_video(tmp_path, "first.mp4"), FrameEngine(), frames_root)
    second = _evaluate(_video(tmp_path, "second.mp4"), FrameEngine(), frames_root)
    assert first.verdict == "violation_high"
    assert second.verdict == "allow"
    assert directories[0] != directories[1]
    assert all(folder.parent == frames_root and not folder.exists() for folder in directories)


def test_failed_second_video_cannot_reuse_earlier_ad_frame(tmp_path, monkeypatch):
    directories = _fake_process(monkeypatch, [([b"ad"], 0), ([], 1)])
    frames_root = tmp_path / "_frames"
    _evaluate(_video(tmp_path, "first.mp4"), FrameEngine(), frames_root)
    second_engine = FrameEngine()
    second = _evaluate(_video(tmp_path, "second.mp4"), second_engine, frames_root)
    assert second.verdict == "record_only"
    assert second.recommended_actions == []
    assert second_engine.seen == []
    assert all(not folder.exists() for folder in directories)


def test_nonzero_ffmpeg_discards_partial_output(tmp_path, monkeypatch):
    directories = _fake_process(monkeypatch, [([b"ad"], 1)])
    engine = FrameEngine()
    decision = _evaluate(_video(tmp_path, "partial.mp4"), engine, tmp_path / "_frames")
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []
    assert engine.seen == []
    assert all(not folder.exists() for folder in directories)


@pytest.mark.parametrize(
    "failure", [OSError("synthetic failure"), subprocess.TimeoutExpired("ffmpeg", 60)]
)
def test_extraction_exception_cleans_partial_frames(tmp_path, monkeypatch, failure):
    directories = _fake_process(monkeypatch, [([b"ad"], failure)])
    decision = _evaluate(_video(tmp_path, "broken.mp4"), FrameEngine(), tmp_path / "_frames")
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []
    assert all(not folder.exists() for folder in directories)


def test_analyzer_exception_cleans_frames(tmp_path, monkeypatch):
    directories = _fake_process(monkeypatch, [([b"ad"], 0)])
    with pytest.raises(RuntimeError, match="synthetic analyzer failure"):
        _evaluate(
            _video(tmp_path, "broken-analysis.mp4"), FrameEngine(fails=True), tmp_path / "_frames"
        )
    assert all(not folder.exists() for folder in directories)


def test_legacy_shared_frames_are_untouched_and_never_read(tmp_path, monkeypatch):
    directories = _fake_process(monkeypatch, [([b"normal"], 0)])
    frames_root = tmp_path / "_frames"
    frames_root.mkdir()
    legacy = frames_root / "frame_16.png"
    legacy.write_bytes(b"ad")
    decision = _evaluate(_video(tmp_path, "current.mp4"), FrameEngine(), frames_root)
    assert decision.verdict == "allow"
    assert legacy.read_bytes() == b"ad"
    assert all(not folder.exists() for folder in directories)
