"""No device is contacted by these adapter and process-lock regression tests."""

import threading

import pytest
from app.phone_inspector.device import DeviceIdentity, Paused, Phone
from app.phone_inspector.locks import FileLock
from app.phone_inspector.pages import InspectionError, Page, Rect


def test_pause_before_command_prevents_subprocess(monkeypatch):
    phone = object.__new__(Phone)
    phone.stop = threading.Event()
    phone.stop.set()
    monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("Unexpected subprocess"))
    with pytest.raises(Paused):
        phone._run("anything")


def test_changed_profile_or_unapproved_button_cannot_be_tapped(monkeypatch):
    phone = object.__new__(Phone)
    back = Rect(1, 1, 10, 10)
    expected = Page(kind="profile", qq="12345601", back=back)
    monkeypatch.setattr(phone, "snapshot", lambda: Page(kind="profile", qq="12345602", back=back))
    monkeypatch.setattr(phone, "adb_call", lambda *a: pytest.fail("Unexpected phone input"))
    with pytest.raises(InspectionError, match="页面已变化"):
        phone.tap(expected, back)
    monkeypatch.setattr(phone, "snapshot", lambda: expected)
    with pytest.raises(InspectionError, match="允许"):
        phone.tap(expected, Rect(200, 1000, 400, 1100))


@pytest.mark.parametrize(
    "user,power,policy",
    [
        (0, b"mWakefulness=Awake", b"showing=false"),
        (999, b"mWakefulness=Asleep", b"showing=false"),
        (999, b"mWakefulness=Awake", b"showing=true"),
    ],
)
def test_changed_clone_sleep_or_lock_pauses(monkeypatch, user, power, policy):
    phone = object.__new__(Phone)
    phone.identity = DeviceIdentity("fake", 999, "9.3.60")
    focus = f"mCurrentFocus=Window{{abc u{user} com.tencent.mobileqq/com.tencent.mobileqq.activity.TroopMemberListActivity}}".encode()

    def fake_call(*args):
        return policy if args[-1] == "policy" else power if args[-1] == "power" else focus

    monkeypatch.setattr(phone, "adb_call", fake_call)
    with pytest.raises(InspectionError):
        phone.foreground()


def test_device_lock_excludes_second_writer_and_releases_after_exception(tmp_path):
    target = tmp_path / "device.lock"
    with pytest.raises(ValueError):
        with FileLock(target), pytest.raises(InspectionError), FileLock(target):
            pytest.fail("Two device writers acquired same lock")
        raise ValueError("synthetic interruption")
    with FileLock(target):
        pass


def test_reveal_cover_does_not_swipe_unknown_or_changed_page(monkeypatch):
    phone = object.__new__(Phone)
    cover = Page(kind="profile_cover", profile_name="合成名", viewport=Rect(0, 228, 1080, 2128))
    monkeypatch.setattr(phone, "snapshot", lambda: Page())
    monkeypatch.setattr(phone, "adb_call", lambda *a: pytest.fail("Unexpected phone input"))
    with pytest.raises(InspectionError):
        phone.reveal_profile(cover)
    monkeypatch.setattr(phone, "snapshot", lambda: cover)
    with pytest.raises(InspectionError):
        phone.reveal_profile(
            Page(kind="profile_cover", profile_name="其他名", viewport=cover.viewport)
        )
