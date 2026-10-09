"""
Notification channels for AgentDB governance events.

Two built-in channels:
  EmailChannel  — sends via SMTP (stdlib only; no new dependencies).
  SlackChannel  — sends via Slack Web API (requires slack-sdk extra).

Both implement NotificationChannel.send(subject, body).
"""
from __future__ import annotations

import smtplib
import ssl
from abc import ABC, abstractmethod
from email.message import EmailMessage
from typing import Optional


class NotificationChannel(ABC):
    """Minimal interface every channel must implement."""

    @abstractmethod
    def send(self, subject: str, body: str) -> None:  # pragma: no cover
        """Send a notification.  Raise on unrecoverable failure."""


class EmailChannel(NotificationChannel):
    """
    Send notifications via SMTP.

    Minimum required config: ``to_address``.  Everything else has a
    default that works against a local relay on port 25.  For TLS relay
    (e.g. SendGrid, SES SMTP) supply host/port/user/password.

    Args:
        to_address: Destination email address.
        from_address: Envelope-from (default: agentdb@localhost).
        smtp_host: SMTP server hostname (default: localhost).
        smtp_port: SMTP server port (default: 25).
        smtp_user: Login username; omit for unauthenticated relays.
        smtp_password: Login password; omit for unauthenticated relays.
        use_tls: If True, use SMTP_SSL instead of plain SMTP (default: False).
    """

    def __init__(
        self,
        to_address: str,
        from_address: str = "agentdb@localhost",
        smtp_host: str = "localhost",
        smtp_port: int = 25,
        smtp_user: Optional[str] = None,
        smtp_password: Optional[str] = None,
        use_tls: bool = False,
    ) -> None:
        self.to_address = to_address
        self.from_address = from_address
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.use_tls = use_tls

    def send(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.from_address
        msg["To"] = self.to_address
        msg.set_content(body)

        ctx = ssl.create_default_context() if self.use_tls else None
        smtp_cls = smtplib.SMTP_SSL if self.use_tls else smtplib.SMTP
        with smtp_cls(self.smtp_host, self.smtp_port, context=ctx) as server:  # type: ignore[call-arg]
            if self.smtp_user and self.smtp_password:
                server.login(self.smtp_user, self.smtp_password)
            server.send_message(msg)


class SlackChannel(NotificationChannel):
    """
    Send notifications to a Slack channel or user via the Slack Web API.

    Requires the ``notifications`` optional extra::

        pip install agentdatabase[notifications]

    Args:
        bot_token: Slack bot OAuth token (``xoxb-...``).
        channel: Channel ID, channel name (``#ops-alerts``), or user ID (``U…``).
    """

    def __init__(self, bot_token: str, channel: str) -> None:
        try:
            from slack_sdk import WebClient  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "SlackChannel requires slack-sdk: "
                "pip install agentdatabase[notifications]"
            ) from exc
        self._client = WebClient(token=bot_token)
        self.channel = channel

    def send(self, subject: str, body: str) -> None:
        text = f"*{subject}*\n{body}"
        self._client.chat_postMessage(channel=self.channel, text=text)
