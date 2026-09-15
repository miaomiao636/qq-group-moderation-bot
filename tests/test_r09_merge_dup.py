"""R09 回归：重复 sample_id 冲突必须报错，不得静默覆盖（主审 13b9b1f 审查）。"""

import json

import pytest
from scripts.w2_sample_merge import _load_unique


def _write(tmp_path, rows, name="labels.jsonl"):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def test_conflicting_duplicate_raises(tmp_path) -> None:
    path = _write(
        tmp_path,
        [
            {"sample_id": "s1", "label": "confirmed_violation"},
            {"sample_id": "s1", "label": "confirmed_normal"},
        ],
    )
    with pytest.raises(SystemExit, match="冲突"):
        _load_unique(path, "labels")


def test_identical_duplicate_is_tolerated(tmp_path) -> None:
    path = _write(
        tmp_path,
        [
            {"sample_id": "s1", "label": "confirmed_violation"},
            {"sample_id": "s1", "label": "confirmed_violation"},
        ],
    )
    merged = _load_unique(path, "labels")
    assert list(merged) == ["s1"]


def test_unique_rows_all_kept(tmp_path) -> None:
    path = _write(
        tmp_path,
        [
            {"sample_id": "s1", "label": "confirmed_violation"},
            {"sample_id": "s2", "label": "confirmed_normal"},
        ],
    )
    assert len(_load_unique(path, "labels")) == 2
