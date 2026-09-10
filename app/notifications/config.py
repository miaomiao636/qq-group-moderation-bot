"""Local-owner notification opt-ins; no addresses or secrets in representations."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import PROJECT_ROOT

_MAILBOX = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}"
)
_HEARTBEAT_URL = re.compile(
    r"https://hc-ping\.com/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


class NotificationSettings(BaseSettings):
    # https://docs.pydantic.dev/latest/concepts/pydantic_settings/
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
        hide_input_in_errors=True,
    )
    enabled: bool = Field(False, alias="NOTIFICATIONS_ENABLED")
    qq_enabled: bool = Field(False, alias="NOTIFICATION_QQ_ENABLED")
    qq_group_id: str = Field("", alias="NOTIFICATION_QQ_GROUP_ID", repr=False)
    email_enabled: bool = Field(False, alias="NOTIFICATION_EMAIL_ENABLED")
    smtp_host: str = Field("", alias="SMTP_HOST", repr=False)
    smtp_port: int = Field(587, ge=1, le=65535, alias="SMTP_PORT")
    smtp_username: str = Field("", alias="SMTP_USERNAME", repr=False)
    smtp_password: str = Field("", alias="SMTP_PASSWORD", repr=False)
    smtp_from: str = Field("", alias="SMTP_FROM", repr=False)
    email_to: str = Field("", alias="NOTIFICATION_EMAIL_TO", repr=False)
    email_backup_to: str = Field("", alias="NOTIFICATION_EMAIL_BACKUP_TO", repr=False)
    smtp_security: Literal["starttls", "ssl"] = Field("starttls", alias="SMTP_SECURITY")
    timeout_seconds: float = Field(8, ge=1, le=20, alias="NOTIFICATION_TIMEOUT_SECONDS")
    poll_seconds: int = Field(30, ge=10, le=300, alias="NOTIFICATION_POLL_SECONDS")
    summary_seconds: int = Field(900, ge=60, le=86400, alias="NOTIFICATION_SUMMARY_SECONDS")
    escalation_seconds: int = Field(900, ge=60, le=86400, alias="NOTIFICATION_ESCALATION_SECONDS")
    heartbeat_enabled: bool = Field(False, alias="NOTIFICATION_HEARTBEAT_ENABLED")
    heartbeat_url: str = Field("", alias="NOTIFICATION_HEARTBEAT_URL", repr=False)
    heartbeat_probe_token: str = Field("", alias="NOTIFICATION_HEARTBEAT_PROBE_TOKEN", repr=False)
    heartbeat_local_port: int = Field(
        8000, ge=1, le=65535, alias="NOTIFICATION_HEARTBEAT_LOCAL_PORT"
    )

    @field_validator("qq_group_id")
    @classmethod
    def _group_id(cls, value: str) -> str:
        if value and (not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) > 2**63 - 1):
            raise ValueError("notification group must be a positive numeric ID")
        return value

    @field_validator("smtp_host")
    @classmethod
    def _host(cls, value: str) -> str:
        if value and (
            len(value) > 253
            or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value)
        ):
            raise ValueError("SMTP host must be a bare hostname or IPv4 address")
        return value

    @field_validator("smtp_from", "email_to", "email_backup_to")
    @classmethod
    def _addresses(cls, value: str) -> str:
        if not value:
            return value
        if "\r" in value or "\n" in value:
            raise ValueError("notification mailbox contains forbidden characters")
        addresses = [address.strip() for address in value.split(",")]
        if len(addresses) > 10 or any(
            len(address) > 254 or not _MAILBOX.fullmatch(address) for address in addresses
        ):
            raise ValueError("expected at most ten bare ASCII mailboxes")
        return ",".join(dict.fromkeys(addresses))

    @field_validator("heartbeat_url")
    @classmethod
    def _heartbeat(cls, value: str) -> str:
        if value and not _HEARTBEAT_URL.fullmatch(value):
            raise ValueError("heartbeat URL must be the fixed HTTPS Healthchecks UUID endpoint")
        return value

    @field_validator("heartbeat_probe_token")
    @classmethod
    def _probe_token(cls, value: str) -> str:
        if value and (len(value) > 256 or any(not 33 <= ord(char) <= 126 for char in value)):
            raise ValueError("invalid heartbeat probe token")
        return value

    @model_validator(mode="after")
    def _complete_sinks(self) -> NotificationSettings:
        if bool(self.smtp_username) != bool(self.smtp_password):
            raise ValueError("SMTP username and password must be configured together")
        if "," in self.smtp_from:
            raise ValueError("SMTP_FROM must contain exactly one mailbox")
        if not self.enabled:
            return self
        if self.qq_enabled and not self.qq_group_id:
            raise ValueError("QQ notifications require a fixed management group")
        if self.email_enabled and not all((self.smtp_host, self.smtp_from, self.email_to)):
            raise ValueError("email notifications require SMTP host, sender and primary recipients")
        if self.heartbeat_enabled and not all((self.heartbeat_url, self.heartbeat_probe_token)):
            raise ValueError("heartbeat requires a URL and dedicated probe token")
        if self.heartbeat_enabled and len(self.heartbeat_probe_token) < 32:
            raise ValueError("heartbeat probe token must contain at least 32 random characters")
        return self
