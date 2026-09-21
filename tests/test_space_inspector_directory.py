"""Internal directory regressions; synthetic HTTP responses, not reviewer probes."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from app.space_inspector import directory as module
from app.space_inspector.contracts import Group, InspectionError
from app.space_inspector.directory import Directory, from_local_config


def response(data: object) -> httpx.Response:
    return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": data})


def test_groups_are_account_checked_and_duplicates_are_removed() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers["authorization"] == "Bearer synthetic-token"
        assert json.loads(request.content) == {}
        if request.url.path == "/get_login_info":
            return response({"user_id": 12345678, "nickname": "not retained"})
        return response(
            [
                {"group_id": 23456789, "group_name": "合成群", "member_count": 2},
                {"group_id": "23456789", "group_name": "合成群", "member_count": 2},
            ]
        )

    directory = Directory(
        "12345678", "http://127.0.0.1:3000", "synthetic-token", httpx.MockTransport(handle)
    )
    try:
        assert directory.groups() == [Group("23456789", "合成群", 2)]
        assert calls == ["/get_login_info", "/get_group_list", "/get_login_info"]
    finally:
        directory.close()


def test_account_change_after_member_read_discards_the_list() -> None:
    logins = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal logins
        if request.url.path == "/get_login_info":
            logins += 1
            return response({"user_id": 12345678 if logins == 1 else 87654321})
        return response([{"group_id": 23456789, "user_id": 34567890}])

    directory = Directory(
        "12345678", "http://127.0.0.1:3000", "synthetic-token", httpx.MockTransport(handle)
    )
    try:
        with pytest.raises(InspectionError, match="账号"):
            directory.members("23456789")
    finally:
        directory.close()


def directory_for_data(data: object) -> Directory:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/get_login_info":
            return response({"user_id": "12345678"})
        return response(data)

    return Directory("12345678", "http://127.0.0.1:3000", "", httpx.MockTransport(handle))


def test_group_name_can_include_joined_emoji() -> None:
    directory = directory_for_data(
        [{"group_id": 23456789, "group_name": "家人\U0001f468\u200d\U0001f469", "member_count": 2}]
    )
    try:
        assert directory.groups()[0].name == "家人\U0001f468\u200d\U0001f469"
    finally:
        directory.close()


def test_directory_checks_deadline_between_small_response_chunks(monkeypatch):
    clock = [0.0]
    consumed = []

    class DrippingResponse(httpx.SyncByteStream):
        def __iter__(self):
            for index in range(10):
                clock[0] += 5.0
                consumed.append(index)
                yield b"x"

    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, request=request, stream=DrippingResponse())
    )
    directory = Directory("11111111", "http://127.0.0.1:3000", "", transport=transport)
    try:
        with pytest.raises(InspectionError, match="超时"):
            directory.verify_identity()
        assert len(consumed) == 5
        assert clock[0] == 25.0
    finally:
        directory.close()


def test_members_keep_only_numeric_id_and_exact_group() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.method == "POST"
        if request.url.path == "/get_login_info":
            return response({"user_id": "12345678"})
        assert json.loads(request.content) == {"group_id": 23456789, "no_cache": True}
        return response(
            [
                {"group_id": 23456789, "user_id": "34567890", "nickname": "discard me"},
                {"group_id": "23456789", "user_id": 34567890, "avatar": "https://invalid"},
                {"group_id": 23456789, "user_id": 45678901},
            ]
        )

    directory = Directory("12345678", "http://localhost:3000/", "", httpx.MockTransport(handle))
    try:
        assert directory.members("23456789") == ["34567890", "45678901"]
        assert calls == ["/get_login_info", "/get_group_member_list", "/get_login_info"]
        assert "discard me" not in repr(directory)
    finally:
        directory.close()


@pytest.mark.parametrize("operation", ["groups", "members"])
def test_wrong_account_before_read_sends_no_directory_request(operation: str) -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return response({"user_id": 87654321})

    directory = Directory("12345678", "http://127.0.0.1:3000", "", httpx.MockTransport(handle))
    try:
        with pytest.raises(InspectionError, match="账号"):
            directory.groups() if operation == "groups" else directory.members("23456789")
        assert calls == ["/get_login_info"]
    finally:
        directory.close()


def test_account_change_after_group_read_discards_the_list() -> None:
    logins = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal logins
        if request.url.path == "/get_login_info":
            logins += 1
            return response({"user_id": 12345678 if logins == 1 else 87654321})
        return response([{"group_id": 23456789, "group_name": "合成群", "member_count": 0}])

    directory = Directory("12345678", "http://127.0.0.1:3000", "", httpx.MockTransport(handle))
    try:
        with pytest.raises(InspectionError, match="账号"):
            directory.groups()
    finally:
        directory.close()


@pytest.mark.parametrize(
    "bad_id", [True, False, 12345678.0, None, "012345678", "12345\n", "../12345678"]
)
def test_invalid_login_identity_fails_closed(bad_id: object) -> None:
    directory = Directory(
        "12345678",
        "http://127.0.0.1:3000",
        "",
        httpx.MockTransport(lambda _: response({"user_id": bad_id})),
    )
    try:
        with pytest.raises(InspectionError):
            directory.verify_identity()
    finally:
        directory.close()


@pytest.mark.parametrize("bad_id", [True, 23456789.0, "023456789", None])
def test_invalid_group_ids_are_rejected(bad_id: object) -> None:
    directory = directory_for_data(
        [{"group_id": bad_id, "group_name": "合成群", "member_count": 0}]
    )
    try:
        with pytest.raises(InspectionError):
            directory.groups()
    finally:
        directory.close()


@pytest.mark.parametrize(
    "data",
    [
        [{"group_id": 45678901, "user_id": 34567890}],
        [{"user_id": 34567890}],
        [{"group_id": True, "user_id": 34567890}],
        [{"group_id": 23456789, "user_id": True}],
        [{"group_id": 23456789, "user_id": 34567890.0}],
        [{"group_id": 23456789, "user_id": None}],
        [None],
        {"user_id": 34567890},
    ],
)
def test_invalid_member_rows_never_return_partial_results(data: object) -> None:
    directory = directory_for_data(data)
    try:
        with pytest.raises(InspectionError):
            directory.members("23456789")
    finally:
        directory.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("member_count", True),
        ("member_count", 1.0),
        ("member_count", "1"),
        ("member_count", -1),
        ("member_count", None),
        ("group_name", None),
        ("group_name", "x" * 513),
        ("group_name", "a\nb"),
        ("group_name", "a\x00b"),
        ("group_name", "a\u202eb"),
    ],
)
def test_bad_group_metadata_is_not_silently_coerced(field: str, value: object) -> None:
    row = {"group_id": 23456789, "group_name": "合成群", "member_count": 0, field: value}
    directory = directory_for_data([row])
    try:
        with pytest.raises(InspectionError):
            directory.groups()
    finally:
        directory.close()


def test_conflicting_duplicate_group_metadata_is_rejected() -> None:
    directory = directory_for_data(
        [
            {"group_id": 23456789, "group_name": "A", "member_count": 1},
            {"group_id": 23456789, "group_name": "B", "member_count": 1},
        ]
    )
    try:
        with pytest.raises(InspectionError, match="冲突"):
            directory.groups()
    finally:
        directory.close()


@pytest.mark.parametrize("operation,size", [("groups", 10001), ("members", 5001)])
def test_raw_list_limits_apply_before_deduplication(operation: str, size: int) -> None:
    row = {"group_id": 23456789, "group_name": "合成群", "member_count": 1, "user_id": 34567890}
    directory = directory_for_data([row] * size)
    try:
        with pytest.raises(InspectionError, match="限制"):
            directory.groups() if operation == "groups" else directory.members("23456789")
    finally:
        directory.close()


@pytest.mark.parametrize(
    "base_url",
    [
        "https://127.0.0.1:3000",
        "http://example.invalid:3000",
        "http://192.168.1.2:3000",
        "http://0.0.0.0:3000",
        "http://[::]:3000",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://token@127.0.0.1:3000",
        "http://127.0.0.1:3000/api",
        "http://127.0.0.1:3000/?token=x",
        "http://127.0.0.1:3000/#x",
        " http://127.0.0.1:3000",
        "http://127.0.0.1:3000\n",
        "http://localhost.evil.invalid:3000",
        "http://[::1%25zone]:3000",
    ],
)
def test_only_unambiguous_loopback_http_urls_are_accepted(base_url: str) -> None:
    with pytest.raises(InspectionError, match="回环"):
        Directory("12345678", base_url, "synthetic-secret")


@pytest.mark.parametrize(
    "base_url", ["http://localhost:3000", "http://127.0.0.2:3000", "http://[::1]:3000"]
)
def test_loopback_hosts_work_without_exposing_credentials(base_url: str) -> None:
    directory = Directory(
        "12345678",
        base_url,
        "synthetic-secret",
        httpx.MockTransport(lambda _: response({"user_id": 12345678})),
    )
    try:
        directory.verify_identity()
        assert repr(directory) == "Directory(self_id='12345678')"
        assert "synthetic-secret" not in str(directory)
    finally:
        directory.close()


def test_redirects_are_never_followed() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(307, headers={"location": "http://example.invalid/steal"})

    directory = Directory(
        "12345678", "http://127.0.0.1:3000", "synthetic-secret", httpx.MockTransport(handle)
    )
    try:
        with pytest.raises(InspectionError):
            directory.groups()
        assert len(calls) == 1
        assert calls[0] == "http://127.0.0.1:3000/get_login_info"
    finally:
        directory.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "ok", "retcode": False, "data": {}},
        {"status": "ok", "retcode": 0.0, "data": {}},
        {"status": "ok", "retcode": "0", "data": {}},
        {"status": "failed", "retcode": 0, "data": {}},
        {"status": "async", "retcode": 1, "data": {}},
        {"status": "ok", "retcode": 1, "data": {}},
        {"status": "ok", "retcode": 0},
        [],
        None,
    ],
)
def test_only_explicit_success_envelopes_are_accepted(payload: object) -> None:
    directory = Directory(
        "12345678",
        "http://127.0.0.1:3000",
        "",
        httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    )
    try:
        with pytest.raises(InspectionError):
            directory.verify_identity()
    finally:
        directory.close()


def test_remote_errors_and_transport_errors_are_redacted() -> None:
    secret = "synthetic-secret"

    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"{secret} {request.url}", request=request)

    directory = Directory("12345678", "http://127.0.0.1:3000", secret, httpx.MockTransport(handle))
    try:
        with pytest.raises(InspectionError) as caught:
            directory.verify_identity()
        assert secret not in str(caught.value)
        assert "http" not in str(caught.value)
        assert caught.value.__suppress_context__
    finally:
        directory.close()


def test_response_stream_is_bounded_and_closed() -> None:
    class Oversized(httpx.SyncByteStream):
        count = 0
        closed = False

        def __iter__(self):
            for _ in range(400):
                self.count += 1
                yield b"x" * 65536

        def close(self) -> None:
            self.closed = True

    stream = Oversized()
    directory = Directory(
        "12345678",
        "http://127.0.0.1:3000",
        "",
        httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
    )
    try:
        with pytest.raises(InspectionError, match="大小限制"):
            directory.verify_identity()
        assert stream.count == 321
        assert stream.closed
    finally:
        directory.close()


@pytest.mark.parametrize(
    "headers",
    [
        {"content-length": str(20 * 1024 * 1024 + 1)},
        {"content-length": "wrong"},
        {"content-encoding": "gzip"},
    ],
)
def test_unsupported_response_headers_stop_before_body(headers: dict[str, str]) -> None:
    directory = Directory(
        "12345678",
        "http://127.0.0.1:3000",
        "",
        httpx.MockTransport(
            lambda _: httpx.Response(200, headers=headers, stream=httpx.ByteStream(b""))
        ),
    )
    try:
        with pytest.raises(InspectionError):
            directory.verify_identity()
    finally:
        directory.close()


def test_stream_deadline_stops_slow_drip(monkeypatch: pytest.MonkeyPatch) -> None:
    readings = iter([0.0, 21.0])
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(readings)))
    directory = Directory(
        "12345678",
        "http://127.0.0.1:3000",
        "",
        httpx.MockTransport(lambda _: response({"user_id": 12345678})),
    )
    try:
        with pytest.raises(InspectionError, match="超时"):
            directory.verify_identity()
    finally:
        directory.close()


def test_no_action_outside_read_allowlist_and_close_is_idempotent() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return response(None)

    directory = Directory("12345678", "http://127.0.0.1:3000", "", httpx.MockTransport(handle))
    with pytest.raises(InspectionError, match="只读"):
        directory._call("set_group_kick", {"group_id": 23456789, "user_id": 34567890})
    directory.close()
    directory.close()
    with pytest.raises(InspectionError, match="关闭"):
        directory.verify_identity()
    assert calls == []


def write_config(folder: Path, servers: object, self_id: str = "12345678") -> None:
    (folder / f"onebot11_{self_id}.json").write_text(
        json.dumps({"network": {"httpServers": servers}}), encoding="utf-8"
    )


def test_config_uses_only_exact_account_and_keeps_live_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(
        tmp_path,
        [{"enable": True, "host": "localhost", "port": "3000", "token": "synthetic-secret"}],
    )
    write_config(
        tmp_path,
        [{"enable": True, "host": "127.0.0.1", "port": 9000, "token": "other-secret"}],
        "87654321",
    )
    real_client = httpx.Client
    captured: dict[str, Any] = {}

    def client(**kwargs: Any) -> httpx.Client:
        captured.update(kwargs)
        kwargs["transport"] = httpx.MockTransport(lambda _: response({"user_id": 87654321}))
        return real_client(**kwargs)

    monkeypatch.setattr(module.httpx, "Client", client)
    directory = from_local_config(tmp_path, "12345678")
    try:
        assert captured["trust_env"] is False
        assert captured["follow_redirects"] is False
        assert captured["base_url"] == "http://127.0.0.1:3000"
        with pytest.raises(InspectionError, match="账号"):
            directory.groups()
    finally:
        directory.close()


def test_missing_matching_config_never_falls_back(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        [{"enable": True, "host": "127.0.0.1", "port": 9000, "token": "other-secret"}],
        "87654321",
    )
    with pytest.raises(InspectionError) as caught:
        from_local_config(tmp_path, "12345678")
    assert "other-secret" not in str(caught.value)


@pytest.mark.parametrize(
    "servers",
    [
        [],
        [{"enable": False, "host": "127.0.0.1", "port": 3000}],
        [{"enable": "true", "host": "127.0.0.1", "port": 3000}],
        [{"enable": True, "host": "0.0.0.0", "port": 3000}],
        [{"enable": True, "host": "127.0.0.1", "port": True}],
        [{"enable": True, "host": "127.0.0.1", "port": 3000.0}],
        [{"enable": True, "host": "127.0.0.1", "port": 0}],
        [{"enable": True, "host": "127.0.0.1", "port": 65536}],
        [{"enable": True, "host": "127.0.0.1", "port": 3000, "token": "x\r\nAuthorization: y"}],
        [
            {"enable": True, "host": "127.0.0.1", "port": 3000},
            {"enable": True, "host": "127.0.0.1", "port": 3001},
        ],
    ],
)
def test_invalid_config_is_rejected_without_fallback(tmp_path: Path, servers: object) -> None:
    write_config(tmp_path, servers)
    with pytest.raises(InspectionError):
        from_local_config(tmp_path, "12345678")


def test_config_size_limit_and_path_traversal(tmp_path: Path) -> None:
    (tmp_path / "onebot11_12345678.json").write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(InspectionError):
        from_local_config(tmp_path, "12345678")
    with pytest.raises(InspectionError):
        from_local_config(tmp_path, "../12345678")
