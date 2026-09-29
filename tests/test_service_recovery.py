"""Windows service configuration contract, using an in-memory API only."""

import ctypes as ct

import pytest


class FakeApi:
    def __init__(self, fail=None, mismatch=False):
        self.fail, self.mismatch = fail, mismatch
        self.closed = []
        self.written = {}
        self.keep = []

    def OpenSCManagerW(self, machine, database, access):
        assert (machine, database, access) == (None, None, 1)
        return 0 if self.fail == "manager" else 101

    def OpenServiceW(self, manager, name, access):
        assert (manager, name, access) == (101, "FixtureWeb", 0x13)
        return 0 if self.fail == "service" else 102

    def CloseServiceHandle(self, handle):
        self.closed.append(handle)
        return 1

    def ChangeServiceConfig2W(self, handle, level, pointer):
        from app.service_recovery import FailureActions, FailureFlag

        assert handle == 102
        if level == 2:
            value = ct.cast(pointer, ct.POINTER(FailureActions)).contents
            self.written[2] = (
                value.reset,
                [(value.actions[i].kind, value.actions[i].delay) for i in range(value.count)],
            )
        else:
            self.written[4] = ct.cast(pointer, ct.POINTER(FailureFlag)).contents.enabled
        return 0 if self.fail == f"write{level}" else 1

    def QueryServiceConfig2W(self, handle, level, buffer, size, needed):
        from app.service_recovery import Action, FailureActions, FailureFlag

        assert handle == 102 and size == 8192
        if self.fail == f"read{level}":
            return 0
        if level == 2:
            reset, actions = self.written[2]
            if self.mismatch:
                actions[-1] = (1, 5000)
            array = (Action * 3)(*(Action(*a) for a in actions))
            self.keep.append(array)
            value = FailureActions(reset, None, None, 3, array)
        else:
            value = FailureFlag(self.written[4])
        ct.memmove(buffer, ct.byref(value), ct.sizeof(value))
        return 1


def test_two_backoff_restarts_then_none_are_written_and_read_back():
    from app.service_recovery import configure

    api = FakeApi()
    configure("FixtureWeb", api=api)
    assert api.written == {2: (86400, [(1, 5000), (1, 15000), (0, 0)]), 4: 1}
    assert api.closed == [102, 101]


@pytest.mark.parametrize("failure", ["manager", "service", "write2", "write4", "read2", "read4"])
def test_configuration_failure_never_succeeds_and_closes_handles(failure):
    from app.service_recovery import configure

    api = FakeApi(fail=failure)
    with pytest.raises(RuntimeError):
        configure("FixtureWeb", api=api)
    assert api.closed == (
        [] if failure == "manager" else [101] if failure == "service" else [102, 101]
    )


def test_readback_must_include_stop_after_second_retry():
    from app.service_recovery import configure

    with pytest.raises(RuntimeError):
        configure("FixtureWeb", api=FakeApi(mismatch=True))


@pytest.mark.parametrize("name", ["", "../x", "a b", "a;Stop", "a" * 81])
def test_invalid_service_name_is_rejected_before_api(name):
    from app.service_recovery import configure

    with pytest.raises(ValueError):
        configure(name, api=FakeApi())
