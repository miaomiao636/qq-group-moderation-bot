"""Bounded, read-only OneBot directory with live account checks around each read."""

from __future__ import annotations

import ipaddress
import json
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.space_inspector.contracts import Group, InspectionError, numeric_id

_MAX_RESPONSE_BYTES = 20 * 1024 * 1024
_MAX_CONFIG_BYTES = 1024 * 1024
_MAX_GROUPS = 10_000
_MAX_MEMBERS = 5_000
_READ_ACTIONS = frozenset({"get_login_info", "get_group_list", "get_group_member_list"})


def _base_url(value: str) -> str:
    """Resolve localhost without DNS; reject credentials and ambiguous URL forms."""
    try:
        if (
            not isinstance(value, str)
            or len(value) > 2048
            or value.strip() != value
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError
        parts = urlsplit(value)
        if (
            parts.scheme != "http"
            or parts.username is not None
            or parts.password is not None
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
            or not parts.hostname
        ):
            raise ValueError
        host = "127.0.0.1" if parts.hostname == "localhost" else parts.hostname
        address = ipaddress.ip_address(host)
        if not address.is_loopback or "%" in host:
            raise ValueError
        port = 80 if parts.port is None else parts.port
        if not 1 <= port <= 65535:
            raise ValueError
        formatted_host = f"[{address}]" if address.version == 6 else str(address)
        return f"http://{formatted_host}:{port}"
    except (TypeError, ValueError):
        raise InspectionError("群目录接口必须是明确的本机回环 HTTP 地址。") from None


def _token(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 8192
        or any(ord(char) < 33 or ord(char) > 126 for char in value)
    ):
        raise InspectionError("群目录接口凭据格式无效。")
    return value


class Directory:
    """No credential or third-party response is included in displayable errors."""

    def __init__(
        self,
        self_id: str,
        base_url: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.self_id = numeric_id(self_id)
        base_url = _base_url(base_url)
        token = _token(token)
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=httpx.Timeout(10.0, connect=5.0),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self._closed = False

    def __repr__(self) -> str:
        return f"Directory(self_id={self.self_id!r})"

    def _call(self, action: str, params: dict[str, object]) -> object:
        if self._closed:
            raise InspectionError("群目录连接已关闭，请重新连接。")
        if action not in _READ_ACTIONS:
            raise InspectionError("群目录仅允许指定的只读查询。")
        body = json.dumps(params, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        if len(body) > 4096:
            raise InspectionError("群目录请求超出大小限制。")
        deadline = time.monotonic() + 20.0
        try:
            with self._client.stream(
                "POST", "/" + action, content=body, headers={"Content-Type": "application/json"}
            ) as response:
                if response.status_code != 200:
                    raise InspectionError("群目录接口未返回成功响应，请检查本机连接。")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise InspectionError("群目录接口返回了不受支持的压缩响应。")
                length = response.headers.get("content-length")
                if length is not None and (
                    not re.fullmatch(r"[0-9]{1,12}", length) or int(length) > _MAX_RESPONSE_BYTES
                ):
                    raise InspectionError("群目录响应超出大小限制或长度无效。")
                content = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() > deadline:
                        raise InspectionError("群目录读取超时，请稍后重试。")
                    if len(content) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise InspectionError("群目录响应超出大小限制。")
                    content.extend(chunk)
                if time.monotonic() > deadline:
                    raise InspectionError("群目录读取超时，请稍后重试。")
            payload = json.loads(content)
        except InspectionError:
            raise
        except (httpx.HTTPError, OSError, ValueError, UnicodeError, RecursionError):
            raise InspectionError("群目录读取失败，请检查连接或接口配置。") from None
        if (
            not isinstance(payload, dict)
            or payload.get("status") != "ok"
            or type(payload.get("retcode")) is not int
            or payload["retcode"] != 0
            or "data" not in payload
        ):
            raise InspectionError("群目录接口未确认查询成功，已停止读取。")
        return payload["data"]

    def verify_identity(self) -> None:
        data = self._call("get_login_info", {})
        if not isinstance(data, dict) or numeric_id(data.get("user_id")) != self.self_id:
            raise InspectionError("群目录当前登录账号与指定账号不一致，已停止读取。")

    def groups(self) -> list[Group]:
        self.verify_identity()
        data = self._call("get_group_list", {})
        self.verify_identity()
        if not isinstance(data, list) or len(data) > _MAX_GROUPS:
            raise InspectionError("群目录格式无效或群数量超出限制。")
        groups: dict[str, Group] = {}
        for row in data:
            if not isinstance(row, dict):
                raise InspectionError("群目录条目格式无效。")
            group_id = numeric_id(row.get("group_id"))
            name, count = row.get("group_name"), row.get("member_count")
            if (
                not isinstance(name, str)
                or len(name) > 512
                or any(
                    unicodedata.category(char).startswith("C") and char not in {"\u200c", "\u200d"}
                    for char in name
                )
                or type(count) is not int
                or count < 0
            ):
                raise InspectionError("群名称或群人数格式无效。")
            group = Group(group_id, name, count)
            previous = groups.get(group_id)
            if previous is not None and previous != group:
                raise InspectionError("群目录存在同群信息冲突，请重新读取。")
            groups[group_id] = group
        return list(groups.values())

    def members(self, group_id: str) -> list[str]:
        group_id = numeric_id(group_id)
        self.verify_identity()
        data = self._call("get_group_member_list", {"group_id": int(group_id), "no_cache": True})
        self.verify_identity()
        if not isinstance(data, list) or len(data) > _MAX_MEMBERS:
            raise InspectionError("群成员格式无效或成员数量超出限制。")
        members: dict[str, None] = {}
        for row in data:
            if not isinstance(row, dict) or numeric_id(row.get("group_id")) != group_id:
                raise InspectionError("群成员来源与指定群不一致，已停止读取。")
            members[numeric_id(row.get("user_id"))] = None
        return list(members)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._client.close()


def from_local_config(config_dir: Path, self_id: str) -> Directory:
    """Read one account's file; never discover/fallback to other accounts or servers."""
    self_id = numeric_id(self_id)
    try:
        with (config_dir / f"onebot11_{self_id}.json").open("rb") as source:
            raw = source.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            raise ValueError
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("network"), dict):
            raise ValueError
        servers = data["network"].get("httpServers", [])
        if isinstance(servers, dict):
            servers = [servers]
        if not isinstance(servers, list) or len(servers) > 100:
            raise ValueError
        enabled: list[dict[str, object]] = []
        for server in servers:
            if not isinstance(server, dict) or type(server.get("enable", False)) is not bool:
                raise ValueError
            if server.get("enable") is True:
                enabled.append(server)
        if len(enabled) != 1:
            raise InspectionError("指定账号必须只有一个已启用的本机 HTTP 群目录接口。")
        host = enabled[0].get("host", "127.0.0.1")
        port = enabled[0].get("port")
        token = enabled[0].get("token", "")
        if (
            not isinstance(host, str)
            or not isinstance(token, str)
            or not isinstance(port, (int, str))
            or isinstance(port, bool)
            or not re.fullmatch(r"[0-9]{1,5}", str(port))
            or not 1 <= int(port) <= 65535
        ):
            raise ValueError
        formatted_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        return Directory(self_id, f"http://{formatted_host}:{port}", token)
    except InspectionError:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        raise InspectionError("无法读取指定账号的群目录配置，请检查本机配置。") from None
