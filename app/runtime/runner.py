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
import math
import time
from typing import Any

import httpx
from websockets.asyncio.client import connect

from app.adapters.qq_official.media import download_attachment
from app.core.async_utils import blocking_call
from app.core.media_diagnostics import download_error_code
from app.db import SessionLocal
from app.moderation.image_engine import ImageModerationEngine
from app.moderation.imaging import dhash
from app.moderation.rules import TextRuleEngine
from app.runtime.pipeline import MEDIA_DIR, run_pipeline

WS_URL = "wss://api.sgroup.qq.com/websocket"
API_BASE = "https://api.bot.qq.com"
RECONNECT_BACKOFF_MAX = 30.0

SAMPLES_DIR = MEDIA_DIR.parent / "t002_media"


def _seed_image_engine() -> ImageModerationEngine:
    """用负责人逐张确认的样本哈希初始化引擎黑/白名单（manifest 为准）。"""
    engine = ImageModerationEngine()
    manifest_path = SAMPLES_DIR / "manifest.json"
    if not manifest_path.exists():
        print("[runner] WARN: sample manifest not found, image lists empty")
        return engine
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    seeded_violation = 0
    seeded_allowed = 0
    for item in manifest.get("items", []):
        sample_path = SAMPLES_DIR / str(item.get("file", ""))
        if not sample_path.exists():
            continue
        try:
            from app.moderation.imaging import image_frames_from_source

            image_hash = dhash(image_frames_from_source(sample_path)[0])
        except Exception as exc:  # noqa: BLE001
            print(f"[runner] WARN: hash failed {item.get('file')}: {exc}")
            continue
        label = item.get("label")
        if label == "violation":
            engine.add_violation_hash(image_hash)
            seeded_violation += 1
        elif label == "allowed_campus_wall":
            engine.add_allowed_hash(image_hash)
            seeded_allowed += 1
    print(f"[runner] image engine seeded: violation={seeded_violation} allowed={seeded_allowed}")
    return engine


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
    """R-102-4：附件安全下载到 data/media/（流式大小限制+安全文件名+磁盘配额）。

    下载失败/超限/超配额时 filename 留空，流水线据此判 record_only（不处罚）。
    """
    message_id = str(payload.get("id") or "msg")
    errors: list[str] = []
    payload["_media_download_errors"] = errors
    for idx, att in enumerate(payload.get("attachments") or []):
        url = str(att.get("url") or "")
        if not url:
            errors.append("missing_url")
            continue
        declared = str(att.get("content_type") or "")
        name, _ext, reason = await download_attachment(
            client, url, MEDIA_DIR, message_id, idx, declared
        )
        if name:
            att["filename"] = name
        else:
            # 下载失败：流水线将判 record_only
            att["filename"] = ""
            att["_download_error"] = reason
            errors.append(download_error_code(reason or ""))


async def _listen_once(app_id: str, app_secret: str, stop: asyncio.Event) -> float:
    """连接并处理事件直到断开；返回本次存活秒数（用于退避判断）。"""
    started = time.monotonic()
    async with httpx.AsyncClient(
        timeout=15, follow_redirects=False, trust_env=False, http2=False
    ) as dl_client:
        token = await _get_token(dl_client, app_id, app_secret)
        async with connect(WS_URL, max_size=2**22) as ws:
            hello = json.loads(await ws.recv())
            hb_interval = float(hello["d"]["heartbeat_interval"]) / 1000
            if not math.isfinite(hb_interval) or hb_interval <= 0:
                raise ValueError("invalid heartbeat interval")
            last_s = hello.get("s", 0)
            await ws.send(
                json.dumps(
                    {"op": 2, "d": {"token": f"QQBot {token}", "intents": 1 << 25, "shard": [0, 1]}}
                )
            )
            print("[runner] connected, shadow mode (record only)")
            text_engine = TextRuleEngine()

            async def heartbeat() -> None:
                while not stop.is_set():
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=hb_interval)
                    except TimeoutError:
                        await ws.send(json.dumps({"op": 1, "d": last_s}))

            # Heartbeats must run during incoming traffic and slow moderation.
            # A failed heartbeat cancels the receiver and reaches run()'s reconnect.
            async with asyncio.TaskGroup() as group:
                heartbeat_task = group.create_task(heartbeat())
                try:
                    image_engine = await blocking_call(_seed_image_engine)
                    while not stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5)
                        except TimeoutError:
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
                                record = await run_pipeline(
                                    data,
                                    session,
                                    text_engine=text_engine,
                                    image_engine=image_engine,
                                )
                                if record:
                                    print(
                                        f"[shadow] {record.kind} verdict={record.verdict} "
                                        f"conf={record.confidence} {record.reason[:60]}"
                                    )
                finally:
                    heartbeat_task.cancel()
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
