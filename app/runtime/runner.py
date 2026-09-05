"""WebSocket 常驻运行器（影子模式入口）。

`uv run python -m app.runtime` 启动：
- 连接官方 WebSocket，自动心跳、断线指数退避重连（有限退避上限30秒）；
- 收到 GROUP_MESSAGE_CREATE → 下载媒体附件 → 影子流水线（只记录不处罚）；
- 群主/管理员内容与全部判定均只落库，可在管理后台查看。

影子模式硬约束：本运行器**从不调用**撤回/禁言/警告接口。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
from websockets.asyncio.client import connect

from app.adapters.qq_official.parser import EventParseError
from app.db import SessionLocal
from app.moderation.image_engine import ImageModerationEngine
from app.moderation.rules import TextRuleEngine
from app.runtime.pipeline import MEDIA_DIR, run_pipeline

WS_URL = "wss://api.sgroup.qq.com/websocket"
API_BASE = "https://api.bot.qq.com"
RECONNECT_BACKOFF_MAX = 30.0

EXT_BY_CT = {
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/png": ".png",
    "voice": ".amr",
    "video/mp4": ".mp4",
    "file": ".bin",
}


def _read_env_credentials() -> tuple[str, str]:
    from app.config import get_settings

    settings = get_settings()
    return settings.qq_app_id, settings.qq_app_secret


async def _get_token(client: httpx.AsyncClient, app_id: str, app_secret: str) -> str:
    resp = await client.post(
        f"{API_BASE}/app/getAppAccessToken", json={"appId": app_id, "clientSecret": app_secret}
    )
    resp.raise_for_status()
    token = str(resp.json().get("access_token") or "")
    if not token:
        raise RuntimeError("令牌响应缺少 access_token")
    return token


async def _download_attachments(client: httpx.AsyncClient, payload: dict[str, Any]) -> None:
    """D-013：附件URL有时效，收到即下载到 data/media/（文件名取消息ID尾缀+序号，保证唯一）。"""
    message_id = str(payload.get("id") or "msg")
    for idx, att in enumerate(payload.get("attachments") or []):
        url = str(att.get("url") or "")
        if not url:
            continue
        if url.startswith("//"):
            url = "https:" + url
        try:
            resp = await client.get(url, timeout=30)
            resp.raise_for_status()
        except httpx.HTTPError:
            continue  # 下载失败不处罚，由媒体引擎按缺失处理
        declared = str(att.get("content_type") or "")
        ext = EXT_BY_CT.get(declared, ".bin")
        if resp.content[:3] == b"GIF":
            ext = ".gif"
        elif resp.content[:8] == b"\x89PNG\r\n\x1a\n":
            ext = ".png"
        elif resp.content[:3] == b"\xff\xd8\xff":
            ext = ".jpg"
        name = f"{message_id[-12:]}_{idx}{ext}"
        (MEDIA_DIR / name).write_bytes(resp.content)
        # 回写本地文件名供流水线媒体判定对应
        att["filename"] = name
        if not att.get("url"):
            att["url"] = url


async def _listen_once(app_id: str, app_secret: str, stop: asyncio.Event) -> float:
    """连接并处理事件直到断开；返回本次存活秒数（用于退避判断）。"""
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as dl_client:
        token = await _get_token(dl_client, app_id, app_secret)
        async with connect(WS_URL, max_size=2**22) as ws:
            hello = json.loads(await ws.recv())
            hb_interval = hello["d"]["heartbeat_interval"] / 1000
            last_s = hello.get("s", 0)
            await ws.send(
                json.dumps(
                    {"op": 2, "d": {"token": f"QQBot {token}", "intents": 1 << 25, "shard": [0, 1]}}
                )
            )
            print("[runner] connected, shadow mode (record only)")
            next_hb = time.monotonic() + hb_interval
            text_engine = TextRuleEngine()
            image_engine = ImageModerationEngine()

            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except TimeoutError:
                    if time.monotonic() >= next_hb:
                        await ws.send(json.dumps({"op": 1, "d": last_s}))
                        next_hb = time.monotonic() + hb_interval
                    continue
                payload = json.loads(raw)
                if payload.get("s") is not None:
                    last_s = payload["s"]
                op = payload.get("op")
                if op == 11:
                    continue
                if op == 1:
                    await ws.send(json.dumps({"op": 11}))
                    continue
                if op == 9:
                    raise RuntimeError("会话失效（op 9），需要重新连接")
                if op == 0 and payload.get("t") == "GROUP_MESSAGE_CREATE":
                    data = payload.get("d") or {}
                    await _download_attachments(dl_client, data)
                    async with SessionLocal() as session:
                        try:
                            record = await run_pipeline(
                                data, session, text_engine=text_engine, image_engine=image_engine
                            )
                        except EventParseError as exc:
                            print(f"[runner] parse error: {exc}")
                            continue
                        if record:
                            print(
                                f"[shadow] {record.kind} verdict={record.verdict} "
                                f"conf={record.confidence} {record.reason[:60]}"
                            )
    return time.monotonic() - started


async def run(stop: asyncio.Event | None = None) -> None:
    """常驻入口：指数退避重连。"""
    stop = stop or asyncio.Event()
    app_id, app_secret = _read_env_credentials()
    if not app_id or not app_secret:
        raise RuntimeError("QQ_APP_ID / QQ_APP_SECRET 未配置（.env），无法启动运行器")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    backoff = 1.0
    while not stop.is_set():
        try:
            alive = await _listen_once(app_id, app_secret, stop)
            backoff = 1.0 if alive > 60 else min(backoff * 2, RECONNECT_BACKOFF_MAX)
        except Exception as exc:  # noqa: BLE001 - 断线重连
            print(f"[runner] disconnected: {type(exc).__name__}: {exc}; retry in {backoff:.0f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)


if __name__ == "__main__":
    asyncio.run(run())
