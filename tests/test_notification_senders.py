"""All transports below are fakes; these tests never send QQ messages or email."""

import asyncio
import smtplib
import ssl
import threading

import pytest
from app.adapters.onebot.actions import OneBotActionError
from app.config import Settings
from app.notifications.config import NotificationSettings
from app.notifications.contracts import DeliveryMessage
from app.notifications.senders import EmailSender, QQSender

MESSAGE = DeliveryMessage(12, "系统提醒", "请进入后台确认。[CQ:at,qq=all]")


def notification_settings(**overrides):
    values = dict(
        enabled=True,
        qq_enabled=True,
        qq_group_id="10001",
        email_enabled=True,
        smtp_host="smtp.example.test",
        smtp_from="sender@example.test",
        email_to="primary@example.test",
        email_backup_to="backup@example.test",
        smtp_username="fixture-user",
        smtp_password="fixture-secret",
    )
    values.update(overrides)
    return NotificationSettings(_env_file=None, **values)


def runtime_settings(**overrides):
    values = dict(
        onebot_ws_enabled=True,
        onebot_access_token="fixture-token",
        onebot_self_id="10002",
        action_mode="SHADOW",
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


class FakeCaller:
    def __init__(self, response=None, error=None):
        self.response = response if response is not None else {"status": "ok", "retcode": 0}
        self.error = error
        self.calls = []

    async def __call__(self, action, params):
        self.calls.append((action, params))
        if self.error:
            raise self.error
        return self.response


async def test_default_disabled_channels_make_zero_calls():
    caller = FakeCaller()
    config = NotificationSettings(_env_file=None)
    assert (
        await QQSender(config, runtime_settings(), caller=caller).send(MESSAGE)
    ).status == "SKIPPED"
    assert (
        await EmailSender(config, smtp_factory=lambda **_: pytest.fail("external call")).send(
            MESSAGE
        )
    ).status == "SKIPPED"
    assert caller.calls == []


async def test_qq_explicit_authorization_only_sends_fixed_group_plain_text_in_shadow():
    caller = FakeCaller()
    result = await QQSender(notification_settings(), runtime_settings(), caller=caller).send(
        MESSAGE
    )
    assert result.status == "SENT"
    assert caller.calls == [
        (
            "send_group_msg",
            {
                "group_id": 10001,
                "message": [
                    {
                        "type": "text",
                        "data": {"text": "[通知 #12] 系统提醒\n请进入后台确认。[CQ:at,qq=all]"},
                    }
                ],
            },
        )
    ]


@pytest.mark.parametrize(
    "runtime",
    [
        {"onebot_ws_enabled": False},
        {"onebot_access_token": ""},
        {"onebot_self_id": ""},
        {"onebot_self_id": "invalid"},
    ],
)
async def test_qq_incomplete_runtime_refuses_before_sending(runtime):
    caller = FakeCaller()
    settings = runtime_settings().model_copy(update=runtime)
    result = await QQSender(notification_settings(), settings, caller=caller).send(MESSAGE)
    assert result.status == "FAILED" and not result.retryable
    assert caller.calls == []


@pytest.mark.parametrize(
    "response,status",
    [
        ({"status": "ok", "retcode": 0}, "SENT"),
        ({"status": "failed", "retcode": 1400}, "FAILED"),
        ({"status": "async", "retcode": 1}, "UNKNOWN"),
        ({"status": "failed", "retcode": 0}, "UNKNOWN"),
        ({"status": "ok", "retcode": True}, "UNKNOWN"),
        ({"status": "ok", "retcode": "0"}, "UNKNOWN"),
        ({"status": "ok"}, "UNKNOWN"),
        ({}, "UNKNOWN"),
    ],
)
async def test_qq_response_certainty(response, status):
    result = await QQSender(
        notification_settings(), runtime_settings(), caller=FakeCaller(response)
    ).send(MESSAGE)
    assert result.status == status
    assert not result.retryable


@pytest.mark.parametrize(
    "kind,status,retryable",
    [
        ("disconnected", "FAILED", True),
        ("not_ready", "FAILED", True),
        ("timeout", "UNKNOWN", False),
        ("response_lost", "UNKNOWN", False),
    ],
)
async def test_qq_transport_failure_certainty(kind, status, retryable, caplog):
    result = await QQSender(
        notification_settings(),
        runtime_settings(),
        caller=FakeCaller(error=OneBotActionError(kind, "fixture-secret")),
    ).send(MESSAGE)
    assert (result.status, result.retryable) == (status, retryable)
    assert "fixture-secret" not in repr(result) + caplog.text


async def test_qq_outer_timeout_is_unknown_and_does_not_repeat():
    async def hang(*_args):
        await asyncio.Event().wait()

    config = notification_settings().model_copy(update={"timeout_seconds": 0.02})
    result = await QQSender(config, runtime_settings(), caller=hang).send(MESSAGE)
    assert result.status == "UNKNOWN" and not result.retryable


class FakeSMTP:
    def __init__(self, *, error_stage="", error=None, refused=None, gate=None):
        self.events = []
        self.error_stage, self.error, self.refused, self.gate = error_stage, error, refused, gate
        self.kwargs = None

    def factory(self, **kwargs):
        self.kwargs = kwargs
        self._event("connect")
        return self

    def _event(self, name):
        self.events.append(name)
        if name == self.error_stage:
            raise self.error

    def ehlo(self):
        self._event("ehlo")
        return 250, b"OK"

    def starttls(self, *, context):
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        self._event("starttls")

    def login(self, username, password):
        assert (username, password) == ("fixture-user", "fixture-secret")
        self._event("login")

    def send_message(self, message, *, from_addr, to_addrs):
        self._event("send")
        self.message, self.from_addr, self.to_addrs = message, from_addr, to_addrs
        if self.gate:
            self.gate.wait(1)
        return self.refused or {}

    def close(self):
        self._event("close")


@pytest.mark.parametrize("security", ["starttls", "ssl"])
@pytest.mark.parametrize(
    "audience,recipient", [("primary", "primary@example.test"), ("backup", "backup@example.test")]
)
async def test_smtp_uses_verified_tls_and_fixed_envelope(security, audience, recipient):
    smtp = FakeSMTP()
    config = notification_settings(smtp_security=security)
    result = await EmailSender(
        config, smtp_factory=smtp.factory, smtp_ssl_factory=smtp.factory
    ).send(MESSAGE, audience)
    assert result.status == "SENT"
    assert smtp.to_addrs == (recipient,) and smtp.from_addr == "sender@example.test"
    assert smtp.kwargs["timeout"] == 8
    assert smtp.events[-1] == "close"
    if security == "starttls":
        assert (
            smtp.events.index("starttls") < smtp.events.index("login") < smtp.events.index("send")
        )
        assert smtp.events.count("ehlo") == 2
    else:
        assert smtp.kwargs["context"].check_hostname
        assert "starttls" not in smtp.events


@pytest.mark.parametrize(
    "stage,error,status,retryable",
    [
        ("connect", ConnectionRefusedError("fixture-secret"), "FAILED", True),
        ("starttls", ssl.SSLCertVerificationError("fixture-secret"), "FAILED", False),
        ("login", smtplib.SMTPAuthenticationError(535, b"fixture-secret"), "FAILED", False),
        ("send", smtplib.SMTPServerDisconnected("fixture-secret"), "UNKNOWN", False),
        ("send", TimeoutError("fixture-secret"), "UNKNOWN", False),
        ("send", ssl.SSLEOFError("fixture-secret"), "UNKNOWN", False),
        ("send", smtplib.SMTPDataError(450, b"fixture-secret"), "FAILED", True),
        (
            "send",
            smtplib.SMTPRecipientsRefused({"primary@example.test": (550, b"fixture-secret")}),
            "FAILED",
            False,
        ),
    ],
)
async def test_smtp_errors_do_not_leak_or_blindly_retry(stage, error, status, retryable, caplog):
    smtp = FakeSMTP(error_stage=stage, error=error)
    result = await EmailSender(notification_settings(), smtp_factory=smtp.factory).send(MESSAGE)
    assert (result.status, result.retryable) == (status, retryable)
    assert "fixture-secret" not in repr(result) + caplog.text


async def test_partial_smtp_acceptance_is_not_retried():
    smtp = FakeSMTP(refused={"backup@example.test": (450, b"refused")})
    config = notification_settings(email_to="primary@example.test,backup@example.test")
    result = await EmailSender(config, smtp_factory=smtp.factory).send(MESSAGE)
    assert result.status == "UNKNOWN" and not result.retryable
    assert result.error_code == "smtp_partial_acceptance"


async def test_smtp_timeout_is_unknown_and_at_most_one_thread_in_flight():
    gate = threading.Event()
    smtp = FakeSMTP(gate=gate)
    config = notification_settings().model_copy(update={"timeout_seconds": 0.02})
    sender = EmailSender(config, smtp_factory=smtp.factory)
    try:
        assert (await sender.send(MESSAGE)).status == "UNKNOWN"
        second = await sender.send(MESSAGE)
        assert second.status == "FAILED" and second.retryable
        assert smtp.events.count("send") == 1
    finally:
        gate.set()
        await asyncio.sleep(0.03)


@pytest.mark.parametrize("audience", ["other", "attacker@example.test"])
async def test_sender_never_accepts_an_arbitrary_destination(audience):
    smtp, caller = FakeSMTP(), FakeCaller()
    assert (
        await EmailSender(notification_settings(), smtp_factory=smtp.factory).send(
            MESSAGE, audience
        )
    ).status == "FAILED"
    assert (
        await QQSender(notification_settings(), runtime_settings(), caller=caller).send(
            MESSAGE, audience
        )
    ).status == "FAILED"
    assert not smtp.events and not caller.calls


async def test_email_header_injection_refused_before_connect():
    smtp = FakeSMTP()
    result = await EmailSender(notification_settings(), smtp_factory=smtp.factory).send(
        DeliveryMessage(1, "title\r\nBcc: attacker@example.test", "safe")
    )
    assert result.status == "FAILED" and not smtp.events


async def test_cancelled_smtp_call_keeps_background_thread_tracked():
    gate = threading.Event()
    smtp = FakeSMTP(gate=gate)
    sender = EmailSender(notification_settings(), smtp_factory=smtp.factory)
    task = asyncio.create_task(sender.send(MESSAGE))
    try:
        for _ in range(100):
            if "send" in smtp.events:
                break
            await asyncio.sleep(0.001)
        assert "send" in smtp.events
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        result = await sender.send(MESSAGE)
        assert (result.status, result.error_code, result.retryable) == ("FAILED", "smtp_busy", True)
        assert smtp.events.count("send") == 1
    finally:
        gate.set()
        await asyncio.sleep(0.03)
