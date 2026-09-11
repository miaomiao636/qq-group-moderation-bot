"""媒体下载与存储安全（R-102-4）。

- 安全文件名：仅 ASCII 字母数字_-，其余替换为下划线，防止路径穿越与非法字符；
- 流式大小限制：边下载边累加，超过上限立即中止（避免超大文件耗尽内存/磁盘）；
- 磁盘配额：下载前检查 `MEDIA_DIR` 已用空间，超配额则拒绝下载并返回原因；
- 内容嗅探扩展名：魔数优先；
- 清理：按保留期删除媒体文件，与数据库保留期一致。
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import socket
import time
import urllib.parse
from contextlib import suppress
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
_QUOTA_LOCK = asyncio.Lock()


def safe_filename(message_id: str, idx: int) -> str:
    """从消息ID与序号生成安全文件名（不含扩展名）。"""
    digest = hashlib.sha256(message_id.encode("utf-8")).hexdigest()
    return f"{digest}_{idx}"


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


def ensure_media_dir(media_dir: Path) -> None:
    media_dir.mkdir(parents=True, exist_ok=True)


def sniff_file_head(path: Path, limit: int = 16) -> bytes:
    """只读取文件头，避免为魔数嗅探把完整媒体读入内存。"""
    with path.open("rb") as f:
        return f.read(limit)


_MAX_REDIRECT_HOPS = 3
_DOWNLOAD_TOTAL_SECONDS = 120.0


async def stream_download(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
    *,
    size_limit: int = DEFAULT_FILE_LIMIT,
    media_dir: Path | None = None,
    quota_bytes: int = MEDIA_QUOTA_BYTES,
) -> tuple[bool, str, int]:
    """Bound the entire redirect/stream operation, including slow-drip responses."""
    try:
        async with asyncio.timeout(_DOWNLOAD_TOTAL_SECONDS):
            return await _stream_download(
                client,
                url,
                dest,
                size_limit=size_limit,
                media_dir=media_dir,
                quota_bytes=quota_bytes,
            )
    except TimeoutError:
        return False, "下载总时长超时", 0
    finally:
        # A failed/cancelled stream is never a retained media artifact.
        with suppress(OSError):
            dest.with_suffix(dest.suffix + ".part").unlink(missing_ok=True)


async def _stream_download(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
    *,
    size_limit: int,
    media_dir: Path | None,
    quota_bytes: int,
) -> tuple[bool, str, int]:
    """流式下载到 dest，超限时中止。返回 (ok, reason, bytes)。

    P1-6: 每跳校验并固定 IP，保留逻辑 Host / TLS SNI。
    调用方须提供专用 HTTP/1.1 client（http2=False、trust_env=False）。
    """
    current_url = url
    try:
        for _hop in range(_MAX_REDIRECT_HOPS + 1):
            pinned = await _pin_media_url(current_url)
            if pinned is None:
                return False, "URL被SSRF防护拦截", 0
            target, host_header, server_hostname = pinned
            async with client.stream(
                "GET",
                target,
                timeout=30,
                follow_redirects=False,
                # Do not pool one IP's TLS connection across different logical
                # hosts: each request must verify its own original hostname.
                headers={"Host": host_header, "Connection": "close"},
                extensions={"sni_hostname": server_hostname},
            ) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location", "")
                    if not location:
                        return False, "重定向缺少Location头", 0
                    current_url = urllib.parse.urljoin(current_url, location)
                    continue
                resp.raise_for_status()
                bytes_written = 0
                tmp = dest.with_suffix(dest.suffix + ".part")
                tmp.unlink(missing_ok=True)
                quota_dir = media_dir or dest.parent
                base_size = total_media_size(quota_dir)
                content_length = resp.headers.get("content-length")
                if content_length and content_length.isdigit():
                    expected_size = int(content_length)
                    if base_size + expected_size > quota_bytes:
                        return False, f"磁盘配额不足，剩余{max(quota_bytes - base_size, 0)}字节", 0
                with tmp.open("wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        bytes_written += len(chunk)
                        if bytes_written > size_limit:
                            f.close()
                            tmp.unlink(missing_ok=True)
                            return False, f"文件超过{size_limit}字节上限", bytes_written
                        if base_size + bytes_written > quota_bytes:
                            f.close()
                            tmp.unlink(missing_ok=True)
                            return (
                                False,
                                f"磁盘配额不足，剩余{max(quota_bytes - base_size, 0)}字节",
                                bytes_written,
                            )
                        f.write(chunk)
                if total_media_size(quota_dir) > quota_bytes:
                    tmp.unlink(missing_ok=True)
                    return False, "磁盘配额不足", bytes_written
                tmp.replace(dest)
                return True, "", bytes_written
        return False, "重定向次数超上限", 0
    except httpx.HTTPError as exc:
        return False, f"下载失败:{type(exc).__name__}", 0
    except OSError as exc:
        return False, f"写入失败:{type(exc).__name__}", 0


# SSRF防护：拒绝回环/私有/链路本地/多播/未指定/保留地址（P1-6扩展）
_BLOCKED_NETWORKS = [
    ipaddress.ip_network(n)
    for n in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "0.0.0.0/8",
        "100.64.0.0/10",  # CGN/共享地址
        "192.0.0.0/24",  # IETF协议分配
        "198.18.0.0/15",  # 基准测试
        "224.0.0.0/3",  # 多播+保留
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "::/128",  # 未指定
        "::ffff:0:0/96",  # IPv4映射
    )
]

_ALLOWED_PORTS = {80, 443}  # P1-6: 仅允许标准HTTP/HTTPS端口


def _is_safe_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """检查IP是否安全（不在被封锁的网络段内）。"""
    return (
        ip.is_global
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_unspecified
        and not any(ip in net for net in _BLOCKED_NETWORKS)
    )


def _is_safe_media_url_sync(url: str) -> bool:
    """同步检查：scheme/端口/字面IP。域名需另行DNS解析检查。"""
    try:
        parsed = urllib.parse.urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if (
        not host
        or host == "localhost"
        or "%" in host
        or parsed.username is not None
        or parsed.password is not None
    ):
        return False
    # P1-6: 端口白名单
    if port is not None and port not in _ALLOWED_PORTS:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # 域名，需要DNS解析检查
    return _is_safe_ip(ip)


async def _check_dns_resolution(host: str) -> bool:
    """P1-6: DNS解析后检查所有地址，拒绝解析到内网/回环的域名。"""
    return bool(await _resolve_public_addresses(host))


async def _resolve_public_addresses(host: str) -> list[str]:
    """Resolve all addresses once, reject the whole answer if any address is unsafe."""
    loop = asyncio.get_event_loop()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), 5)
    except (socket.gaierror, OSError, TimeoutError):
        return []
    addresses: list[str] = []
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return []
        if not _is_safe_ip(ip):
            return []
        if str(ip) not in addresses:
            addresses.append(str(ip))
    return addresses


async def _is_safe_media_url(url: str) -> bool:
    """P1-6: 完整SSRF检查——scheme/端口/字面IP/DNS解析后全地址。"""
    return await _pin_media_url(url) is not None


async def _pin_media_url(url: str) -> tuple[httpx.URL, str, str] | None:
    """Connect to a checked literal IP, while retaining Host and TLS verification identity.

    HTTPX forwards ``sni_hostname`` to httpcore's TLS ``server_hostname``; both SNI
    and certificate checking use the original hostname. DNS is never repeated by
    the transport. Callers construct the dedicated media client with trust_env=False.
    """
    if not _is_safe_media_url_sync(url):
        return None
    try:
        original = httpx.URL(url)
        host = original.host
        try:
            addresses = [str(ipaddress.ip_address(host))]
        except ValueError:
            addresses = await _resolve_public_addresses(host)
        if not addresses:
            return None
        host_header = original.netloc.decode("ascii")
        return original.copy_with(host=addresses[0]), host_header, host
    except (ValueError, httpx.InvalidURL):
        return None


async def download_attachment(
    client: httpx.AsyncClient,
    url: str,
    media_dir: Path,
    message_id: str,
    idx: int,
    content_type: str,
    *,
    quota_bytes: int = MEDIA_QUOTA_BYTES,
) -> tuple[str | None, str, str]:
    """下载单个附件。返回 (filename, ext, reason)。

    filename=None 表示未下载（reason 给出原因：配额/大小/网络）。
    """
    if not url:
        return None, "", "无URL"
    if url.startswith("//"):
        url = "https:" + url
    if not _is_safe_media_url_sync(url):
        return None, "", "URL被SSRF防护拦截（内网/回环/DNS解析到内网）"

    ensure_media_dir(media_dir)
    async with _QUOTA_LOCK:
        if total_media_size(media_dir) >= quota_bytes:
            return None, "", "磁盘配额已满"

        kind = "video" if content_type.startswith("video/") else "default"
        limit = _LIMITS_BY_KIND.get(kind, DEFAULT_FILE_LIMIT)

        # 先流式下载到 .bin 临时，再嗅探扩展名重命名
        stem = safe_filename(message_id, idx)
        tmp_path = media_dir / f"{stem}.bin"
        ok, reason, _bytes = await stream_download(
            client,
            url,
            tmp_path,
            size_limit=limit,
            media_dir=media_dir,
            quota_bytes=quota_bytes,
        )
        if not ok:
            tmp_path.unlink(missing_ok=True)
            return None, "", reason or "下载失败"
        data_head = sniff_file_head(tmp_path)
        ext = sniff_ext(data_head, content_type)
        final_path = media_dir / f"{stem}{ext}"
        try:
            tmp_path.replace(final_path)
        except OSError:
            final_path = tmp_path.with_suffix(ext)
            tmp_path.rename(final_path)
        return final_path.name, ext, ""


def purge_media(media_dir: Path, retention_days: int = 30, now: float | None = None) -> int:
    """删除过期媒体并返回计数；真实 I/O 失败向上传播，不伪报清理成功。

    文件可能已被并发清理，FileNotFoundError 可忽略。其他错误必须交给
    maintenance 记录固定失败码；此前已删除的文件不会随数据库回滚而恢复。
    """
    if not media_dir.exists():
        return 0
    cutoff = (now or time.time()) - retention_days * 86400
    deleted = 0
    for f in media_dir.iterdir():
        if not f.is_file() or f.suffix == ".part":
            continue
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                deleted += 1
        except FileNotFoundError:
            continue
    return deleted


def media_cleanup_due(now: datetime | None = None) -> bool:
    """是否到了媒体清理时机（保留期外文件存在即应清理）；调度由部署层驱动。"""
    return True
