"""Prompts and schemas for the deduplicator agents.

Edit the SYSTEM_PROMPT below to tune similarity judgment.  The JSON
schema constrains the LLM output shape.
"""

from __future__ import annotations

from typing import Any

# ── Dedup comparison schema (structured output, layer 1) ─────────────────────

DEDUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["is_similar", "confidence", "reasoning"],
    "properties": {
        "is_similar": {
            "type": "boolean",
            "description": (
                "True if the two documents are substantially the same "
                "(duplicates or near-duplicates), False otherwise."
            ),
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Confidence in the similarity judgment (0.0 to 1.0).",
        },
        "reasoning": {
            "type": "string",
            "minLength": 1,
            "maxLength": 300,
            "description": "Brief explanation of the similarity or differences (1-3 sentences).",
        },
    },
    "additionalProperties": False,
}

# ── System prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a legal document de-duplication assistant.  You will be given the
extracted text of TWO documents.  Your job is to determine whether they
are **the same document** — i.e. a true duplicate or near-identical copy.

Mark as duplicate (is_similar = true) ONLY when the two documents are
clearly the same file or the same signed agreement with trivial differences:
- The same document saved in two formats (e.g. one PDF and one DOCX).
- Two copies of the same executed/signed agreement with no meaningful
  differences in content (e.g. a scan and a digital copy).
- The same document with only cosmetic differences: page numbers,
  headers/footers, whitespace, font, or identical tracked-changes that
  do not change any clause.

Mark as NOT duplicate (is_similar = false) for everything else, including:
- Different versions or drafts of a negotiation (even if only one clause
  changed — version 1 vs version 2, clean vs redlined, etc.).
- The same template filled in for different customers or counterparties.
- Amendments, renewals, or addenda to an earlier agreement.
- Documents with any substantive difference in parties, dates, pricing,
  scope, or terms.

Be strict: when in doubt, return false.  Only return true when the two
documents are essentially identical copies of the same final document.

Return your answer as a JSON object with these keys:
  is_similar  -- true or false
  confidence  -- a float between 0.0 and 1.0
  reasoning   -- a brief explanation (1-3 sentences)
"""

# ── Instruction injected before untrusted document text ──────────────────────

COMPARE_INSTRUCTION = (
    "Compare the following two documents and determine whether they are "
    "the exact same document (e.g. PDF and DOCX of the same file, or two "
    "identical copies).  Be strict — different versions, redlines, or "
    "different parties are NOT duplicates.  "
    "Return ONLY the JSON object described in the system prompt."
)
