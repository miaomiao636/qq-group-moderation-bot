"""T-102 动作适配器测试：全部使用 httpx MockTransport，不触网。"""

from __future__ import annotations

import json

import httpx
import pytest
from app.adapters.qq_official.actions import OfficialActionAdapter
from app.adapters.qq_official.auth import TokenError, TokenManager

GROUP = "GROUP_OPENID_TEST_1234"
MEMBER = "MEMBER_OPENID_TEST_5678"
MSG_ID = "ROBOT1.0_TEST_MESSAGE_ID"
TOKEN_RESP = {"access_token": "TEST_TOKEN", "expires_in": 7200}


class Responder:
    """令牌请求统一应答；动作请求按预设序列返回并记录。"""

    def __init__(self, responses: list[httpx.Response] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[httpx.Request] = []

    def route(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/app/getAppAccessToken"):
            return httpx.Response(200, json=TOKEN_RESP)
        self.calls.append(request)
        if not self.responses:
            raise AssertionError("没有预设响应可用")
        return self.responses.pop(0)


def make_adapter(
    responses: list[httpx.Response],
) -> tuple[OfficialActionAdapter, Responder, httpx.AsyncClient]:
    responder = Responder(responses)
    client = httpx.AsyncClient(transport=httpx.MockTransport(responder.route), timeout=10)
    token_manager = TokenManager("APP", "SECRET", client=client)
    adapter = OfficialActionAdapter(token_manager, api_base="https://api.bot.qq.com", client=client)
    return adapter, responder, client


async def teardown(adapter: OfficialActionAdapter, client: httpx.AsyncClient) -> None:
    await adapter.aclose()
    await client.aclose()


@pytest.mark.asyncio
async def test_recall_success() -> None:
    adapter, responder, client = make_adapter([httpx.Response(200, json={})])
    result = await adapter.recall(GROUP, MSG_ID)
    assert result.ok is True
    assert result.action == "recall"
    assert result.attempts == 1
    assert responder.calls[-1].method == "DELETE"
    await teardown(adapter, client)


@pytest.mark.asyncio
async def test_recall_duplicate_still_ok_idempotent() -> None:
    # D-012：重复撤回返回200，适配器视为成功
    adapter, _, client = make_adapter([httpx.Response(200, json={}), httpx.Response(200, json={})])
    assert (await adapter.recall(GROUP, MSG_ID)).ok
    assert (await adapter.recall(GROUP, MSG_ID)).ok
    await teardown(adapter, client)


@pytest.mark.asyncio
async def test_recall_permission_error_is_flagged() -> None:
    adapter, _, client = make_adapter(
        [
            httpx.Response(400, json={"code": 40062003, "message": "无操作权限"}),
        ]
    )
    result = await adapter.recall(GROUP, MSG_ID)
    assert result.ok is False
    assert result.err_code == 40062003
    assert result.is_permission_error is True
    await teardown(adapter, client)


@pytest.mark.asyncio
async def test_mute_body_uses_expire_time() -> None:
    adapter, responder, client = make_adapter([httpx.Response(200, json={})])
    result = await adapter.mute(GROUP, MEMBER, 3600)
    assert result.ok is True
    assert result.action == "mute"
    body = json.loads(responder.calls[-1].content)
    entry = body["members"][0]
    assert entry["op"] == "add"
    assert entry["member_openid"] == MEMBER
    # mute_expire_at 必须是带 Asia/Shanghai 时区的 RFC3339 到期时间
    assert entry["mute_expire_at"].endswith("+08:00")
    await teardown(adapter, client)


@pytest.mark.asyncio
async def test_mute_rejects_out_of_range_seconds() -> None:
    adapter, _, client = make_adapter([])
    for seconds in (0, -5, 30 * 24 * 3600 + 1):
        result = await adapter.mute(GROUP, MEMBER, seconds)
        assert result.ok is False
        assert result.attempts == 0  # 本地拒绝，未发起请求
    await teardown(adapter, client)


@pytest.mark.asyncio
async def test_mute_max_seconds_accepted() -> None:
    adapter, responder, client = make_adapter([httpx.Response(200, json={})])
    result = await adapter.mute(GROUP, MEMBER, 30 * 24 * 3600)
    assert result.ok is True
    assert len(responder.calls) == 1
    await teardown(adapter, client)


@pytest.mark.asyncio
async def test_mute_protected_role_returns_platform_error_verbatim() -> None:
    # D-012：群主/管理员禁言被平台拒绝（40103004），适配器原样返回供审计
    adapter, _, client = make_adapter(
        [
            httpx.Response(
                400,
                json={"code": 40103004, "message": "目标成员为机器人/群主/管理员，不允许被禁言"},
            ),
        ]
    )
    result = await adapter.mute(GROUP, MEMBER, 3600)
    assert result.ok is False
    assert result.is_permission_error is True
    assert "不允许被禁言" in result.err_message
    await teardown(adapter, client)


@pytest.mark.asyncio
async def test_unmute_uses_del_op() -> None:
    adapter, responder, client = make_adapter([httpx.Response(200, json={})])
    result = await adapter.unmute(GROUP, MEMBER)
    assert result.action == "unmute"
    body = json.loads(responder.calls[-1].content)
    assert body["members"][0]["op"] == "del"
    assert body["members"][0]["mute_expire_at"] == ""
    await teardown(adapter, client)


@pytest.mark.asyncio
async def test_warn_not_retried_on_network_error() -> None:
    # 警告不可重试：网络错误也只尝试一次，避免重复警告
    calls: list[httpx.Request] = []

    def route(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/app/getAppAccessToken"):
            return httpx.Response(200, json=TOKEN_RESP)
        raise httpx.ConnectError("boom", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(route), timeout=10)
    token_manager = TokenManager("APP", "SECRET", client=client)
    adapter = OfficialActionAdapter(token_manager, api_base="https://api.bot.qq.com", client=client)
    result = await adapter.warn(GROUP, MSG_ID, "警告：请遵守群规")
    assert result.ok is False
    assert result.attempts == 1
    assert result.action == "warn"
    assert len(calls) == 2  # 1次令牌 + 1次动作，无重试
    await teardown(adapter, client)


@pytest.mark.asyncio
async def test_warn_empty_text_rejected_locally() -> None:
    adapter, _, client = make_adapter([])
    result = await adapter.warn(GROUP, MSG_ID, "   ")
    assert result.ok is False
    assert result.attempts == 0
    await teardown(adapter, client)


def test_token_manager_requires_credentials() -> None:
    with pytest.raises(TokenError):
        TokenManager("", "")
