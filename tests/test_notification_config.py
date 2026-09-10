"""Notification credentials and destinations are local, explicit and fail closed."""

import pytest
from app.notifications.config import NotificationSettings


def test_safe_notification_defaults() -> None:
    settings = NotificationSettings(_env_file=None)
    assert not any(
        (settings.enabled, settings.qq_enabled, settings.email_enabled, settings.heartbeat_enabled)
    )
    assert settings.timeout_seconds == 8
    assert (settings.poll_seconds, settings.summary_seconds, settings.escalation_seconds) == (
        30,
        900,
        900,
    )
    assert settings.smtp_security == "starttls"
    assert settings.heartbeat_local_port == 8000


@pytest.mark.parametrize(
    "options",
    [
        {"SMTP_SECURITY": "none"},
        {"SMTP_SECURITY": "plain"},
        {"NOTIFICATION_TIMEOUT_SECONDS": 0},
        {"NOTIFICATION_TIMEOUT_SECONDS": 21},
        {"NOTIFICATION_POLL_SECONDS": 9},
        {"NOTIFICATION_POLL_SECONDS": 301},
        {"NOTIFICATION_SUMMARY_SECONDS": 59},
        {"NOTIFICATION_SUMMARY_SECONDS": 86401},
        {"NOTIFICATION_ESCALATION_SECONDS": 59},
        {"NOTIFICATION_ESCALATION_SECONDS": 86401},
        {"SMTP_PORT": 0},
        {"SMTP_PORT": 65536},
        {"NOTIFICATION_HEARTBEAT_LOCAL_PORT": 0},
        {"NOTIFICATION_QQ_GROUP_ID": "-2"},
        {"NOTIFICATION_QQ_GROUP_ID": "１２"},
        {"NOTIFICATION_QQ_GROUP_ID": "9223372036854775808"},
        {"SMTP_HOST": "smtp.example.test:25"},
        {"SMTP_HOST": "smtp.example.test\r\ninjected"},
        {"SMTP_FROM": "Display <sender@example.test>"},
        {"NOTIFICATION_EMAIL_TO": "one@example.test\r\nBcc: two@example.test"},
        {"NOTIFICATION_EMAIL_TO": "a@example.test,"},
        {"NOTIFICATION_EMAIL_BACKUP_TO": ","},
        {"NOTIFICATION_EMAIL_TO": ",".join(f"user{i}@example.test" for i in range(11))},
    ],
)
def test_notification_configuration_rejects_unsafe_values(options) -> None:
    with pytest.raises(ValueError):
        NotificationSettings(_env_file=None, **options)


@pytest.mark.parametrize(
    "url",
    [
        "http://hc-ping.com/11111111-1111-4111-8111-111111111111",
        "https://example.test/11111111-1111-4111-8111-111111111111",
        "https://hc-ping.com.evil.test/11111111-1111-4111-8111-111111111111",
        "https://hc-ping.com/not-a-uuid",
        "https://hc-ping.com/11111111-1111-4111-8111-111111111111/fail",
        "https://hc-ping.com/11111111-1111-4111-8111-111111111111?create=1",
        "https://hc-ping.com/11111111-1111-4111-8111-111111111111#fragment",
        "https://user:password@hc-ping.com/11111111-1111-4111-8111-111111111111",
        "https://hc-ping.com:444/11111111-1111-4111-8111-111111111111",
    ],
)
def test_heartbeat_accepts_only_fixed_https_uuid_endpoint(url) -> None:
    with pytest.raises(ValueError) as failure:
        NotificationSettings(_env_file=None, NOTIFICATION_HEARTBEAT_URL=url)
    assert url not in str(failure.value)


@pytest.mark.parametrize(
    "options",
    [
        {"NOTIFICATION_QQ_ENABLED": True},
        {"NOTIFICATION_EMAIL_ENABLED": True},
        {"NOTIFICATION_HEARTBEAT_ENABLED": True},
        {
            "NOTIFICATION_HEARTBEAT_ENABLED": True,
            "NOTIFICATION_HEARTBEAT_URL": "https://hc-ping.com/11111111-1111-4111-8111-111111111111",
        },
        {"SMTP_USERNAME": "username-only"},
        {"SMTP_PASSWORD": "password-only"},
        {
            "NOTIFICATION_HEARTBEAT_ENABLED": True,
            "NOTIFICATION_HEARTBEAT_URL": "https://hc-ping.com/11111111-1111-4111-8111-111111111111",
            "NOTIFICATION_HEARTBEAT_PROBE_TOKEN": "short",
        },
        {"NOTIFICATION_HEARTBEAT_PROBE_TOKEN": "x" * 32 + "\x00"},
    ],
)
def test_enabled_sink_requires_complete_configuration(options) -> None:
    with pytest.raises(ValueError):
        NotificationSettings(_env_file=None, NOTIFICATIONS_ENABLED=True, **options)


def test_mailboxes_are_normalized_and_secrets_hidden() -> None:
    settings = NotificationSettings(
        _env_file=None,
        SMTP_USERNAME="fixture-user",
        SMTP_PASSWORD="fixture-secret",
        NOTIFICATION_EMAIL_TO=" first@example.test, second@example.test ",
        NOTIFICATION_HEARTBEAT_URL="https://hc-ping.com/11111111-1111-4111-8111-111111111111",
        NOTIFICATION_HEARTBEAT_PROBE_TOKEN="fixture-probe-secret",
    )
    assert settings.email_to == "first@example.test,second@example.test"
    for secret in (
        "fixture-secret",
        "fixture-user",
        "fixture-probe-secret",
        "11111111-1111",
        "first@example.test",
    ):
        assert secret not in repr(settings)
