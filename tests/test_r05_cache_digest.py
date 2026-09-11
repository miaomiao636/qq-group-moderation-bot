"""R05 回归：外置业务规则变化必须改变缓存指纹（主审 13b9b1f 审查）。"""

from app.adapters.ai.openai_compatible import OpenAICompatibleTextModerator
from app.moderation.ai import AIModerationRequest, ai_cache_key


def _mod(rules: str) -> OpenAICompatibleTextModerator:
    return OpenAICompatibleTextModerator(
        base_url="https://example",
        api_key="test-only",
        model_id="fake-model",
        extra_system_rules=rules,
    )


def _key(mod: OpenAICompatibleTextModerator) -> str:
    req = AIModerationRequest(message_id="x", group_openid="g", content_kind="text", text="t")
    req = req.model_copy(update={"policy_context": req.policy_context + "|" + mod.prompt_digest})
    return ai_cache_key(req, model_id=mod.model_id, prompt_version=mod.prompt_version)


def test_rules_change_changes_cache_key() -> None:
    """规则 A→B 后缓存键必须不同，否则重启仍命中旧判定（R05 复现场景）。"""
    assert _key(_mod("规则A")) != _key(_mod("规则B"))


def test_same_rules_same_cache_key() -> None:
    """规则不变则缓存可复用（不得误伤正常缓存收益）。"""
    assert _key(_mod("规则A")) == _key(_mod("规则A"))


def test_prompt_digest_covers_effective_system_prompt() -> None:
    mod = _mod("规则A")
    assert "规则A" in mod.system_prompt
    assert len(mod.prompt_digest) == 16
    # 通用提示词本身不含业务规则词（防泄漏护栏），摘要必须由外置规则驱动
    assert (
        mod.prompt_digest
        != OpenAICompatibleTextModerator(
            base_url="https://example", api_key="test-only", model_id="fake-model"
        ).prompt_digest
    )
