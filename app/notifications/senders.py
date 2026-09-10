"""Fixed-target notifications, independently authorized from punishment actions.

QQ: https://github.com/botuniverse/onebot-11/blob/master/api/public.md#send_group_msg-发送群消息
SMTP: https://docs.python.org/3.12/library/smtplib.html
SENT means channel acceptance, never a human acknowledgement.
"""

from __future__ import annotations

import asyncio
import re
import smtplib
import ssl
from collections.abc import Callable, Mapping
from contextlib import suppress

from app.adapters.onebot.actions import OneBotActionCaller, OneBotActionError
from app.config import Settings
from app.notifications.config import NotificationSettings
from app.notifications.contracts import DeliveryMessage, DeliveryResult

SMTPFactory = Callable[..., smtplib.SMTP]


def _valid_message(message: DeliveryMessage) -> bool:
    return (
        type(message.notice_id) is int
        and message.notice_id > 0
        and bool(message.subject.strip())
        and len(message.subject) <= 160
        and not any(char in message.subject for char in "\r\n\x00")
        and len(message.body) <= 4000
        and "\x00" not in message.body
    )


class QQSender:
    """Only a plain-text message to the explicitly configured management group."""

    def __init__(
        self,
        settings: NotificationSettings,
        runtime_settings: Settings,
        *,
        caller: OneBotActionCaller | None = None,
    ) -> None:
        self._settings, self._runtime = settings, runtime_settings
        if caller is None:
            from app.runtime.onebot_actions import onebot_action_hub

            caller = onebot_action_hub.call
        self._caller = caller

    async def send(self, message: DeliveryMessage, audience: str = "primary") -> DeliveryResult:
        if not self._settings.enabled or not self._settings.qq_enabled:
            return DeliveryResult("SKIPPED", "qq_disabled")
        if audience not in ("primary", "backup"):
            return DeliveryResult("FAILED", "invalid_audience")
        if audience == "backup":
            return DeliveryResult("SKIPPED", "qq_backup_not_configured")
        if not _valid_message(message):
            return DeliveryResult("FAILED", "invalid_message")
        if not all(
            (
                self._runtime.onebot_ws_enabled,
                self._runtime.onebot_access_token.strip(),
                self._settings.qq_group_id,
            )
        ) or not re.fullmatch(r"[1-9][0-9]{0,18}", self._runtime.onebot_self_id):
            return DeliveryResult("FAILED", "qq_configuration_invalid")
        try:
            async with asyncio.timeout(self._settings.timeout_seconds):
                response = await self._caller(
                    "send_group_msg",
                    {
                        "group_id": int(self._settings.qq_group_id),
                        "message": [
                            {
                                "type": "text",
                                "data": {
                                    "text": f"[通知 #{message.notice_id}] {message.subject}\n{message.body}"
                                },
                            }
                        ],
                    },
                )
        except OneBotActionError as exc:
            if exc.kind in ("not_ready", "disconnected"):
                return DeliveryResult("FAILED", "qq_not_ready", retryable=True)
            return DeliveryResult("UNKNOWN", "qq_transport_uncertain")
        except Exception:  # Boundary returns only a fixed code, never response or exception text.
            return DeliveryResult("UNKNOWN", "qq_transport_uncertain")
        if not isinstance(response, Mapping) or type(response.get("retcode")) is not int:
            return DeliveryResult("UNKNOWN", "qq_response_uncertain")
        if response.get("status") == "ok" and response["retcode"] == 0:
            return DeliveryResult("SENT")
        if response.get("status") == "failed" and response["retcode"] not in (0, 1):
            return DeliveryResult("FAILED", "qq_rejected")
        return DeliveryResult("UNKNOWN", "qq_response_uncertain")


class EmailSender:
    """Verified TLS SMTP with one bounded in-flight thread per long-lived sender.

    The async deadline cannot kill a stdlib socket thread. An expired call is UNKNOWN,
    and this sender refuses to spawn another thread until the first actually exits.
    The dispatcher must persist UNKNOWN and must never replay that delivery.
    """

    def __init__(
        self,
        settings: NotificationSettings,
        *,
        smtp_factory: SMTPFactory | None = None,
        smtp_ssl_factory: SMTPFactory | None = None,
    ) -> None:
        self._settings = settings
        self._smtp_factory = smtp_factory or smtplib.SMTP
        self._smtp_ssl_factory = smtp_ssl_factory or smtplib.SMTP_SSL
        self._inflight: asyncio.Task[DeliveryResult] | None = None

    async def send(self, message: DeliveryMessage, audience: str = "primary") -> DeliveryResult:
        if not self._settings.enabled or not self._settings.email_enabled:
            return DeliveryResult("SKIPPED", "email_disabled")
        if audience not in ("primary", "backup"):
            return DeliveryResult("FAILED", "invalid_audience")
        addresses = (
            self._settings.email_to if audience == "primary" else self._settings.email_backup_to
        )
        if not addresses:
            return DeliveryResult("SKIPPED", "email_audience_not_configured")
        if not _valid_message(message):
            return DeliveryResult("FAILED", "invalid_message")
        if self._inflight is not None and not self._inflight.done():
            return DeliveryResult("FAILED", "smtp_busy", retryable=True)
        self._inflight = asyncio.create_task(
            asyncio.to_thread(self._send, message, tuple(addresses.split(",")))
        )
        try:
            return await asyncio.wait_for(
                asyncio.shield(self._inflight), self._settings.timeout_seconds
            )
        except TimeoutError:
            return DeliveryResult("UNKNOWN", "smtp_timeout")
        # CancelledError propagates; shield keeps the thread tracked and the
        # dispatcher's interrupted SENDING claim must become UNKNOWN, never retry.

    def _send(self, message: DeliveryMessage, recipients: tuple[str, ...]) -> DeliveryResult:
        from email.message import EmailMessage

        client: smtplib.SMTP | None = None
        sending = False
        try:
            settings = self._settings
            context = ssl.create_default_context()
            if settings.smtp_security == "ssl":
                client = self._smtp_ssl_factory(
                    host=settings.smtp_host,
                    port=settings.smtp_port,
                    timeout=settings.timeout_seconds,
                    context=context,
                )
            else:
                client = self._smtp_factory(
                    host=settings.smtp_host,
                    port=settings.smtp_port,
                    timeout=settings.timeout_seconds,
                )
            client.ehlo()
            if settings.smtp_security == "starttls":
                client.starttls(context=context)
                client.ehlo()
            if settings.smtp_username:
                client.login(settings.smtp_username, settings.smtp_password)
            mail = EmailMessage()
            mail["Subject"] = f"[通知 #{message.notice_id}] {message.subject}"
            mail["From"] = settings.smtp_from
            mail["To"] = ", ".join(recipients)
            mail.set_content(message.body)
            sending = True
            refused = client.send_message(mail, from_addr=settings.smtp_from, to_addrs=recipients)
            if refused:
                # Some recipients already accepted DATA; replaying the whole list duplicates mail.
                return DeliveryResult("UNKNOWN", "smtp_partial_acceptance")
            return DeliveryResult("SENT")
        except ssl.SSLError:
            return (
                DeliveryResult("UNKNOWN", "smtp_delivery_uncertain")
                if sending
                else DeliveryResult("FAILED", "smtp_security_or_auth_failed")
            )
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPNotSupportedError):
            return DeliveryResult("FAILED", "smtp_security_or_auth_failed")
        except smtplib.SMTPRecipientsRefused as exc:
            retryable = bool(exc.recipients) and all(
                400 <= item[0] < 500 for item in exc.recipients.values()
            )
            return DeliveryResult("FAILED", "smtp_recipients_rejected", retryable=retryable)
        except (
            smtplib.SMTPSenderRefused,
            smtplib.SMTPDataError,
            smtplib.SMTPHeloError,
            smtplib.SMTPConnectError,
        ) as exc:
            return DeliveryResult("FAILED", "smtp_rejected", retryable=400 <= exc.smtp_code < 500)
        except (OSError, smtplib.SMTPException):
            return (
                DeliveryResult("UNKNOWN", "smtp_delivery_uncertain")
                if sending
                else DeliveryResult("FAILED", "smtp_connect_failed", retryable=True)
            )
        except Exception:
            return (
                DeliveryResult("UNKNOWN", "smtp_delivery_uncertain")
                if sending
                else DeliveryResult("FAILED", "smtp_pre_send_failed")
            )
        finally:
            if client is not None:
                # DATA's result is authoritative. QUIT failure must not turn accepted mail into a retry.
                with suppress(Exception):
                    client.close()
