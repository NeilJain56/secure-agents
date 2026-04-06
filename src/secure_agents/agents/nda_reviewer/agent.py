"""NDA Reviewer agent - monitors email for NDAs and analyzes them."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

import structlog

from secure_agents.core.base_agent import BaseAgent
from secure_agents.core.base_provider import Message
from secure_agents.core.registry import register_agent
from secure_agents.core.security import sanitize_text, AuditLog, cleanup_temp_files
from secure_agents.agents.nda_reviewer.prompts import SYSTEM_PROMPT, REVIEW_PROMPT_TEMPLATE

logger = structlog.get_logger()

# Heuristics for NDA detection
NDA_KEYWORDS = ["nda", "non-disclosure", "nondisclosure", "confidentiality agreement", "confidential"]
NDA_FILENAME_PATTERNS = [re.compile(kw, re.IGNORECASE) for kw in NDA_KEYWORDS]


def _is_nda_candidate(filename: str, text: str = "") -> bool:
    """Check if a file is likely an NDA based on filename and content."""
    name_lower = filename.lower()
    for pattern in NDA_FILENAME_PATTERNS:
        if pattern.search(name_lower):
            return True
    if text:
        text_lower = text[:2000].lower()
        matches = sum(1 for kw in NDA_KEYWORDS if kw in text_lower)
        return matches >= 2
    return False


@register_agent("nda_reviewer")
class NDAReviewerAgent(BaseAgent):
    """Monitors email inbox, detects NDA documents, analyzes them, and reports findings."""

    name = "nda_reviewer"
    description = "Automated NDA review via email monitoring"
    version = "0.2.0"
    features = [
        "Monitors inbox for incoming NDA documents",
        "Detects NDAs by filename and content heuristics",
        "Extracts text from PDF and DOCX attachments",
        "AI-powered clause-by-clause risk analysis",
        "Generates structured risk reports with scores",
        "Emails findings back to the original sender",
    ]

    def __init__(self, tools, provider, config=None):
        super().__init__(tools, provider, config)
        self.poll_interval = self.config.get("poll_interval_seconds", 60)
        security = self.config.get("security", {})
        self.audit = AuditLog(security.get("audit_log_path", "./logs/audit.log"))

    def tick(self) -> None:
        """One iteration: check email, process any NDAs found."""
        email_reader = self.get_tool("email_reader")
        result = email_reader.execute()
        emails = result.get("emails", [])

        if not emails:
            logger.debug("nda_reviewer.no_new_emails")
        else:
            for email_data in emails:
                self._process_email(email_data)

        # Wait for poll interval, but wake up immediately on stop signal
        self._stop_event.wait(self.poll_interval)

    def _process_email(self, email_data: dict) -> None:
        """Process a single email: check attachments for NDAs and analyze."""
        sender = email_data.get("sender", "unknown")
        subject = email_data.get("subject", "")
        attachments = email_data.get("attachments", [])

        self.audit.log(
            "email_received",
            sender=sender,
            subject=subject,
            attachment_count=len(attachments),
        )

        doc_parser = self.get_tool("document_parser")

        for filepath in attachments:
            # Parse the document
            parse_result = doc_parser.execute(file_path=filepath)
            if "error" in parse_result:
                logger.warning("nda_reviewer.parse_failed", file=filepath, error=parse_result["error"])
                continue

            text = parse_result.get("text", "")
            filename = parse_result.get("metadata", {}).get("filename", filepath)

            # Check if it's an NDA
            if not _is_nda_candidate(filename, text):
                logger.info("nda_reviewer.not_nda", filename=filename)
                continue

            logger.info("nda_reviewer.nda_detected", filename=filename)
            self.audit.log("nda_detected", filename=filename, sender=sender)

            # Analyze the NDA
            review = self._analyze_nda(text, filename)
            if review is None:
                continue

            # Save the report
            self._save_report(review, filename, sender)

            # Send findings back via email
            self._send_findings(review, filename, sender, subject)

    def _analyze_nda(self, text: str, filename: str) -> dict | None:
        """Run LLM analysis on NDA text."""
        sanitized_text = sanitize_text(text)

        messages = [
            Message(role="system", content=SYSTEM_PROMPT),
            Message(role="user", content=REVIEW_PROMPT_TEMPLATE.format(document_text=sanitized_text)),
        ]

        try:
            response = self.provider.complete(messages, json_mode=True)
            review = json.loads(response.content)
            self.audit.log("nda_analyzed", filename=filename, risk_score=review.get("risk_score"))
            return review
        except json.JSONDecodeError:
            logger.error("nda_reviewer.bad_json", filename=filename)
            self.audit.log("nda_analysis_failed", filename=filename, reason="invalid_json")
            return None
        except Exception as e:
            logger.error("nda_reviewer.analysis_error", filename=filename, error=str(e))
            self.audit.log("nda_analysis_failed", filename=filename, reason=str(e))
            return None

    def _save_report(self, review: dict, filename: str, sender: str) -> None:
        """Save the review report to local storage."""
        storage = self.get_tool("file_storage")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        report_name = f"nda_review_{timestamp}_{filename}.json"

        report = {
            "filename": filename,
            "sender": sender,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "review": review,
        }

        storage.execute(action="save", filename=report_name, data=report, subfolder="nda_reviews")

    def _send_findings(self, review: dict, filename: str, sender: str, original_subject: str) -> None:
        """Email the review findings back to the sender."""
        email_sender = self.get_tool("email_sender")

        risk_score = review.get("risk_score", "N/A")
        risk_level = review.get("risk_level", "unknown")
        summary = review.get("summary", "No summary available.")
        concerns = review.get("concerns", [])
        suggestions = review.get("suggested_revisions", [])

        body = f"""NDA Review Results for: {filename}
{'=' * 60}

Risk Score: {risk_score}/10 ({risk_level.upper()})

Summary:
{summary}

"""
        if concerns:
            body += "Concerns:\n"
            for i, concern in enumerate(concerns, 1):
                body += f"  {i}. {concern}\n"
            body += "\n"

        if suggestions:
            body += "Suggested Revisions:\n"
            for i, suggestion in enumerate(suggestions, 1):
                body += f"  {i}. {suggestion}\n"
            body += "\n"

        clauses = review.get("clauses_analysis", [])
        if clauses:
            body += "Clause Analysis:\n"
            for clause in clauses:
                body += f"  - {clause.get('clause', 'Unknown')}: [{clause.get('risk', 'N/A')}] {clause.get('finding', '')}\n"
                if clause.get("recommendation"):
                    body += f"    Recommendation: {clause['recommendation']}\n"
            body += "\n"

        body += """---
This analysis was generated by Secure Agents NDA Reviewer.
This is not legal advice. Please consult with your legal team for final review.
"""

        email_sender.execute(
            to=sender,
            subject=f"Re: {original_subject} - NDA Review Results [{risk_level.upper()}]",
            body=body,
        )

        self.audit.log("findings_sent", filename=filename, recipient=sender, risk_score=risk_score)
