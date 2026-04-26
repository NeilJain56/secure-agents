"""Prompts and schemas for the reps & warranties reviewer agent."""

from __future__ import annotations

from typing import Any

# ── Schema ────────────────────────────────────────────────────────────────────

REPS_ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["results"],
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "rep_id", "triggered", "confidence",
                    "quoted_language", "reasoning",
                ],
                "properties": {
                    "rep_id": {
                        "type": "string",
                        "description": "Matches the id field of the rep being evaluated.",
                    },
                    "triggered": {
                        "type": "boolean",
                        "description": (
                            "True if this contract would need to be disclosed in response "
                            "to this rep. False if the rep is not implicated."
                        ),
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "description": "Confidence in the triggered judgment (0.0–1.0).",
                    },
                    "quoted_language": {
                        "type": "string",
                        "description": (
                            "Verbatim language copied from the contract that triggers this "
                            "rep. Empty string if not triggered."
                        ),
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "1–2 sentence explanation of the judgment.",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a legal analyst assisting with due diligence for a commercial acquisition.
Your task is to review a contract and determine which representations and warranties
(reps) in a purchase agreement are triggered by that contract.

A rep is "triggered" if the contract would need to be DISCLOSED on a disclosure
schedule in order for that rep to be accurate as written. In other words: would a
reasonable lawyer reviewing this contract say "this contract needs to go on the
schedule for Rep X"?

For each rep you evaluate:
1. Determine whether the contract triggers a disclosure obligation.
2. If TRIGGERED: copy verbatim the exact language from the contract that creates the
   disclosure obligation. Do not paraphrase — use the exact words from the document.
3. Rate your confidence from 0.0 (very uncertain) to 1.0 (certain).
4. Write 1–2 sentences explaining why the rep is or is not triggered.

Important rules:
- Err on the side of flagging (triggered = true) when uncertain. A missed disclosure
  is more harmful than an over-disclosure. The human reviewer will make the final call.
- Only quote language that is actually present in the contract text provided.
- Return one result entry per rep, in the same order the reps are listed, using the
  exact rep_id values provided.
- Do not skip any rep — even clearly non-triggered reps need a result entry.

Return ONLY a JSON object with a "results" array. No other text.
"""


def build_review_instruction(reps: list[dict]) -> str:
    """Build the numbered rep list to inject before untrusted contract text."""
    count = len(reps)
    lines = [
        f"Review the contract below against these {count} "
        f"representation{'s' if count != 1 else ''} and "
        f"warrant{'ies' if count != 1 else 'y'}. "
        f"Return one result per rep using the exact rep_id values shown.\n",
    ]
    for i, rep in enumerate(reps, 1):
        lines.append(f"{i}. rep_id: \"{rep['id']}\"")
        lines.append(f"   Title: {rep['title']}")
        lines.append(f"   Text: \"{rep['text']}\"")
        lines.append("")
    return "\n".join(lines)
