"""R-112 N05 回归：purge_drill 显式判定——残留/未清理必须失败（禁止假通过）。

主审复现点：旧版 action_logs 校验用固定日期窗口 `< '2026-01-01'`，而注入
时间为"200 天前"，故意不清理时残留 1 行仍报告 0。本回归锁定：
- 全部清理成功 → 无失败；
- 任何残留（尤其 action_logs）→ 非空失败列表（main 将返回非零）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

MOD_PATH = Path(__file__).resolve().parent.parent / "scripts" / "purge_drill.py"


def _load():
    spec = importlib.util.spec_from_file_location("purge_drill_r112", MOD_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_CLEAN: dict[str, object] = {
    "violation_old_snapshot": '{"purged": true, "reason": "raw_retention_expired"}',
    "violation_new_snapshot": '{"text": "未到期原文-应保留"}',
    "event_old_remaining": 0,
    "event_new_remaining": 1,
    "action_old_remaining": 0,
    "shadow_old_detail": '{"text_preview": ""}',
    "shadow_new_detail": '{"text_preview": "未到期影子原文-应保留"}',
    "feedback_old_text": None,
    "media_old_exists": False,
    "media_new_exists": True,
}


def test_clean_state_passes() -> None:
    mod = _load()
    assert mod._evaluate(dict(_CLEAN)) == []


def test_no_purge_residual_must_fail() -> None:
    """R-112 N05 假通过点：不执行 purge（行全残留）时必须报失败。"""
    mod = _load()
    residual = dict(_CLEAN)
    residual.update(
        {
            "violation_old_snapshot": '{"text": "DRILL原始违规原文-应被清除"}',
            "event_old_remaining": 1,
            "action_old_remaining": 1,
            "shadow_old_detail": '{"text_preview": "DRILL影子原文-应被清除"}',
            "feedback_old_text": "DRILL反馈原文-应被清除",
            "media_old_exists": True,
        }
    )
    failures = mod._evaluate(residual)
    assert failures
    assert any("action_old_remaining" in f for f in failures)


def test_partial_residual_isolated_failures() -> None:
    """单项残留只报该项——未到期对照被误删同样必须失败。"""
    mod = _load()
    only_media = dict(_CLEAN)
    only_media["media_old_exists"] = True
    failures = mod._evaluate(only_media)
    assert len(failures) == 1 and "media_old_exists" in failures[0]

    deleted_fresh = dict(_CLEAN)
    deleted_fresh["event_new_remaining"] = 0
    failures = mod._evaluate(deleted_fresh)
    assert any("event_new_remaining" in f for f in failures)


def test_shadow_new_missing_must_fail() -> None:
    """R-113 F04：未到期影子对照缺失（None 或误清空）必须失败。"""
    mod = _load()
    deleted = dict(_CLEAN)
    deleted["shadow_new_detail"] = None
    failures = mod._evaluate(deleted)
    assert any("shadow_new_detail" in f for f in failures)

    wiped = dict(_CLEAN)
    wiped["shadow_new_detail"] = '{"text_preview": ""}'
    failures = mod._evaluate(wiped)
    assert any("shadow_new_detail" in f for f in failures)
