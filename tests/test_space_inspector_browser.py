"""Internal regressions, not external reviewer probes; all identities are synthetic."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from app.space_inspector.browser import NOTICE, Browser, classify_page
from app.space_inspector.contracts import BLOCKED, RESTRICTED, UNCONFIRMED, InspectionError

QQ, VIEWER = "12345678", "98765432"


def page() -> dict[str, object]:
    return {
        "url": f"https://user.qzone.qq.com/{QQ}",
        "ready": "complete",
        "viewers": [VIEWER],
        "normal_profile": False,
        "panels": [{"paragraphs": ["温馨提示:", NOTICE, "返回我的空间"], "report_icons": 1}],
    }


def test_verified_qzone_system_notice_is_observed() -> None:
    result = classify_page(page(), QQ, VIEWER)
    assert result.status == RESTRICTED
    assert result.evidence["notice"] == NOTICE
    assert result.evidence["viewer_qq"] == VIEWER


def test_real_punctuation_is_required_not_an_invented_warning() -> None:
    raw = page()
    raw["panels"] = [
        {"paragraphs": ["温馨提示:", NOTICE.replace("！", "!"), "返回我的空间"], "report_icons": 1}
    ]
    assert classify_page(raw, QQ, VIEWER).status == BLOCKED


def test_loaded_profile_without_warning_remains_unconfirmed() -> None:
    raw = page()
    raw.update(panels=[], normal_profile=True)
    result = classify_page(raw, QQ, VIEWER)
    assert result.status == UNCONFIRMED
    assert result.evidence["notice"] == ""


@pytest.mark.parametrize(
    "url",
    [
        "https://user.qzone.qq.com/22345678",
        "https://user.qzone.qq.com.evil.test/12345678",
        "http://user.qzone.qq.com/12345678",
        "https://secret@user.qzone.qq.com/12345678",
        "https://user.qzone.qq.com/12345678?token=SECRET",
        "https://user.qzone.qq.com/12345678#other",
    ],
)
def test_other_target_or_untrusted_location_is_not_used(url: str) -> None:
    raw = page()
    raw["url"] = url
    result = classify_page(raw, QQ, VIEWER)
    assert result.status == BLOCKED
    assert "SECRET" not in repr(result)


def test_login_redirect_is_a_job_blocker_not_an_account_result() -> None:
    raw = page()
    raw.update(url="https://i.qq.com/?s_url=SECRET", viewers=[])
    result = classify_page(raw, QQ, VIEWER)
    assert result.status == BLOCKED
    assert result.reason == "login_required"
    assert "SECRET" not in repr(result)


@pytest.mark.parametrize(
    "viewers", [[], ["11111111"], [VIEWER, "11111111"], [VIEWER, VIEWER], True]
)
def test_missing_changed_or_ambiguous_viewer_stops(viewers: object) -> None:
    raw = page()
    raw["viewers"] = viewers
    assert classify_page(raw, QQ, VIEWER).status == BLOCKED


def test_user_text_is_not_a_top_level_system_notice() -> None:
    raw = page()
    raw.update(panels=[], body_text=NOTICE, normal_profile=True)
    assert classify_page(raw, QQ, VIEWER).status == UNCONFIRMED


@pytest.mark.parametrize(
    "change",
    [
        {"ready": "loading"},
        {"panels": []},
        {"panels": "SECRET"},
        {"panels": [{"paragraphs": ["温馨提示:", NOTICE], "report_icons": 0}]},
        {"panels": [{"paragraphs": ["温馨提示:", NOTICE], "report_icons": True}]},
        {"normal_profile": "true", "panels": []},
    ],
)
def test_incomplete_unknown_or_malformed_pages_pause(change: dict[str, object]) -> None:
    raw = page()
    raw.update(deepcopy(change))
    result = classify_page(raw, QQ, VIEWER)
    assert result.status == BLOCKED
    assert "SECRET" not in repr(result)


def test_main_route_is_supported_when_actual_target_matches() -> None:
    raw = page()
    raw.update(url=f"https://user.qzone.qq.com/{QQ}/main", panels=[], normal_profile=True)
    assert classify_page(raw, QQ, VIEWER).status == UNCONFIRMED


def test_failed_browser_close_can_be_retried_without_losing_handle(tmp_path):
    browser = Browser(tmp_path / "profile")
    calls = []

    def close():
        calls.append("close")
        if len(calls) == 1:
            raise RuntimeError("SECRET")

    context = SimpleNamespace(close=close)
    browser._context = context
    browser._runtime = SimpleNamespace(stop=lambda: calls.append("stop"))
    with pytest.raises(InspectionError) as raised:
        browser.close()
    assert "SECRET" not in str(raised.value)
    assert browser._context is context
    browser.close()
    assert calls == ["close", "close", "stop"]
    assert browser._context is None
