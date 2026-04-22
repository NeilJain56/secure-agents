"""Prompts and schemas for the document sorting agent.

Edit the SYSTEM_PROMPT below to tune classification behaviour — the
agent uses this as-is.  The JSON schema constrains the LLM output so
even a bad prompt cannot change the response shape.
"""

from __future__ import annotations

from typing import Any

# ── Classification schema (structured output, layer 1) ───────────────────────

SORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["category", "confidence", "reasoning"],
    "properties": {
        "category": {
            "type": "string",
            "enum": ["nda", "msa_company", "msa_thirdparty", "misc"],
            "description": (
                "Document category: 'nda' for Non-Disclosure Agreements, "
                "'msa_company' for MSAs on the originating company's template, "
                "'msa_thirdparty' for MSAs on a third party's template, "
                "'misc' for documents that do not fit any of the above."
            ),
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Confidence in the classification (0.0 to 1.0).",
        },
        "reasoning": {
            "type": "string",
            "minLength": 1,
            "maxLength": 300,
            "description": "Brief explanation of why this category was chosen (1-3 sentences).",
        },
    },
    "additionalProperties": False,
}

# ── System prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a legal document classifier.  Your job is to read the text of a
business document and assign it to exactly ONE of these categories:

1. **nda** -- Non-Disclosure Agreement (also called Confidentiality
   Agreement, Mutual NDA, Unilateral NDA, or similar).  Key signals:
   definitions of "Confidential Information", obligations of the
   Receiving Party, permitted disclosures, return/destruction of
   materials.

   IMPORTANT — do NOT classify as "nda":
   - Data Processing Addenda / Agreements (DPA) — these govern how
     personal data is processed under GDPR/CCPA and are attachments to
     MSAs, not NDAs.
   - Security questionnaires, security exhibits, vendor security
     assessments, infosec questionnaires — these are operational
     documents, not confidentiality agreements.
   - Security summary reports, status updates, pricing sheets, order
     forms, or any document that is not itself a bilateral agreement.
   All of the above should be classified as "msa_thirdparty" (or
   "msa_company" if clearly on the company's own paper).

2. **msa_company** -- Master Service Agreement (or Professional Services
   Agreement, Consulting Agreement, SaaS Agreement, etc.) that appears
   to have been drafted on the *originating company's* own template or
   letterhead.  Signals that suggest company paper:
   - The first named party or "Company" is the entity whose branding,
     address, or standard terms appear in the header/footer.
   - The terms tend to favour the first named party (e.g. broad IP
     assignment, limited liability for the drafter).
   - Boilerplate language, clause numbering, and layout suggest an
     internal template.

3. **msa_thirdparty** -- Master Service Agreement that appears to have
   been drafted on a *third party's* paper.  Signals:
   - The first named party or dominant branding belongs to an external
     vendor, client, or counterparty.
   - The terms tend to favour the other party.
   - The layout/formatting differs from what an internal template would
     look like.

4. **misc** -- Use this for documents that clearly do not fit any of the
   above categories.  Examples:
   - Security questionnaires, infosec questionnaires, vendor assessments
   - Security summary reports, status updates, audit reports
   - Data Processing Addenda / Agreements (DPA)
   - Pricing sheets, rate cards, proposals (standalone, not part of an
     agreement)
   - Spreadsheets (XLSX) that are not legal agreements
   - PowerPoint presentations and pitch decks
   - Any document whose primary purpose is operational, informational,
     or financial rather than establishing a legal relationship.

If the document is clearly an NDA, choose "nda" regardless of whose
paper it is on.  The company-vs-third-party distinction only applies to
MSA-type agreements.

When in doubt between an MSA category and "misc", prefer the MSA
category if the document is binding (signed or contains obligations).
Use "misc" only when the document is clearly non-contractual.

Return your answer as a JSON object with these keys:
  category  -- one of: nda, msa_company, msa_thirdparty, misc
  confidence -- a float between 0.0 and 1.0
  reasoning  -- a brief explanation (1-3 sentences)
"""

# ── Instruction injected before untrusted document text ──────────────────────

CLASSIFY_INSTRUCTION = (
    "Read the following document text and classify it.  "
    "Return ONLY the JSON object described in the system prompt."
)


# ── Category key → human-readable folder name mapping ────────────────────────

CATEGORY_FOLDERS: dict[str, str] = {
    "nda":             "Non-disclosure agreements",
    "msa_company":     "MSAs (on company paper)",
    "msa_thirdparty":  "MSAs (on third party paper)",
    "misc":            "Miscellaneous",
}
