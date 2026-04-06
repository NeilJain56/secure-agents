"""Email reader tool - monitors an IMAP inbox for new messages with attachments."""

from __future__ import annotations

import email
import tempfile
from datetime import datetime, timedelta
from email.header import decode_header
from pathlib import Path
from typing import Any

import structlog
from imapclient import IMAPClient

from secure_agents.core.base_tool import BaseTool
from secure_agents.core.credentials import get_credential, get_oauth2_token
from secure_agents.core.registry import register_tool

logger = structlog.get_logger()


@register_tool("email_reader")
class EmailReaderTool(BaseTool):
    """Monitors an IMAP mailbox and downloads attachments from new emails."""

    name = "email_reader"
    description = "Read emails via IMAP and download attachments"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self.host = self.config.get("host", "imap.gmail.com")
        self.port = int(self.config.get("port", 993))
        self.username = self.config.get("username", "")
        self.auth_method = self.config.get("auth_method", "app_password")
        self.use_ssl = self.config.get("use_ssl", True)
        self.download_dir = self.config.get("download_dir", tempfile.mkdtemp(prefix="secure_agents_"))

    def _authenticate(self, client: IMAPClient) -> None:
        """Authenticate with the IMAP server using the configured method."""
        if self.auth_method == "oauth2":
            token = get_oauth2_token(self.username)
            if not token:
                raise RuntimeError(
                    f"No OAuth2 token for {self.username}. "
                    "Run: secure-agents auth gmail"
                )
            auth_string = f"user={self.username}\x01auth=Bearer {token}\x01\x01"
            client.oauth2_login(self.username, token)
        else:
            # App password from keychain or env var
            password = get_credential("email_password")
            if not password:
                raise RuntimeError(
                    "No email password found. Store it with:\n"
                    "  secure-agents auth setup\n"
                    "Or set the EMAIL_PASSWORD environment variable."
                )
            client.login(self.username, password)

    def execute(self, **kwargs: Any) -> dict:
        """Poll inbox for unread emails with attachments.

        kwargs:
            folder: IMAP folder to check (default: INBOX)
            mark_read: Whether to mark fetched emails as read (default: True)
            since_days: Only look at emails from the last N days (default: 1)
            max_emails: Maximum number of emails to process per poll (default: 20)

        Returns:
            {"emails": [{"sender", "subject", "date", "attachments": [path, ...]}]}
        """
        folder = kwargs.get("folder", "INBOX")
        mark_read = kwargs.get("mark_read", True)
        since_days = int(kwargs.get("since_days", self.config.get("since_days", 1)))
        max_emails = int(kwargs.get("max_emails", self.config.get("max_emails", 20)))

        emails = []
        try:
            with IMAPClient(self.host, port=self.port, ssl=self.use_ssl) as client:
                self._authenticate(client)
                client.select_folder(folder)

                # Only search for unseen emails received within the last N days
                since_date = (datetime.now() - timedelta(days=since_days)).strftime("%d-%b-%Y")
                msg_ids = client.search(["UNSEEN", "SINCE", since_date])
                if not msg_ids:
                    return {"emails": []}

                logger.info("email_reader.found", count=len(msg_ids), processing=min(len(msg_ids), max_emails))

                # Process only the most recent N emails per poll
                for msg_id in msg_ids[-max_emails:]:
                    raw_messages = client.fetch([msg_id], ["RFC822"])
                    raw = raw_messages[msg_id][b"RFC822"]
                    msg = email.message_from_bytes(raw)

                    sender = msg.get("From", "")
                    subject = self._decode_header(msg.get("Subject", ""))
                    date = msg.get("Date", "")

                    attachments = self._extract_attachments(msg)

                    if attachments:
                        emails.append({
                            "sender": sender,
                            "subject": subject,
                            "date": date,
                            "message_id": str(msg_id),
                            "attachments": attachments,
                        })

                    if mark_read:
                        client.set_flags([msg_id], [b"\\Seen"])

        except Exception as e:
            logger.error("email_reader.error", error=str(e))
            return {"emails": [], "error": str(e)}

        logger.info("email_reader.done", emails_with_attachments=len(emails))
        return {"emails": emails}

    def _extract_attachments(self, msg: email.message.Message) -> list[str]:
        """Download attachments from an email message."""
        attachments = []
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            filename = part.get_filename()
            if filename is None:
                continue

            filename = self._decode_header(filename)
            safe_name = "".join(c for c in filename if c.isalnum() or c in ".-_ ")
            filepath = Path(self.download_dir) / safe_name

            payload = part.get_payload(decode=True)
            if payload:
                filepath.write_bytes(payload)
                attachments.append(str(filepath))
                logger.info("email_reader.attachment", filename=safe_name, size=len(payload))

        return attachments

    def _decode_header(self, header: str) -> str:
        """Decode an email header value."""
        decoded_parts = decode_header(header)
        result = []
        for part, charset in decoded_parts:
            if isinstance(part, bytes):
                result.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                result.append(part)
        return "".join(result)

    def validate_config(self) -> bool:
        if not self.username:
            return False
        if self.auth_method == "oauth2":
            return get_oauth2_token(self.username) is not None
        return get_credential("email_password") is not None
