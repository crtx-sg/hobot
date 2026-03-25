"""PHI redaction and re-injection for non-PHI-safe LLM providers."""

import re
import uuid

# Patterns that may contain PHI
_PATTERNS = [
    # Patient IDs: P001-P999, UHID followed by digits
    (re.compile(r"\b(P\d{3,})\b"), "PID"),
    (re.compile(r"\b(UHID\d+)\b"), "PID"),
    # MRN identifiers
    (re.compile(r"\b(MRN\d+)\b"), "MRN"),
    # Dates of birth: YYYY-MM-DD
    (re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"), "DTE"),
    # Phone numbers: various formats
    (re.compile(r"\b(\+?\d[\d\-\s]{8,14}\d)\b"), "PHN"),
    # Email addresses
    (re.compile(r"(\S+@\S+\.\S+)"), "EML"),
    # SSN: 123-45-6789
    (re.compile(r"\b(\d{3}-\d{2}-\d{4})\b"), "SSN"),
    # Aadhaar: 1234 5678 9012
    (re.compile(r"\b(\d{4}\s\d{4}\s\d{4})\b"), "ADR"),
    # Doctor names after "Dr."
    (re.compile(r"(Dr\.?\s+[A-Z][a-z]+)"), "DNM"),
]

# Prefix used for all tokens — must be unlikely in normal text
_TOKEN_PREFIX = "XPHI"


def redact(text: str) -> tuple[str, dict[str, str]]:
    """Replace PHI patterns with opaque placeholder tokens.

    Tokens use the format XPHI_<label>_<hex> (no brackets) so LLMs treat
    them as opaque identifiers and pass them through verbatim to tool calls.

    Returns (redacted_text, mapping) where mapping is {token: original}.
    """
    mapping: dict[str, str] = {}
    result = text
    # Collect all matches first, dedup by original value
    seen_originals: dict[str, str] = {}  # original -> token
    for pattern, label in _PATTERNS:
        for match in pattern.finditer(result):
            original = match.group(1)
            if original not in seen_originals:
                hex_id = uuid.uuid4().hex[:8]
                token = f"{_TOKEN_PREFIX}_{label}_{hex_id}"
                seen_originals[original] = token
                mapping[token] = original
    # Apply replacements: longest originals first to avoid partial matches
    for original, token in sorted(seen_originals.items(), key=lambda x: -len(x[0])):
        result = result.replace(original, token)
    return result, mapping


def restore(text: str, mapping: dict[str, str]) -> str:
    """Re-inject original PHI values from redaction mapping.

    Handles several ways LLMs may mangle tokens:
    - Exact token:         XPHI_PID_a1b2c3d4  → P001
    - Brackets added:      [XPHI_PID_a1b2c3d4] → P001
    - Hex suffix only:     a1b2c3d4            → P001
    - Label + hex:         PID_a1b2c3d4        → P001
    """
    result = text

    # Build secondary lookups for fuzzy matching
    hex_map: dict[str, str] = {}  # hex suffix -> original
    label_hex_map: dict[str, str] = {}  # label_hex -> original

    for token, original in mapping.items():
        parts = token.split("_")
        if len(parts) >= 3:
            hex_id = parts[-1]
            label_hex = "_".join(parts[1:])  # e.g. PID_a1b2c3d4
            hex_map[hex_id] = original
            label_hex_map[label_hex] = original

    # Pass 1: exact token matches (most specific)
    for token, original in mapping.items():
        result = result.replace(token, original)
        # Also handle if LLM wrapped in brackets
        result = result.replace(f"[{token}]", original)

    # Pass 2: label_hex matches (e.g. PID_a1b2c3d4)
    for label_hex, original in label_hex_map.items():
        result = result.replace(label_hex, original)

    # Pass 3: hex-only matches — only replace if hex is ≥8 chars and stands
    # alone (word boundary) to avoid false positives
    for hex_id, original in hex_map.items():
        if len(hex_id) >= 8:
            result = re.sub(rf"\b{re.escape(hex_id)}\b", original, result)

    return result
