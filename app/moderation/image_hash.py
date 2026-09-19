"""图片感知哈希白名单（负责人 2026-09-19：「确定放行」的图不再受模型波动影响）。

设计要点：

- **感知哈希（dHash 64 位），不是文件 SHA256**：QQ 转发会重压缩/缩放图片，精确哈希必然
  miss；dHash 对压缩/缩放稳定，适合"同一张图再次出现"的判定。
- 命中判定：汉明距离 <= 阈值（默认 8/64，需用负责人样本校准；越宽松误放行风险越高）。
- 读取**每条消息直读数据库**、异常返回空集（fail-closed）：读不到白名单绝不误放行。
- 白名单只提供"这是负责人认可的那类图"这一条证据；**色情 / 暴力违禁品与本地硬证据
  仍然优先**（与 D-039 小程序码放行的例外口径一致），由调用方负责这些例外。
- 命中计数 best-effort：失败只记日志，不影响判定结果。
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ImageAllowlist

logger = logging.getLogger(__name__)

# 64 位 dHash 的命中阈值：先用负责人样本校准，偏严格（越小越保守）
DEFAULT_MAX_DISTANCE = 8


def dhash64(data: bytes) -> int | None:
    """计算 64 位 dHash；无法解码/过小/缺 Pillow 时返回 ``None``（按未命中处理）。"""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            gray = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
            pixels = list(gray.getdata())
    except Exception:  # noqa: BLE001 - 任何解码问题都按"未命中"处理
        return None
    if len(pixels) < 72:
        return None
    bits = 0
    for row in range(8):
        for col in range(8):
            left = pixels[row * 9 + col]
            right = pixels[row * 9 + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def dhash64_file(path: Path) -> int | None:
    """从文件算 dHash；读不到文件返回 ``None``。"""
    try:
        return dhash64(path.read_bytes())
    except OSError:
        return None


def to_hex(phash: int) -> str:
    """64 位哈希 → 16 位十六进制字符串（入库格式）。"""
    return f"{phash:016x}"


def from_hex(value: str) -> int | None:
    """十六进制字符串 → 64 位哈希；非法值返回 ``None``。"""
    try:
        parsed = int(value, 16)
    except (TypeError, ValueError):
        return None
    return parsed if 0 <= parsed < (1 << 64) else None


def hamming64(left: int, right: int) -> int:
    """两个 64 位哈希的汉明距离。"""
    return bin(left ^ right).count("1")


def best_match(
    phash: int,
    candidates: Iterable[tuple[int, int]],
    *,
    max_distance: int = DEFAULT_MAX_DISTANCE,
) -> tuple[int, int] | None:
    """在 ``(条目 id, 哈希)`` 中找最接近的一条；返回 ``(id, 距离)``，无命中返回 ``None``。"""
    best: tuple[int, int] | None = None
    for row_id, candidate in candidates:
        distance = hamming64(phash, candidate)
        if distance <= max_distance and (best is None or distance < best[1]):
            best = (row_id, distance)
    return best


async def load_enabled(session: AsyncSession) -> list[tuple[int, int]]:
    """读取启用中的白名单 ``[(id, phash)]``；**任何异常都返回空列表**（fail-closed）。"""
    try:
        rows = (
            await session.execute(
                select(ImageAllowlist.id, ImageAllowlist.phash).where(
                    ImageAllowlist.enabled.is_(True)
                )
            )
        ).all()
    except Exception:  # noqa: BLE001 - 读不到白名单时绝不误放行
        logger.warning("读取图片哈希白名单失败，按空集处理（fail-closed）", exc_info=True)
        return []
    candidates: list[tuple[int, int]] = []
    for row_id, value in rows:
        parsed = from_hex(str(value))
        if parsed is not None:
            candidates.append((int(row_id), parsed))
    return candidates


async def match_bytes(
    session: AsyncSession, data: bytes, *, max_distance: int = DEFAULT_MAX_DISTANCE
) -> tuple[int, int] | None:
    """判断图片字节是否命中白名单；返回 ``(条目 id, 距离)`` 或 ``None``。"""
    phash = dhash64(data)
    if phash is None:
        return None
    candidates = await load_enabled(session)
    if not candidates:
        return None
    return best_match(phash, candidates, max_distance=max_distance)


async def record_hit(session: AsyncSession, entry_id: int) -> None:
    """累加命中次数（best-effort：失败只记日志，不影响判定）。"""
    try:
        await session.execute(
            update(ImageAllowlist)
            .where(ImageAllowlist.id == entry_id)
            .values(hit_count=ImageAllowlist.hit_count + 1)
        )
    except Exception:  # noqa: BLE001
        logger.warning("累加图片白名单命中次数失败 id=%s", entry_id, exc_info=True)
