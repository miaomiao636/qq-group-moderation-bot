"""Set and verify finite Windows service recovery. Does not install or start services."""

from __future__ import annotations

import argparse
import ctypes as ct
import re
import sys
from typing import Any


class Action(ct.Structure):
    _fields_ = [("kind", ct.c_uint32), ("delay", ct.c_uint32)]


class FailureActions(ct.Structure):
    _fields_ = [
        ("reset", ct.c_uint32),
        ("reboot_message", ct.c_wchar_p),
        ("command", ct.c_wchar_p),
        ("count", ct.c_uint32),
        ("actions", ct.POINTER(Action)),
    ]


class FailureFlag(ct.Structure):
    _fields_ = [("enabled", ct.c_int32)]


def _api() -> Any:
    if sys.platform != "win32":
        raise RuntimeError("WINDOWS_REQUIRED")
    api = ct.WinDLL("advapi32", use_last_error=True)
    api.OpenSCManagerW.argtypes = [ct.c_wchar_p, ct.c_wchar_p, ct.c_uint32]
    api.OpenSCManagerW.restype = ct.c_void_p
    api.OpenServiceW.argtypes = [ct.c_void_p, ct.c_wchar_p, ct.c_uint32]
    api.OpenServiceW.restype = ct.c_void_p
    api.ChangeServiceConfig2W.argtypes = [ct.c_void_p, ct.c_uint32, ct.c_void_p]
    api.ChangeServiceConfig2W.restype = ct.c_int32
    api.QueryServiceConfig2W.argtypes = [
        ct.c_void_p,
        ct.c_uint32,
        ct.c_void_p,
        ct.c_uint32,
        ct.POINTER(ct.c_uint32),
    ]
    api.QueryServiceConfig2W.restype = ct.c_int32
    api.CloseServiceHandle.argtypes = [ct.c_void_p]
    api.CloseServiceHandle.restype = ct.c_int32
    return api


def configure(name: str, *, api: Any = None) -> None:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", name):
        raise ValueError("INVALID_SERVICE_NAME")
    api = api if api is not None else _api()
    manager = api.OpenSCManagerW(None, None, 1)
    if not manager:
        raise RuntimeError("SERVICE_MANAGER_OPEN_FAILED")
    service = None
    try:
        # QUERY_CONFIG | CHANGE_CONFIG | START (required by restart actions).
        service = api.OpenServiceW(manager, name, 0x13)
        if not service:
            raise RuntimeError("SERVICE_OPEN_FAILED")
        expected = [(1, 5000), (1, 15000), (0, 0)]
        actions = (Action * 3)(*(Action(*item) for item in expected))
        policy = FailureActions(86400, "", "", 3, actions)
        flag = FailureFlag(1)
        for level, value in ((2, policy), (4, flag)):
            if not api.ChangeServiceConfig2W(service, level, ct.byref(value)):
                raise RuntimeError("SERVICE_RECOVERY_WRITE_FAILED")
        # Windows documents an 8 KiB maximum for QueryServiceConfig2W.
        for level in (2, 4):
            buffer = ct.create_string_buffer(8192)
            needed = ct.c_uint32()
            if not api.QueryServiceConfig2W(service, level, buffer, len(buffer), ct.byref(needed)):
                raise RuntimeError("SERVICE_RECOVERY_READ_FAILED")
            if level == 2:
                actual = ct.cast(buffer, ct.POINTER(FailureActions)).contents
                if (
                    actual.reset != 86400
                    or actual.count != 3
                    or not actual.actions
                    or [(actual.actions[i].kind, actual.actions[i].delay) for i in range(3)]
                    != expected
                    or actual.command
                    or actual.reboot_message
                ):
                    raise RuntimeError("SERVICE_RECOVERY_MISMATCH")
            elif ct.cast(buffer, ct.POINTER(FailureFlag)).contents.enabled != 1:
                raise RuntimeError("SERVICE_RECOVERY_MISMATCH")
    finally:
        if service:
            api.CloseServiceHandle(service)
        api.CloseServiceHandle(manager)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service")
    args = parser.parse_args()
    try:
        configure(args.service)
    except (OSError, ValueError, RuntimeError):
        print("SERVICE_RECOVERY_NOT_VERIFIED")
        return 1
    print("SERVICE_RECOVERY_CONFIG_VERIFIED; reboot and fault-recovery acceptance remain required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
