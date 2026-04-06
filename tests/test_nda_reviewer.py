"""Tests for the NDA Reviewer agent."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from secure_agents.core.base_provider import CompletionResponse, Message
from secure_agents.agents.nda_reviewer.agent import NDAReviewerAgent, _is_nda_candidate


# --- NDA detection heuristics ---

def test_nda_detection_by_filename():
    assert _is_nda_candidate("Company_NDA_2024.pdf") is True
    assert _is_nda_candidate("non-disclosure-agreement.docx") is True
    assert _is_nda_candidate("confidentiality_agreement.pdf") is True
    assert _is_nda_candidate("invoice_2024.pdf") is False
    assert _is_nda_candidate("meeting_notes.docx") is False


def test_nda_detection_by_content():
    nda_text = """
    This Non-Disclosure Agreement is entered into by and between
    Company A (the Disclosing Party) and Company B (the Receiving Party).
    Both parties agree to maintain confidentiality of all shared information.
    """
    assert _is_nda_candidate("document.pdf", nda_text) is True

    invoice_text = "Invoice #1234. Amount due: $5,000. Payment terms: Net 30."
    assert _is_nda_candidate("document.pdf", invoice_text) is False


# --- Agent integration test ---

def test_nda_reviewer_analyze():
    """Test the analysis pipeline with a mock provider."""
    mock_review = {
        "summary": "Standard mutual NDA between Company A and Company B.",
        "risk_score": 3,
        "risk_level": "low",
        "parties": {"disclosing": "Company A", "receiving": "Company B"},
        "key_terms": {
            "duration": "2 years",
            "confidentiality_period": "5 years",
            "governing_law": "Delaware",
            "termination": "30 days written notice",
        },
        "clauses_analysis": [
            {
                "clause": "Definition of Confidential Information",
                "risk": "low",
                "finding": "Broad but standard definition",
                "recommendation": "Acceptable as-is",
            }
        ],
        "concerns": [],
        "missing_clauses": [],
        "suggested_revisions": [],
    }

    # Create mock provider
    mock_provider = MagicMock()
    mock_provider.complete.return_value = CompletionResponse(
        content=json.dumps(mock_review),
        model="test-model",
    )

    # Create mock tools
    mock_email_reader = MagicMock()
    mock_email_sender = MagicMock()
    mock_email_sender.execute.return_value = {"sent": True}
    mock_doc_parser = MagicMock()

    with tempfile.TemporaryDirectory() as tmp:
        from secure_agents.tools.file_storage import FileStorageTool
        file_storage = FileStorageTool(config={"output_dir": tmp})

        tools = {
            "email_reader": mock_email_reader,
            "email_sender": mock_email_sender,
            "document_parser": mock_doc_parser,
            "file_storage": file_storage,
        }

        agent = NDAReviewerAgent(
            tools=tools,
            provider=mock_provider,
            config={"poll_interval_seconds": 0, "security": {"audit_log_path": str(Path(tmp) / "audit.log")}},
        )

        # Test the analysis method directly
        result = agent._analyze_nda("Sample NDA text here", "test_nda.pdf")
        assert result is not None
        assert result["risk_score"] == 3
        assert result["risk_level"] == "low"
        assert mock_provider.complete.called


def test_nda_reviewer_handles_bad_json():
    """Test that the agent handles malformed LLM output gracefully."""
    mock_provider = MagicMock()
    mock_provider.complete.return_value = CompletionResponse(
        content="This is not valid JSON!",
        model="test-model",
    )

    with tempfile.TemporaryDirectory() as tmp:
        tools = {
            "email_reader": MagicMock(),
            "email_sender": MagicMock(),
            "document_parser": MagicMock(),
            "file_storage": MagicMock(),
        }

        agent = NDAReviewerAgent(
            tools=tools,
            provider=mock_provider,
            config={"poll_interval_seconds": 0, "security": {"audit_log_path": str(Path(tmp) / "audit.log")}},
        )

        result = agent._analyze_nda("Sample text", "test.pdf")
        assert result is None
