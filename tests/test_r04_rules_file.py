"""R04 回归：外置规则文件锚定项目根，缺失/为空 fail-closed（主审 13b9b1f 审查）。"""

import pytest
from app.config import Settings
from app.runtime.ai_wiring import _load_extra_rules, _resolve_rules_path


def _settings(raw: str) -> Settings:
    return Settings(ai_prompt_rules_file=raw, _env_file=None)


def test_relative_path_anchors_to_project_root(tmp_path, monkeypatch) -> None:
    """从任意目录启动，相对路径都必须解析到项目根下的同一文件。"""
    monkeypatch.chdir(tmp_path)
    resolved = _resolve_rules_path(_settings("config/ai_prompt_rules.txt"))
    assert resolved.is_file(), resolved


def test_missing_rules_file_fails_closed(tmp_path) -> None:
    with pytest.raises(ValueError, match="规则文件不存在"):
        _load_extra_rules(_settings(str(tmp_path / "nope.txt")))


def test_empty_rules_file_fails_closed(tmp_path) -> None:
    empty = tmp_path / "empty.txt"
    empty.write_text("   \n", encoding="utf-8")
    with pytest.raises(ValueError, match="规则文件为空"):
        _load_extra_rules(_settings(str(empty)))


def test_real_rules_file_loads_and_contains_policy() -> None:
    content = _load_extra_rules(_settings("config/ai_prompt_rules.txt"))
    assert "校园墙" in content
    assert "办证" in content


def test_absolute_path_is_preserved(tmp_path) -> None:
    target = tmp_path / "rules.txt"
    target.write_text("规则A", encoding="utf-8")
    resolved = _resolve_rules_path(_settings(str(target)))
    assert resolved == target
    assert _load_extra_rules(_settings(str(target))) == "规则A"
