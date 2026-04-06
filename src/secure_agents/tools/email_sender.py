"""Email sender tool - sends emails via SMTP with optional attachments."""

from __future__ import annotations

import base64
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import structlog

from secure_agents.core.base_tool import BaseTool
from secure_agents.core.credentials import get_credential, get_oauth2_token
from secure_agents.core.registry import register_tool

logger = structlog.get_logger()


@register_tool("email_sender")
class EmailSenderTool(BaseTool):
    """Sends emails via SMTP with optional attachments.

    Security: TLS is always enforced. Plaintext SMTP connections are not
    supported. Set allow_insecure_connections: true to override (NOT RECOMMENDED).
    """

    name = "email_sender"
    description = "Send emails via SMTP with optional attachments"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self.host = self.config.get("host", "smtp.gmail.com")
        self.port = int(self.config.get("port", 587))
        self.username = self.config.get("username", "")
        self.auth_method = self.config.get("auth_method", "app_password")

        # TLS is always on unless explicitly overridden (NOT RECOMMENDED)
        self._allow_insecure = self.config.get("allow_insecure_connections", False)
        if self._allow_insecure:
            logger.warning("email_sender.INSECURE_CONNECTION",
                         msg="TLS is disabled. Credentials may be transmitted in plaintext.")

    def _authenticate(self, server: smtplib.SMTP) -> None:
        """Authenticate with the SMTP server using the configured method."""
        if self.auth_method == "oauth2":
            token = get_oauth2_token(self.username)
            if not token:
                raise RuntimeError(
                    f"No OAuth2 token for {self.username}. "
                    "Run: secure-agents auth gmail"
                )
            auth_string = f"user={self.username}\x01auth=Bearer {token}\x01\x01"
            server.ehlo()
            server.docmd("AUTH", "XOAUTH2 " + base64.b64encode(auth_string.encode()).decode())
        else:
            password = get_credential("email_password")
            if not password:
                raise RuntimeError(
                    "No email password found. Store it with:\n"
                    "  secure-agents auth setup\n"
                    "Or set the EMAIL_PASSWORD environment variable."
                )
            server.login(self.username, password)

    def execute(self, **kwargs: Any) -> dict:
        """Send an email.

        kwargs:
            to: Recipient email address (required)
            subject: Email subject (required)
            body: Email body text (required)
            html: Optional HTML body
            attachments: Optional list of file paths to attach

        Returns:
            {"sent": True/False, "error": optional error message}
        """
        to = kwargs.get("to", "")
        subject = kwargs.get("subject", "")
        body = kwargs.get("body", "")
        html = kwargs.get("html")
        attachments = kwargs.get("attachments", [])

        if not to or not subject:
            return {"sent": False, "error": "Missing 'to' or 'subject'"}

        # Basic email address validation
        if "@" not in to or "." not in to.split("@")[-1]:
            return {"sent": False, "error": f"Invalid email address: {to}"}

        try:
            msg = MIMEMultipart()
            msg["From"] = self.username
            msg["To"] = to
            msg["Subject"] = subject

            if html:
                msg.attach(MIMEText(body, "plain"))
                msg.attach(MIMEText(html, "html"))
            else:
                msg.attach(MIMEText(body, "plain"))

            for filepath in attachments:
                path = Path(filepath)
                if not path.exists():
                    continue
                with open(path, "rb") as f:
                    attachment = MIMEApplication(f.read(), Name=path.name)
                attachment["Content-Disposition"] = f'attachment; filename="{path.name}"'
                msg.attach(attachment)

            with smtplib.SMTP(self.host, self.port) as server:
                # Always use TLS unless explicitly overridden
                if not self._allow_insecure:
                    server.starttls()
                else:
                    logger.warning("email_sender.SENDING_WITHOUT_TLS")
                self._authenticate(server)
                server.send_message(msg)

            logger.info("email_sender.sent", to=to, subject=subject)
            return {"sent": True}

        except Exception as e:
            logger.error("email_sender.error", error=str(e))
            return {"sent": False, "error": str(e)}

    def validate_config(self) -> bool:
        if not self.username:
            return False
        if self.auth_method == "oauth2":
            return get_oauth2_token(self.username) is not None
        return get_credential("email_password") is not None
