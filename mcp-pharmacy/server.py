"""MCP Pharmacy Tool Server for Hobot.

Provides medication lookup, order status, dispensing, and drug interaction
checking via MCP tools over stdio transport. Connects to the synthetic-pharmacy
backend for live data and maintains a local degraded-mode cache.
"""

import os
import threading
import time
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PHARMACY_BASE = os.environ.get("PHARMACY_BASE", "http://synthetic-pharmacy:8000")

# ---------------------------------------------------------------------------
# Degraded-mode cache
# ---------------------------------------------------------------------------

_cache: dict[str, dict[str, Any]] = {}
# Each entry: {"data": <response_json>, "ts": <epoch float>}


def _cache_get(key: str) -> dict | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    staleness = time.time() - entry["ts"]
    return {
        "data": entry["data"],
        "cached": True,
        "staleness_seconds": round(staleness, 1),
        "warning": f"Backend unavailable. Serving cached data ({round(staleness)}s old).",
    }


def _cache_set(key: str, data: Any) -> None:
    _cache[key] = {"data": data, "ts": time.time()}


# ---------------------------------------------------------------------------
# Hardcoded drug-interaction database
# ---------------------------------------------------------------------------

INTERACTION_DB: list[dict[str, Any]] = [
    {
        "pair": {"warfarin", "aspirin"},
        "severity": "high",
        "description": "Increased risk of bleeding. Concurrent use of warfarin and aspirin significantly elevates hemorrhagic risk.",
    },
    {
        "pair": {"metformin", "contrast"},
        "severity": "high",
        "description": "Risk of lactic acidosis. Metformin should be held before and after iodinated contrast administration.",
    },
    {
        "pair": {"ace-inhibitor", "potassium"},
        "severity": "moderate",
        "description": "Risk of hyperkalemia. ACE inhibitors reduce potassium excretion; supplemental potassium may cause dangerous elevation.",
    },
    {
        "pair": {"ssri", "nsaid"},
        "severity": "moderate",
        "description": "Increased risk of GI bleeding. SSRIs impair platelet function and NSAIDs irritate gastric mucosa.",
    },
]

# Aliases so users can pass common drug names and still match categories.
_ALIASES: dict[str, str] = {
    # ACE inhibitors
    "lisinopril": "ace-inhibitor",
    "enalapril": "ace-inhibitor",
    "ramipril": "ace-inhibitor",
    "captopril": "ace-inhibitor",
    "benazepril": "ace-inhibitor",
    "ace-inhibitor": "ace-inhibitor",
    "ace inhibitor": "ace-inhibitor",
    # SSRIs
    "fluoxetine": "ssri",
    "sertraline": "ssri",
    "paroxetine": "ssri",
    "citalopram": "ssri",
    "escitalopram": "ssri",
    "ssri": "ssri",
    # NSAIDs
    "ibuprofen": "nsaid",
    "naproxen": "nsaid",
    "aspirin": "aspirin",  # aspirin keeps own name for warfarin pair
    "diclofenac": "nsaid",
    "nsaid": "nsaid",
    # Others
    "warfarin": "warfarin",
    "metformin": "metformin",
    "contrast": "contrast",
    "potassium": "potassium",
}


def _normalize(med: str) -> set[str]:
    """Return the set of canonical names a medication maps to."""
    key = med.strip().lower()
    canonical = _ALIASES.get(key, key)
    return {canonical}


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

_http_client: httpx.AsyncClient | None = None


async def _client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(base_url=PHARMACY_BASE, timeout=10.0)
    return _http_client


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp_server = FastMCP(
    "pharmacy",
    instructions=(
        "Pharmacy MCP server. Provides tools to look up patient medications, "
        "check order status, dispense medications (CRITICAL — requires confirmation), "
        "and check drug interactions."
    ),
)


@mcp_server.tool()
async def get_medications(patient_id: str) -> dict:
    """Retrieve the current medication list for a patient.

    Args:
        patient_id: The unique patient identifier.

    Returns:
        Medication list from the pharmacy system.
    """
    cache_key = f"medications:{patient_id}"
    try:
        client = await _client()
        resp = await client.get(f"/medications/{patient_id}")
        resp.raise_for_status()
        data = resp.json()
        _cache_set(cache_key, data)
        return data
    except Exception:
        cached = _cache_get(cache_key)
        if cached:
            return cached
        return {"error": "Pharmacy backend unavailable and no cached data exists."}


@mcp_server.tool()
async def get_order_status(order_id: str) -> dict:
    """Check the status of a pharmacy order.

    Args:
        order_id: The unique order identifier.

    Returns:
        Order status details.
    """
    cache_key = f"order:{order_id}"
    try:
        client = await _client()
        resp = await client.get(f"/order/{order_id}")
        resp.raise_for_status()
        data = resp.json()
        _cache_set(cache_key, data)
        return data
    except Exception:
        cached = _cache_get(cache_key)
        if cached:
            return cached
        return {"error": "Pharmacy backend unavailable and no cached data exists."}


@mcp_server.tool()
async def dispense_medication(
    patient_id: str,
    medication: str,
    dose: str,
    route: str,
    frequency: str,
) -> dict:
    """CRITICAL: Dispense a medication to a patient.

    This is a critical action that results in physical medication dispensing.
    Ensure all parameters are verified before calling.

    Args:
        patient_id: The unique patient identifier.
        medication: Name of the medication to dispense.
        dose: Dosage amount and unit (e.g. "500mg").
        route: Administration route (e.g. "oral", "IV", "IM").
        frequency: Dosing frequency (e.g. "BID", "Q8H", "once").

    Returns:
        Dispensing confirmation or error details.
    """
    payload = {
        "patient_id": patient_id,
        "medication": medication,
        "dose": dose,
        "route": route,
        "frequency": frequency,
    }
    try:
        client = await _client()
        resp = await client.post("/dispense", json=payload)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return {
            "error": f"Failed to dispense medication: {exc}",
            "critical": True,
            "message": "Dispensing could not be completed. Do NOT assume medication was given.",
        }


@mcp_server.tool()
async def check_drug_interactions(medications: list[str]) -> dict:
    """Check a list of medications for known drug interactions.

    Uses a local hardcoded interaction database. Checks pairs including:
    warfarin+aspirin, metformin+contrast, ACE-inhibitors+potassium, SSRIs+NSAIDs.

    Common drug names (e.g. lisinopril, ibuprofen, sertraline) are mapped to
    their drug classes automatically.

    Args:
        medications: List of medication names to check against each other.

    Returns:
        Dict with list of interactions found and the normalized medication set.
    """
    # Build set of canonical names across all input medications.
    canonical_set: set[str] = set()
    for med in medications:
        canonical_set.update(_normalize(med))

    found: list[dict[str, str]] = []
    for entry in INTERACTION_DB:
        pair: set[str] = entry["pair"]
        if pair.issubset(canonical_set):
            found.append(
                {
                    "drugs": sorted(pair),
                    "severity": entry["severity"],
                    "description": entry["description"],
                }
            )

    return {
        "medications_checked": sorted(canonical_set),
        "interaction_count": len(found),
        "interactions": found,
    }


# ---------------------------------------------------------------------------
# Health endpoint (FastAPI on port 8000 in background thread)
# ---------------------------------------------------------------------------

health_app = FastAPI(title="mcp-pharmacy-health")


@health_app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "mcp-pharmacy",
        "cache_entries": len(_cache),
    }


def _run_health_server() -> None:
    uvicorn.run(health_app, host="0.0.0.0", port=8000, log_level="warning")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Start health endpoint in a daemon thread so it doesn't block stdio.
    health_thread = threading.Thread(target=_run_health_server, daemon=True)
    health_thread.start()

    # Run MCP server on stdio transport (blocking).
    mcp_server.run(transport="stdio")
