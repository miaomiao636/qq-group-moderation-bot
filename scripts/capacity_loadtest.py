#!/usr/bin/env python3
"""百群容量压测（容量门槛第③步）——隔离环境合成流量。

设计（2026-09-15，负责人授权执行）：
- **隔离**：独立 SQLite `data/loadtest/loadtest.db`，启动前强校验库路径含
  `loadtest`，绝不触碰生产库；SHADOW 模式、真实动作关闭、通知关闭。
- **假 AI**：本机 Mock OpenAI-compatible 服务（端口 8788），按生产延迟分布
  模拟（90% ≈1.6s / 10% ≈5.3s），不产生真实外呼与费用。
- **真实全链路**：durable inbox 入库 → 3 worker 认领 → 解析 → 规则分级 →
  AI（mock）→ 落库；保留生产限流参数（AI_PER_MINUTE_LIMIT=90 /
  AI_DAILY_CALL_LIMIT=3000）以暴露容量边界。
- **负载**：阶段1 日常均值 0.25 条/秒（百群 ~0.23 的近似）× 3 分钟；
  阶段2 极端峰值 2.0 条/秒（≈120 条/分钟）× 8 分钟；随后排空观察。
- 消息文本中性（避免命中规则），AI 一律返回正常 → 只测吞吐/积压/延迟，
  不压违规分支与案件写入。

用法：`.venv\\Scripts\\python.exe scripts/capacity_loadtest.py`
结果：stdout 时间线 + `data/loadtest/result.json`（汇总指标）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
LT_DIR = ROOT / "data" / "loadtest"
LT_DB = LT_DIR / "loadtest.db"
UNIQUE_TEXT = os.environ.get("LT_UNIQUE_TEXT") == "1"
RESULT_FILE = LT_DIR / ("result_unique.json" if UNIQUE_TEXT else "result.json")
MOCK_PORT = 8788
SELF_ID = "10000001"
GROUPS = [str(900000000 + i) for i in range(100)]
USERS = [str(800000000 + i) for i in range(1000)]
TEXTS = [
    "今天的天气真不错，大家午安",
    "周末有一起打球的朋友吗",
    "图书馆现在人还多吗",
    "刚看完那部电影，挺好看的",
    "有人知道食堂几点关门吗",
    "明早的课在哪个教室来着",
    "谁的外卖放错柜子了",
    "学校附近哪里能修自行车",
]
PHASES = [
    ("daily", 0.25, 180),  # 百群日常均值近似（条/秒, 持续秒）
    ("peak", 2.0, 480),  # 极端峰值 ≈120 条/分钟
]
if os.environ.get("LT_QUICK") == "1":
    # 无缓存补测：纯峰值短负载，文本唯一化使缓存失效；LT_PEAK_RATE 可调速率
    PHASES = [("peak", float(os.environ.get("LT_PEAK_RATE", "2.0")), 150)]
DRAIN_MAX_SECONDS = 300

# ---- 环境必须在 import app 之前设置（pydantic-settings：环境变量优先 .env）----
os.environ.update(
    {
        "APP_ENV": "test",
        "RUN_MODE": "SAFE",
        "DATABASE_URL": f"sqlite+aiosqlite:///{LT_DB.as_posix()}",
        "WEB_HOST": "127.0.0.1",
        "WEB_PORT": "8124",
        "LOG_LEVEL": "WARNING",
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": "loadtest-admin",
        "AI_ENABLED": "true",
        "AI_ENABLED_GROUPS": "*",
        "AI_BASE_URL": f"http://127.0.0.1:{MOCK_PORT}/v1",
        "AI_API_KEY": "loadtest-key",
        "AI_TEXT_MODEL": "mock-flash",
        "AI_VISION_MODEL": "mock-flash",
        "AI_REVIEW_MODEL": "",
        "AI_REVIEW_BASE_URL": "",
        "AI_REVIEW_API_KEY": "",
        "AI_TIMEOUT_SECONDS": "60",
        # 与 2026-09-15 生产调整后的值一致（负责人授权：90→600、3000→50000）
        "AI_PER_MINUTE_LIMIT": "600",
        "AI_DAILY_CALL_LIMIT": "50000",
        "AI_DAILY_BUDGET_CENTS": "0",
        "AI_PROMPT_VERSION": "loadtest",
        "NOTIFICATIONS_ENABLED": "false",
        "NOTIFICATION_QQ_ENABLED": "false",
        "NOTIFICATION_EMAIL_ENABLED": "false",
        "NOTIFICATION_HEARTBEAT_ENABLED": "false",
        "ONEBOT_WS_ENABLED": "false",
        "ONEBOT_ACCESS_TOKEN": "loadtest-token",
        "ONEBOT_SELF_ID": SELF_ID,
        "ONEBOT_ACTIONS_ENABLED": "false",
        "ACTION_MODE": "SHADOW",
        "EMERGENCY_STOP": "false",
        "RAW_RETENTION_DAYS": "15",
        "DECISION_RETENTION_DAYS": "15",
    }
)

_DELAY_MIX = [(1.6, 0.9), (5.3, 0.1)]


class MockAI(BaseHTTPRequestHandler):
    """OpenAI-compatible /v1/chat/completions：模拟延迟 + 固定正常结果。"""

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        delay = random.choices([d for d, _w in _DELAY_MIX], weights=[w for _d, w in _DELAY_MIX])[0]
        time.sleep(delay + random.uniform(-0.2, 0.2))
        content = json.dumps(
            {"category": None, "confidence": 0.05, "evidence": "loadtest", "needs_review": False}
        )
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # 静默
        return


def _migrate() -> None:
    from alembic import command
    from alembic.config import Config as AlembicConfig

    command.upgrade(AlembicConfig(str(ROOT / "alembic.ini")), "head")


async def _snapshot() -> dict[str, int]:
    from app.db import SessionLocal
    from app.moderation.ai import AIUsageLog
    from app.runtime.inbox import InboxEvent
    from app.runtime.models import ShadowDecision
    from sqlalchemy import func, select

    async with SessionLocal() as session:
        processed = (
            await session.execute(select(func.count()).select_from(ShadowDecision))
        ).scalar() or 0
        backlog = (
            await session.execute(
                select(func.count())
                .select_from(InboxEvent)
                .where(InboxEvent.status.in_(("PENDING", "PROCESSING")))
            )
        ).scalar() or 0
        ai_calls = (
            await session.execute(select(func.count()).select_from(AIUsageLog))
        ).scalar() or 0
        limited = (
            await session.execute(
                select(func.count())
                .select_from(AIUsageLog)
                .where(AIUsageLog.error_kind == "ai_rate_or_budget_limited")
            )
        ).scalar() or 0
    return {"processed": processed, "backlog": backlog, "ai_calls": ai_calls, "limited": limited}


async def _run() -> dict[str, object]:
    from app.config import get_settings
    from app.db import SessionLocal
    from app.moderation.ai import AIUsageLog
    from app.runtime import inbox
    from app.runtime.models import ShadowDecision
    from app.runtime.onebot_ws import _ensure_worker
    from sqlalchemy import func, select

    settings = get_settings()
    assert "loadtest" in settings.database_url, f"拒绝非隔离库: {settings.database_url}"
    print(f"[setup] db={settings.database_url}")

    _ensure_worker()
    await asyncio.sleep(1.0)

    counter = {"sent": 0}
    timeline: list[dict[str, object]] = []
    stop_monitor = asyncio.Event()

    async def _send() -> None:
        n = counter["sent"]
        counter["sent"] = n + 1
        gid = random.choice(GROUPS)
        uid = random.choice(USERS)
        text = random.choice(TEXTS)
        if UNIQUE_TEXT:
            text = f"{text} #{n}"  # 使 AI 缓存键失效（无缓存条件下的真实吞吐）
        event = {
            "post_type": "message",
            "message_type": "group",
            "self_id": int(SELF_ID),
            "message_id": 700000000000 + n,
            "group_id": int(gid),
            "user_id": int(uid),
            "time": int(time.time()),
            "message": [{"type": "text", "data": {"text": random.choice(TEXTS)}}],
            "sender": {"user_id": int(uid), "role": "member", "nickname": f"LT{n % 50}"},
        }
        async with SessionLocal() as session:
            await inbox.enqueue_event(session, event, max_pending=500)

    async def _monitor() -> None:
        t0 = time.monotonic()
        while not stop_monitor.is_set():
            snap = await _snapshot()
            line = (
                f"[{time.monotonic() - t0:6.1f}s] sent={counter['sent']}"
                f" processed={snap['processed']} backlog={snap['backlog']}"
                f" ai={snap['ai_calls']} limited={snap['limited']}"
            )
            print(line, flush=True)
            timeline.append({"t": round(time.monotonic() - t0, 1), "sent": counter["sent"], **snap})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_monitor.wait(), timeout=15)

    monitor = asyncio.create_task(_monitor())

    for name, rate, duration in PHASES:
        print(f"[phase] {name}: {rate} 条/秒 × {duration}s", flush=True)
        interval = 1.0 / rate
        nxt = time.monotonic()
        end = nxt + duration
        while time.monotonic() < end:
            await _send()
            nxt += interval
            sleep_for = nxt - time.monotonic()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

    print("[phase] drain: 等待队列排空…", flush=True)
    drain_start = time.monotonic()
    drained_at: float | None = None
    while time.monotonic() - drain_start < DRAIN_MAX_SECONDS:
        snap = await _snapshot()
        if snap["backlog"] == 0:
            drained_at = time.monotonic() - drain_start
            break
        await asyncio.sleep(5)
    stop_monitor.set()
    await monitor

    final = await _snapshot()

    # 端到端延迟（事件 time → 落库时间）
    async with SessionLocal() as session:
        rows = (
            await session.execute(select(ShadowDecision.detail_json, ShadowDecision.created_at))
        ).all()
    e2e: list[float] = []
    for detail, created in rows:
        try:
            sent_at = json.loads(detail or "{}").get("sent_at")
        except json.JSONDecodeError:
            continue
        if not sent_at:
            continue
        try:
            from datetime import datetime

            s = datetime.fromisoformat(str(sent_at).replace("Z", "+00:00")).replace(tzinfo=None)
            e = created.replace(tzinfo=None) if created.tzinfo else created
            delta = (e - s).total_seconds()
        except ValueError:
            continue
        if 0 <= delta < 3600:
            e2e.append(delta)
    e2e.sort()

    def _p(vals: list[float], p: float) -> float | None:
        if not vals:
            return None
        k = int(round((p / 100) * len(vals) + 0.5)) - 1
        return round(vals[max(0, min(len(vals) - 1, k))], 1)

    async with SessionLocal() as session:
        limited = (
            await session.execute(
                select(func.count())
                .select_from(AIUsageLog)
                .where(AIUsageLog.error_kind == "ai_rate_or_budget_limited")
            )
        ).scalar() or 0
        ai_total = (
            await session.execute(select(func.count()).select_from(AIUsageLog))
        ).scalar() or 0
        ok_total = (
            await session.execute(
                select(func.count()).select_from(AIUsageLog).where(AIUsageLog.ok.is_(True))
            )
        ).scalar() or 0

    db_bytes = LT_DB.stat().st_size if LT_DB.exists() else 0
    result: dict[str, object] = {
        "sent": counter["sent"],
        "processed": final["processed"],
        "backlog_after_drain": final["backlog"],
        "drained_after_seconds": drained_at,
        "ai_calls": ai_total,
        "ai_ok": ok_total,
        "ai_limited": limited,
        "e2e_samples": len(e2e),
        "e2e_p50": _p(e2e, 50),
        "e2e_p80": _p(e2e, 80),
        "e2e_p95": _p(e2e, 95),
        "db_bytes": db_bytes,
        "timeline": timeline,
    }
    print(
        "[result] "
        + json.dumps(
            {k: v for k, v in result.items() if k != "timeline"}, ensure_ascii=False, indent=2
        )
    )
    RESULT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    LT_DIR.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(LT_DB) + suffix)
        if p.exists():
            p.unlink()
    _migrate()
    server = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockAI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[setup] mock AI on :{MOCK_PORT}")
    try:
        asyncio.run(_run())
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
