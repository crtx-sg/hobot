"""PHI redaction and re-injection for non-PHI-safe LLM providers."""

import re
import uuid

# Patterns that may contain PHI
_PATTERNS = [
    # Patient IDs: P001-P999, UHID followed by digits
    (re.compile(r"\b(P\d{3,})\b"), "PATIENT_ID"),
    (re.compile(r"\b(UHID\d+)\b"), "PATIENT_ID"),
    # MRN identifiers
    (re.compile(r"\b(MRN\d+)\b"), "MRN"),
    # Dates of birth: YYYY-MM-DD
    (re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"), "DATE"),
    # Phone numbers: various formats
    (re.compile(r"\b(\+?\d[\d\-\s]{8,14}\d)\b"), "PHONE"),
]


def redact(text: str) -> tuple[str, dict[str, str]]:
    """Replace PHI patterns with placeholders. Returns (redacted_text, mapping)."""
    mapping: dict[str, str] = {}
    result = text
    for pattern, label in _PATTERNS:
        for match in pattern.finditer(result):
            original = match.group(1)
            if original not in mapping.values():
                token = f"[{label}_{uuid.uuid4().hex[:6]}]"
                mapping[token] = original
    # Apply replacements (reverse mapping: original -> token)
    reverse = {v: k for k, v in mapping.items()}
    for original, token in reverse.items():
        result = result.replace(original, token)
    return result, mapping


def restore(text: str, mapping: dict[str, str]) -> str:
    """Re-inject original PHI values from redaction mapping."""
    result = text
    for token, original in mapping.items():
        result = result.replace(token, original)
    return result
