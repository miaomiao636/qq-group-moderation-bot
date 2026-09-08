"""T-306 OneBot 反向WebSocket测试：鉴权、事件处理、去重、断线重连、就绪状态。

全部使用脱敏fixture与测试令牌；影子模式外部管理动作调用数断言为0。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from app.main import app
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

WS_PATH = "/onebot/ws"
TOKEN = "test-onebot-token"
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "onebot"


def _load_event(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))["event"]


def _wait_for(predicate: Any, timeout: float = 15.0, interval: float = 0.1) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _lifecycle(self_id: int = 10000001) -> str:
    return json.dumps(
        {
            "time": 1789000000,
            "self_id": self_id,
            "post_type": "meta_event",
            "meta_event_type": "lifecycle",
            "sub_type": "connect",
        }
    )


def _heartbeat(self_id: int = 10000001) -> str:
    return json.dumps(
        {
            "time": 1789000001,
            "self_id": self_id,
            "post_type": "meta_event",
            "meta_event_type": "heartbeat",
            "status": {"online": True},
        }
    )


# ---------- 鉴权 ----------


@contextmanager
def _expect_disconnect() -> Any:
    with pytest.raises(WebSocketDisconnect):
        yield


def test_missing_token_rejected() -> None:
    from app.main import app

    with TestClient(app) as client, _expect_disconnect(), client.websocket_connect(WS_PATH):
        pass


def test_wrong_token_rejected() -> None:
    with (
        TestClient(app) as client,
        _expect_disconnect(),
        client.websocket_connect(f"{WS_PATH}?access_token=wrong-token"),
    ):
        pass


# ---------- 就绪状态与健康检查 ----------


def test_status_ready_when_connected_and_heartbeat_fresh() -> None:
    with TestClient(app) as client:
        assert client.get("/healthz").json()["status"] == "ok"
        with client.websocket_connect(f"{WS_PATH}?access_token={TOKEN}") as ws:
            ws.send_text(_lifecycle())
            ws.send_text(_heartbeat())
            snap = client.get("/onebot/status").json()
            assert snap["connected"] is True
            assert snap["login_state"] == "online"
            assert snap["self_id"] == "10000001"
            assert snap["state"] == "ready"
            # healthz 暴露同一就绪证据；进程存活不等于就绪
            health = client.get("/healthz").json()
            assert health["onebot"]["state"] == "ready"


def test_status_degraded_after_disconnect() -> None:
    with TestClient(app) as client:
        with client.websocket_connect(f"{WS_PATH}?access_token={TOKEN}") as ws:
            ws.send_text(_lifecycle())
            ws.send_text(_heartbeat())
        assert _wait_for(lambda: client.get("/onebot/status").json()["connected"] is False)
        snap = client.get("/onebot/status").json()
        assert snap["state"] == "degraded"
        assert snap["login_state"] == "offline"
        assert snap["disconnect_count"] >= 1


# ---------- 影子处理主流程 ----------


def test_text_message_shadow_processed_with_dedup_and_zero_actions() -> None:
    from app.actions.orchestrator import ActionIntent
    from app.db import SessionLocal
    from app.models import ProcessedEvent
    from app.runtime.models import ShadowDecision
    from sqlalchemy import func, select

    message_id = f"9100{uuid.uuid4().int % 10**8}"
    self_id = 10000001
    dedup_key = f"onebot:{self_id}:{message_id}"

    async def _assert_db() -> None:
        async with SessionLocal() as session:
            decision = (
                await session.execute(
                    select(ShadowDecision).where(ShadowDecision.message_id == message_id)
                )
            ).scalar_one()
            assert decision.provider == "onebot"
            assert decision.external_group_id == "300000001"
            assert decision.external_user_id == "200000001"
            assert decision.verdict in ("violation_high", "record_only")

            processed = (
                await session.execute(
                    select(ProcessedEvent).where(ProcessedEvent.message_id == dedup_key)
                )
            ).scalar_one()
            assert processed.provider == "onebot"

            intents = (
                await session.execute(
                    select(func.count())
                    .select_from(ActionIntent)
                    .where(ActionIntent.external_group_id == "300000001")
                )
            ).scalar_one()
            assert intents == 0, "影子模式：OneBot事件不得产生任何动作意图"

    with TestClient(app) as client:
        with client.websocket_connect(f"{WS_PATH}?access_token={TOKEN}") as ws:
            ws.send_text(_lifecycle(self_id))
            ws.send_text(_heartbeat(self_id))
            event = _load_event("group_message_text.json")
            event["message_id"] = int(message_id)
            ws.send_text(json.dumps(event))

        def _done() -> bool:
            async def _count() -> int:
                async with SessionLocal() as session:
                    return int(
                        (
                            await session.execute(
                                select(func.count())
                                .select_from(ShadowDecision)
                                .where(ShadowDecision.message_id == message_id)
                            )
                        ).scalar_one()
                    )

            return _run(_count()) == 1

        assert _wait_for(_done), "事件未在超时内被影子处理"
        _run(_assert_db())


def test_duplicate_and_reconnect_not_reprocessed() -> None:
    from app.core.dedup import reset_memory_cache
    from app.db import SessionLocal
    from app.runtime.models import ShadowDecision
    from sqlalchemy import func, select

    message_id = f"9100{uuid.uuid4().int % 10**8}"
    self_id = 10000001
    event = _load_event("group_message_text.json")
    event["message_id"] = int(message_id)
    event["self_id"] = self_id
    frame = json.dumps(event)

    def _decision_count() -> int:
        async def _count() -> int:
            async with SessionLocal() as session:
                return int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(ShadowDecision)
                            .where(ShadowDecision.message_id == message_id)
                        )
                    ).scalar_one()
                )

        return _run(_count())

    with TestClient(app) as client:
        with client.websocket_connect(f"{WS_PATH}?access_token={TOKEN}") as ws:
            ws.send_text(_lifecycle(self_id))
            ws.send_text(_heartbeat(self_id))
            ws.send_text(frame)
            ws.send_text(frame)  # 同连接重复推送
        assert _wait_for(lambda: _decision_count() == 1)
        # 模拟进程重启（内存去重缓存清空）后断线重连重放
        reset_memory_cache()
        with client.websocket_connect(f"{WS_PATH}?access_token={TOKEN}") as ws:
            ws.send_text(_lifecycle(self_id))
            ws.send_text(_heartbeat(self_id))
            ws.send_text(frame)
        time.sleep(1.0)
        assert _decision_count() == 1, "重连/重启后重复事件不得重复处理"


def test_invalid_json_keeps_connection_alive() -> None:
    with (
        TestClient(app) as client,
        client.websocket_connect(f"{WS_PATH}?access_token={TOKEN}") as ws,
    ):
        before = client.get("/onebot/status").json()["invalid_total"]
        ws.send_text("not-a-json-frame")
        ws.send_text(_heartbeat())
        assert _wait_for(lambda: client.get("/onebot/status").json()["invalid_total"] >= before + 1)
        snap = client.get("/onebot/status").json()
        assert snap["connected"] is True, "单条非法帧不应断开连接"


def test_invalid_structure_counted_not_processed() -> None:
    from app.db import SessionLocal
    from app.runtime.models import ShadowDecision
    from sqlalchemy import func, select

    event = _load_event("event_invalid_missing_group.json")
    with TestClient(app) as client:
        before = client.get("/onebot/status").json()["invalid_total"]
        with client.websocket_connect(f"{WS_PATH}?access_token={TOKEN}") as ws:
            ws.send_text(_lifecycle())
            ws.send_text(_heartbeat())
            ws.send_text(json.dumps(event))
            time.sleep(0.5)
        assert client.get("/onebot/status").json()["invalid_total"] >= before + 1

    async def _count() -> int:
        async with SessionLocal() as session:
            return int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(ShadowDecision)
                        .where(ShadowDecision.message_id == "910000011")
                    )
                ).scalar_one()
            )

    assert _run(_count()) == 0, "结构非法事件不得产生判定记录"


# ---------- 媒体下载失败与未知内容降级 ----------


def test_media_download_failure_degrades_to_record_only() -> None:
    from app.db import SessionLocal
    from app.runtime.models import ShadowDecision
    from sqlalchemy import select

    message_id = f"9100{uuid.uuid4().int % 10**8}"
    event = _load_event("group_message_image.json")
    event["message_id"] = int(message_id)
    # 不可达端口：模拟媒体下载失败
    event["message"][0]["data"]["url"] = "http://127.0.0.1:9/unreachable.jpg"

    with TestClient(app) as client:
        with client.websocket_connect(f"{WS_PATH}?access_token={TOKEN}") as ws:
            ws.send_text(_lifecycle())
            ws.send_text(_heartbeat())
            ws.send_text(json.dumps(event))

        def _record() -> ShadowDecision | None:
            async def _fetch() -> ShadowDecision | None:
                async with SessionLocal() as session:
                    return (
                        (
                            await session.execute(
                                select(ShadowDecision).where(
                                    ShadowDecision.message_id == message_id
                                )
                            )
                        )
                        .scalars()
                        .first()
                    )

            return _run(_fetch())

        assert _wait_for(lambda: _record() is not None)
        record = _record()
        assert record is not None
        assert record.verdict == "record_only"
        assert "转人工" in record.reason


def test_file_without_url_and_unknown_segment_degrade() -> None:
    from app.db import SessionLocal
    from app.runtime.models import ShadowDecision
    from sqlalchemy import select

    cases = [
        ("group_message_file.json", "910000006"),
        ("group_message_unknown_segment.json", "910000010"),
        ("group_message_forward.json", "910000008"),
    ]
    with TestClient(app) as client:
        with client.websocket_connect(f"{WS_PATH}?access_token={TOKEN}") as ws:
            ws.send_text(_lifecycle())
            ws.send_text(_heartbeat())
            for name, mid in cases:
                event = _load_event(name)
                event["message_id"] = int(mid)
                ws.send_text(json.dumps(event))

        def _record(mid: str) -> ShadowDecision | None:
            async def _fetch() -> ShadowDecision | None:
                async with SessionLocal() as session:
                    return (
                        (
                            await session.execute(
                                select(ShadowDecision).where(ShadowDecision.message_id == mid)
                            )
                        )
                        .scalars()
                        .first()
                    )

            return _run(_fetch())

        for _name, mid in cases:
            assert _wait_for(lambda mid=mid: _record(mid) is not None), f"{mid} 未被处理"
            record = _record(mid)
            assert record is not None
            assert record.verdict == "record_only", f"{mid} 应降级人工"
            assert "转人工" in record.reason


# ---------- 动作隔离（结构性保证） ----------


def test_onebot_adapter_never_defines_management_actions() -> None:
    """OneBot Adapter 包内不得出现任何管理动作实现（撤回/禁言/警告/踢人）。"""
    import ast

    adapter_dir = Path(__file__).parent.parent / "app" / "adapters" / "onebot"
    forbidden = {"recall", "mute", "unmute", "warn", "kick"}
    for py in sorted(adapter_dir.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.name not in forbidden, f"{py.name} 定义了管理动作 {node.name}"
        assert "kick" not in py.read_text(encoding="utf-8").lower(), f"{py.name} 出现 kick"


# ---------- 配置 fail-closed 校验 ----------


def test_enabled_without_token_rejected() -> None:
    from app.config import Settings

    with pytest.raises(ValueError, match="ONEBOT_ACCESS_TOKEN"):
        Settings(onebot_ws_enabled=True, onebot_access_token="  ", _env_file=None)


def test_enabled_with_public_host_rejected() -> None:
    from app.config import Settings

    with pytest.raises(ValueError, match="本机回环或可信内网"):
        Settings(
            onebot_ws_enabled=True,
            onebot_access_token="tok",
            web_host="8.8.8.8",
            _env_file=None,
        )


def test_enabled_private_host_and_loopback_accepted() -> None:
    from app.config import Settings

    ok = Settings(
        onebot_ws_enabled=True,
        onebot_access_token="tok",
        web_host="192.168.1.10",
        _env_file=None,
    )
    assert ok.onebot_ws_enabled is True
    ok2 = Settings(
        onebot_ws_enabled=True, onebot_access_token="tok", web_host="127.0.0.1", _env_file=None
    )
    assert ok2.onebot_ws_enabled is True


def test_bad_ws_path_rejected() -> None:
    from app.config import Settings

    with pytest.raises(ValueError, match="ONEBOT_WS_PATH"):
        Settings(
            onebot_ws_enabled=True,
            onebot_access_token="tok",
            onebot_ws_path="onebot/ws",
            _env_file=None,
        )
