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
async def test_unmute_uses_del_op() -> None:
    adapter, responder, client = make_adapter([httpx.Response(200, json={})])
    result = await adapter.unmute(GROUP, MEMBER)
    assert result.action == "unmute"
    body = json.loads(responder.calls[-1].content)
    assert body["members"][0]["op"] == "del"
    assert body["members"][0]["mute_expire_at"] == ""
    await teardown(adapter, client)


@pytest.mark.asyncio
async def test_official_adapter_exposes_no_automatic_mute_or_warning() -> None:
    adapter, responder, client = make_adapter([])
    assert not hasattr(adapter, "mute")
    assert not hasattr(adapter, "warn")
    assert responder.calls == []
    await teardown(adapter, client)


def test_token_manager_requires_credentials() -> None:
    with pytest.raises(TokenError):
        TokenManager("", "")
