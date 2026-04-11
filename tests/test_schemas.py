"""Tests for JSON Schema validation of structured LLM outputs."""

import json

from secure_agents.core.schemas import (
    NDA_REVIEW_SCHEMA,
    VALIDATOR_VERDICT_SCHEMA,
    validate_schema,
)


# ── NDA review schema ──────────────────────────────────────────────────────

def _valid_nda_review() -> dict:
    return {
        "summary": "Standard mutual NDA between two companies.",
        "risk_score": 3,
        "risk_level": "low",
        "parties": {"disclosing": "Company A", "receiving": "Company B"},
        "key_terms": {
            "duration": "2 years",
            "confidentiality_period": "5 years",
            "governing_law": "Delaware",
            "termination": "30 days notice",
        },
        "clauses_analysis": [
            {
                "clause": "Definition of Confidential Information",
                "risk": "low",
                "finding": "Standard definition.",
                "recommendation": "Accept.",
            }
        ],
        "concerns": [],
        "missing_clauses": [],
        "suggested_revisions": [],
    }


def test_nda_review_schema_accepts_valid():
    payload = json.dumps(_valid_nda_review())
    ok, result = validate_schema(payload, NDA_REVIEW_SCHEMA)
    assert ok is True
    assert result["risk_score"] == 3


def test_nda_review_schema_rejects_invalid_json():
    ok, err = validate_schema("this is not json", NDA_REVIEW_SCHEMA)
    assert ok is False
    assert "Invalid JSON" in err


def test_nda_review_schema_rejects_missing_required_field():
    data = _valid_nda_review()
    del data["risk_score"]
    ok, err = validate_schema(json.dumps(data), NDA_REVIEW_SCHEMA)
    assert ok is False
    assert "risk_score" in err


def test_nda_review_schema_rejects_out_of_range_score():
    data = _valid_nda_review()
    data["risk_score"] = 15
    ok, err = validate_schema(json.dumps(data), NDA_REVIEW_SCHEMA)
    assert ok is False
    assert "15" in err or "maximum" in err


def test_nda_review_schema_rejects_bad_enum():
    data = _valid_nda_review()
    data["risk_level"] = "catastrophic"  # not in enum
    ok, err = validate_schema(json.dumps(data), NDA_REVIEW_SCHEMA)
    assert ok is False
    assert "catastrophic" in err or "enum" in err.lower() or "not in" in err


def test_nda_review_schema_rejects_wrong_type():
    data = _valid_nda_review()
    data["risk_score"] = "three"  # string instead of int
    ok, err = validate_schema(json.dumps(data), NDA_REVIEW_SCHEMA)
    assert ok is False
    assert "integer" in err


def test_nda_review_schema_rejects_additional_properties():
    data = _valid_nda_review()
    data["injected_field"] = "malicious payload"
    ok, err = validate_schema(json.dumps(data), NDA_REVIEW_SCHEMA)
    assert ok is False
    assert "injected_field" in err


def test_nda_review_schema_rejects_bad_nested_enum():
    data = _valid_nda_review()
    data["clauses_analysis"][0]["risk"] = "apocalyptic"
    ok, err = validate_schema(json.dumps(data), NDA_REVIEW_SCHEMA)
    assert ok is False


# ── Validator verdict schema ───────────────────────────────────────────────

def test_validator_verdict_accepts_valid_safe():
    payload = json.dumps({"safe": True, "confidence": 0.9, "reasons": []})
    ok, result = validate_schema(payload, VALIDATOR_VERDICT_SCHEMA)
    assert ok is True
    assert result["safe"] is True


def test_validator_verdict_accepts_valid_unsafe():
    payload = json.dumps({
        "safe": False,
        "confidence": 0.95,
        "reasons": ["Contains prompt injection", "Role switching detected"],
    })
    ok, result = validate_schema(payload, VALIDATOR_VERDICT_SCHEMA)
    assert ok is True
    assert result["safe"] is False
    assert len(result["reasons"]) == 2


def test_validator_verdict_rejects_confidence_out_of_range():
    payload = json.dumps({"safe": True, "confidence": 1.5, "reasons": []})
    ok, err = validate_schema(payload, VALIDATOR_VERDICT_SCHEMA)
    assert ok is False


def test_validator_verdict_rejects_missing_reasons():
    payload = json.dumps({"safe": True, "confidence": 0.9})
    ok, err = validate_schema(payload, VALIDATOR_VERDICT_SCHEMA)
    assert ok is False
    assert "reasons" in err
