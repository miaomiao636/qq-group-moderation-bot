"""媒体下载与存储安全（R-102-4）。

- 安全文件名：仅 ASCII 字母数字_-，其余替换为下划线，防止路径穿越与非法字符；
- 流式大小限制：边下载边累加，超过上限立即中止（避免超大文件耗尽内存/磁盘）；
- 磁盘配额：下载前检查 `MEDIA_DIR` 已用空间，超配额则拒绝下载并返回原因；
- 内容嗅探扩展名：魔数优先；
- 清理：按保留期删除媒体文件，与数据库保留期一致。
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# 单文件大小上限（字节）。图片/语音/文件 50MB，视频 200MB。
_LIMITS_BY_KIND: dict[str, int] = {"video": 200 * 1024 * 1024}
DEFAULT_FILE_LIMIT = 50 * 1024 * 1024
MEDIA_QUOTA_BYTES = 2 * 1024 * 1024 * 1024  # data/media 总配额 2GB

_MAGIC = [
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF8", ".gif"),
    (b"#!AMR", ".amr"),
    (b"\x1aE\xdf\xa3", ".mkv"),
    (b"%PDF-", ".pdf"),
]
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]")


def safe_filename(message_id: str, idx: int) -> str:
    """从消息ID与序号生成安全文件名（不含扩展名）。"""
    stem = _SAFE_NAME.sub("_", message_id)[-24:].lstrip("_") or "msg"
    return f"{stem}_{idx}"


def sniff_ext(data: bytes, declared: str) -> str:
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    if declared.startswith("video/"):
        return ".mp4"
    if declared.startswith("image/"):
        return ".jpg"
    if declared == "voice" or declared.startswith("audio/"):
        return ".amr"
    return ".bin"


def total_media_size(media_dir: Path) -> int:
    if not media_dir.exists():
        return 0
    return sum(f.stat().st_size for f in media_dir.iterdir() if f.is_file())


async def stream_download(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
    *,
    size_limit: int = DEFAULT_FILE_LIMIT,
) -> tuple[bool, str, int]:
    """流式下载到 dest，超限时中止。返回 (ok, reason, bytes)。"""
    bytes_written = 0
    try:
        async with client.stream("GET", url, timeout=30, follow_redirects=True) as resp:
            resp.raise_for_status()
            tmp = dest.with_suffix(dest.suffix + ".part")
            with tmp.open("wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    bytes_written += len(chunk)
                    if bytes_written > size_limit:
                        f.close()
                        tmp.unlink(missing_ok=True)
                        return False, f"文件超过{size_limit}字节上限", bytes_written
                    f.write(chunk)
            tmp.replace(dest)
        return True, "", bytes_written
    except httpx.HTTPError as exc:
        return False, f"下载失败:{type(exc).__name__}", bytes_written
    except OSError as exc:
        return False, f"写入失败:{type(exc).__name__}", bytes_written


async def download_attachment(
    client: httpx.AsyncClient,
    url: str,
    media_dir: Path,
    message_id: str,
    idx: int,
    content_type: str,
) -> tuple[str | None, str, str]:
    """下载单个附件。返回 (filename, ext, reason)。

    filename=None 表示未下载（reason 给出原因：配额/大小/网络）。
    """
    if not url:
        return None, "", "无URL"
    if url.startswith("//"):
        url = "https:" + url

    if total_media_size(media_dir) >= MEDIA_QUOTA_BYTES:
        return None, "", "磁盘配额已满"

    kind = "video" if content_type.startswith("video/") else "default"
    limit = _LIMITS_BY_KIND.get(kind, DEFAULT_FILE_LIMIT)

    # 先流式下载到 .bin 临时，再嗅探扩展名重命名
    stem = safe_filename(message_id, idx)
    tmp_path = media_dir / f"{stem}.bin"
    ok, reason, _bytes = await stream_download(client, url, tmp_path, size_limit=limit)
    if not ok:
        tmp_path.unlink(missing_ok=True)
        return None, "", reason or "下载失败"
    data_head = tmp_path.read_bytes()[:16]
    ext = sniff_ext(data_head, content_type)
    final_path = media_dir / f"{stem}{ext}"
    try:
        tmp_path.replace(final_path)
    except OSError:
        final_path = tmp_path.with_suffix(ext)
        tmp_path.rename(final_path)
    return final_path.name, ext, ""


def purge_media(media_dir: Path, retention_days: int = 30, now: float | None = None) -> int:
    """删除超过保留期的媒体文件，返回删除文件数。"""
    if not media_dir.exists():
        return 0
    cutoff = (now or time.time()) - retention_days * 86400
    deleted = 0
    for f in media_dir.iterdir():
        if not f.is_file() or f.suffix == ".part":
            continue
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                deleted += 1
        except OSError:
            continue
    return deleted


def media_cleanup_due(now: datetime | None = None) -> bool:
    """是否到了媒体清理时机（保留期外文件存在即应清理）；调度由部署层驱动。"""
    return True
