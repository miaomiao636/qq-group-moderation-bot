"""S09/S10 回归：AI 禁用分支先返回；群名读取 data 层级 + 失败允许退避重试。"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from app.config import Settings
from app.db import SessionLocal


def test_s09_disabled_ai_ignores_missing_rules_file(monkeypatch) -> None:
    """关闭 AI 时，残留的失效规则路径不得阻断纯本地规则处理。"""
    from app.runtime import ai_wiring

    settings = Settings(
        ai_enabled=False,
        ai_prompt_rules_file="config/__definitely_missing_rules__.txt",
        ai_enabled_groups="*",
        _env_file=None,
    )
    monkeypatch.setattr(ai_wiring, "get_settings", lambda: settings)
    ai_wiring.build_default_ai_review_service.cache_clear()
    try:
        service = ai_wiring.build_default_ai_review_service()
        assert service.enabled is False
    finally:
        ai_wiring.build_default_ai_review_service.cache_clear()


def test_s09_enabled_ai_still_fails_closed_on_missing_rules(monkeypatch) -> None:
    """启用 AI 时缺规则仍必须拒绝——不能恢复旧版静默忽略规则。"""
    from app.runtime import ai_wiring

    settings = Settings(
        ai_enabled=True,
        ai_prompt_rules_file="config/__definitely_missing_rules__.txt",
        ai_enabled_groups="*",
        _env_file=None,
    )
    monkeypatch.setattr(ai_wiring, "get_settings", lambda: settings)
    ai_wiring.build_default_ai_review_service.cache_clear()
    try:
        with pytest.raises(ValueError, match="规则文件不存在"):
            ai_wiring.build_default_ai_review_service()
    finally:
        ai_wiring.build_default_ai_review_service.cache_clear()


def _patch_hub(monkeypatch, response):
    from app.runtime import onebot_actions

    async def fake_call(action: str, params: dict):
        return response

    monkeypatch.setattr(onebot_actions.onebot_action_hub, "call", fake_call)


@pytest.mark.asyncio
async def test_s10_group_name_read_from_data_layer(monkeypatch) -> None:
    """成功响应时群名在 data.group_name，必须读到并写入备注。"""
    from app.runtime import onebot_wiring

    group_id = f"9{uuid.uuid4().int % 10**8:08d}"
    _patch_hub(
        monkeypatch,
        {"status": "ok", "retcode": 0, "data": {"group_name": "自动备注测试群"}},
    )
    ok = await onebot_wiring._autoname_group_task(group_id)
    assert ok is True
    from app.models import GroupAlias

    async with SessionLocal() as session:
        alias = await session.get(GroupAlias, group_id)
        assert alias is not None
        assert alias.name == "自动备注测试群"


@pytest.mark.asyncio
async def test_s10_non_ok_response_is_retryable(monkeypatch) -> None:
    """非成功响应（retcode != 0）视为可重试失败，不写备注。"""
    from app.runtime import onebot_wiring

    group_id = f"8{uuid.uuid4().int % 10**8:08d}"
    _patch_hub(monkeypatch, {"status": "failed", "retcode": 1404, "data": None})
    ok = await onebot_wiring._autoname_group_task(group_id)
    assert ok is False
    from app.models import GroupAlias

    async with SessionLocal() as session:
        assert await session.get(GroupAlias, group_id) is None


@pytest.mark.asyncio
async def test_s10_failed_attempt_is_released_for_bounded_retry(monkeypatch) -> None:
    """失败后 groups 从 _alias_attempted 释放，并由退避表控制下一次重试。"""
    from app.runtime import onebot_wiring

    group_id = f"7{uuid.uuid4().int % 10**8:08d}"
    _patch_hub(monkeypatch, {"status": "failed", "retcode": 1404, "data": None})
    onebot_wiring._alias_attempted.add(group_id)
    onebot_wiring._alias_retry_after.pop(group_id, None)
    await onebot_wiring._autoname_with_retry_scope(group_id)
    assert group_id not in onebot_wiring._alias_attempted
    assert onebot_wiring._alias_retry_after.get(group_id, 0.0) > 0.0
    onebot_wiring._alias_retry_after.pop(group_id, None)


def test_s04_label_page_defines_category_store() -> None:
    """S04：盲标页脚本必须先定义类别存储 C，render 才能正常首轮渲染。"""
    source = (Path(__file__).resolve().parents[1] / "scripts" / "w2_label_page.py").read_text(
        encoding="utf-8"
    )
    let_c = source.index("let C=load(KEY+'_cat')")
    render_fn = source.index("function render()")
    assert let_c < render_fn
    assert "C[d.sample_id]" in source
