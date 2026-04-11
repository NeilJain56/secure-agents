"""JSON Schema definitions and validation for structured LLM outputs.

Structured outputs are the primary defense against prompt injection:
by constraining the LLM to produce only values that fit a predefined
schema, injected instructions in untrusted input cannot change the
output *shape* — even if they affect individual field values.

The validator LLM (see ``validator.py``) is the secondary defense,
screening content *before* it reaches the primary LLM.

Usage:
    from secure_agents.core.schemas import NDA_REVIEW_SCHEMA, validate_schema

    response = provider.complete(messages, response_schema=NDA_REVIEW_SCHEMA)
    ok, result = validate_schema(response.content, NDA_REVIEW_SCHEMA)
"""

from __future__ import annotations

import json
from typing import Any

import structlog

logger = structlog.get_logger()


# ── JSON Schema: NDA Review ─────────────────────────────────────────────────

NDA_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "summary",
        "risk_score",
        "risk_level",
        "parties",
        "key_terms",
        "clauses_analysis",
        "concerns",
        "missing_clauses",
        "suggested_revisions",
    ],
    "properties": {
        "summary": {
            "type": "string",
            "description": "Brief 2-3 sentence summary of the NDA.",
        },
        "risk_score": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10,
            "description": "Overall risk score from 1 (very low) to 10 (very high).",
        },
        "risk_level": {
            "type": "string",
            "enum": ["low", "medium", "high", "critical"],
        },
        "parties": {
            "type": "object",
            "required": ["disclosing", "receiving"],
            "properties": {
                "disclosing": {"type": "string"},
                "receiving": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "key_terms": {
            "type": "object",
            "required": ["duration", "confidentiality_period", "governing_law", "termination"],
            "properties": {
                "duration": {"type": "string"},
                "confidentiality_period": {"type": "string"},
                "governing_law": {"type": "string"},
                "termination": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "clauses_analysis": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["clause", "risk", "finding", "recommendation"],
                "properties": {
                    "clause": {"type": "string"},
                    "risk": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                    },
                    "finding": {"type": "string"},
                    "recommendation": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        "concerns": {
            "type": "array",
            "items": {"type": "string"},
        },
        "missing_clauses": {
            "type": "array",
            "items": {"type": "string"},
        },
        "suggested_revisions": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "additionalProperties": False,
}


# ── JSON Schema: Validator Verdict ──────────────────────────────────────────

VALIDATOR_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["safe", "confidence", "reasons"],
    "properties": {
        "safe": {
            "type": "boolean",
            "description": "True if the content appears safe for analysis; False if it contains injection attempts.",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Confidence in the safety verdict (0.0 = uncertain, 1.0 = certain).",
        },
        "reasons": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of reasons for the verdict. Empty if safe with high confidence.",
        },
    },
    "additionalProperties": False,
}


# ── Validation ──────────────────────────────────────────────────────────────

def validate_schema(raw_json: str, schema: dict[str, Any]) -> tuple[bool, dict | str]:
    """Parse JSON and validate it against a schema.

    Uses a lightweight recursive validator (no external dependency).
    Returns (True, parsed_dict) on success, (False, error_message) on failure.
    """
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        return False, f"Invalid JSON: {e}"

    errors = _validate_value(data, schema, path="$")
    if errors:
        logger.warning("schema.validation_failed", errors=errors[:5])
        return False, "; ".join(errors[:5])

    return True, data


def _validate_value(value: Any, schema: dict, path: str) -> list[str]:
    """Recursively validate a value against a JSON Schema subset.

    Supports: type, required, properties, additionalProperties, items,
    enum, minimum, maximum, minLength, maxLength.
    """
    errors: list[str] = []

    # Type check
    expected_type = schema.get("type")
    if expected_type:
        if not _check_type(value, expected_type):
            errors.append(f"{path}: expected type '{expected_type}', got {type(value).__name__}")
            return errors  # skip deeper checks if type is wrong

    # Enum
    if "enum" in schema:
        if value not in schema["enum"]:
            errors.append(f"{path}: value {value!r} not in {schema['enum']}")

    # Numeric bounds
    if "minimum" in schema and isinstance(value, (int, float)):
        if value < schema["minimum"]:
            errors.append(f"{path}: {value} < minimum {schema['minimum']}")
    if "maximum" in schema and isinstance(value, (int, float)):
        if value > schema["maximum"]:
            errors.append(f"{path}: {value} > maximum {schema['maximum']}")

    # String length
    if "minLength" in schema and isinstance(value, str):
        if len(value) < schema["minLength"]:
            errors.append(f"{path}: string length {len(value)} < minLength {schema['minLength']}")
    if "maxLength" in schema and isinstance(value, str):
        if len(value) > schema["maxLength"]:
            errors.append(f"{path}: string length {len(value)} > maxLength {schema['maxLength']}")

    # Object validation
    if expected_type == "object" and isinstance(value, dict):
        # Required fields
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}: missing required field '{req}'")

        # Property validation
        props = schema.get("properties", {})
        for key, val in value.items():
            if key in props:
                errors.extend(_validate_value(val, props[key], f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected field '{key}'")

    # Array validation
    if expected_type == "array" and isinstance(value, list):
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(value):
                errors.extend(_validate_value(item, item_schema, f"{path}[{i}]"))

    return errors


def _check_type(value: Any, expected: str) -> bool:
    """Check a value against a JSON Schema type name."""
    type_map = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
        "null": type(None),
    }
    expected_types = type_map.get(expected)
    if expected_types is None:
        return True  # unknown type, pass
    # In JSON, booleans are not integers
    if expected == "integer" and isinstance(value, bool):
        return False
    if expected == "number" and isinstance(value, bool):
        return False
    return isinstance(value, expected_types)
