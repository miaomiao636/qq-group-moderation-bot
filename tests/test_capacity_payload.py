"""R-112 N04 回归：压测 payload 的唯一文本必须真正用于发送内容。

主审复现点：旧版脚本生成了 `text #n` 但 204 行 payload 又取 random.choice，
unique=True 时实际唯一文本数=1，"无缓存最严苛"结论不成立。
"""

from __future__ import annotations

import importlib.util
import os
import random
from pathlib import Path

MOD_PATH = Path(__file__).resolve().parent.parent / "scripts" / "capacity_loadtest.py"


def _load():
    """加载压测脚本模块；模块顶层会设置环境变量，加载后立即恢复。"""
    backup = dict(os.environ)
    try:
        spec = importlib.util.spec_from_file_location("capacity_loadtest_r112", MOD_PATH)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(backup)
    return mod


def test_unique_text_really_unique() -> None:
    mod = _load()
    random.seed(11)
    texts = [mod._payload_text(i, True) for i in range(10)]
    assert len(set(texts)) == 10


def test_non_unique_uses_original_texts_only() -> None:
    mod = _load()
    random.seed(11)
    texts = [mod._payload_text(i, False) for i in range(10)]
    assert all(t in mod.TEXTS for t in texts)
