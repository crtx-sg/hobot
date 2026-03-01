"""MCP tool server for synthetic-monitoring backend."""

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
MONITORING_BASE = os.environ.get("MONITORING_BASE", "http://synthetic-monitoring:8000")

# ---------------------------------------------------------------------------
# Degraded-mode cache: last successful response per patient per endpoint
# ---------------------------------------------------------------------------
_cache: dict[str, dict[str, Any]] = {}  # key = "{endpoint}:{patient_id}"
_cache_ts: dict[str, float] = {}


def _cache_key(endpoint: str, patient_id: str) -> str:
    return f"{endpoint}:{patient_id}"


def _put_cache(endpoint: str, patient_id: str, data: Any) -> None:
    key = _cache_key(endpoint, patient_id)
    _cache[key] = data
    _cache_ts[key] = time.time()


def _get_cached(endpoint: str, patient_id: str) -> dict[str, Any] | None:
    key = _cache_key(endpoint, patient_id)
    if key not in _cache:
        return None
    staleness_secs = round(time.time() - _cache_ts[key], 1)
    return {
        "data": _cache[key],
        "warning": f"DEGRADED MODE: serving cached data ({staleness_secs}s stale). Live backend unreachable.",
    }


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
async def _get(path: str, patient_id: str, endpoint_label: str) -> dict[str, Any]:
    """GET from monitoring backend with degraded-mode fallback."""
    url = f"{MONITORING_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            _put_cache(endpoint_label, patient_id, data)
            return data
    except Exception as exc:
        cached = _get_cached(endpoint_label, patient_id)
        if cached is not None:
            return cached
        return {"error": f"Backend unreachable and no cached data available: {exc}"}


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
mcp = FastMCP("monitoring")


@mcp.tool()
async def get_vitals(patient_id: str) -> dict[str, Any]:
    """Return the latest vitals for a patient.

    Fetches current vital signs (heart rate, blood pressure, SpO2, etc.)
    from the synthetic-monitoring backend.
    """
    return await _get(f"/vitals/{patient_id}", patient_id, "vitals")


@mcp.tool()
async def get_vitals_history(patient_id: str) -> dict[str, Any]:
    """Return the vitals history for a patient.

    Fetches historical vital-sign readings from the synthetic-monitoring
    backend.
    """
    return await _get(f"/vitals/{patient_id}/history", patient_id, "vitals_history")


@mcp.tool()
async def list_wards() -> dict[str, Any]:
    """List all wards with their patient counts.

    Returns ward IDs and the number of patients currently assigned to each ward.
    """
    return await _get("/wards", "_wards", "list_wards")


@mcp.tool()
async def list_doctors() -> dict[str, Any]:
    """List all doctors with their patient counts.

    Returns doctor IDs and the number of patients assigned to each doctor.
    """
    return await _get("/doctors", "_doctors", "list_doctors")


@mcp.tool()
async def get_ward_patients(ward_id: str) -> dict[str, Any]:
    """Get all patients in a ward with latest vitals and NEWS deterioration scores.

    Returns patients sorted by NEWS score (highest/most critical first).

    Args:
        ward_id: The ward identifier (e.g. ICU-A, ICU-B, CARDIAC).
    """
    return await _get(f"/ward/{ward_id}/patients", ward_id, "ward_patients")


@mcp.tool()
async def get_doctor_patients(doctor_id: str) -> dict[str, Any]:
    """Get all patients for a doctor with latest vitals and NEWS deterioration scores.

    Returns patients sorted by NEWS score (highest/most critical first).

    Args:
        doctor_id: The doctor identifier (e.g. DR-SMITH, DR-JONES, DR-PATEL).
    """
    return await _get(f"/doctor/{doctor_id}/patients", doctor_id, "doctor_patients")


@mcp.tool()
async def get_patient_events(patient_id: str, hours: int = 24) -> dict[str, Any]:
    """Get clinical alarm/event summaries for a patient.

    Returns events (with condition, heart rate, timestamp) from the last N hours.

    Args:
        patient_id: The patient identifier.
        hours: Number of hours to look back (default 24, max 168).
    """
    return await _get(
        f"/events/{patient_id}?hours={hours}", patient_id, "patient_events"
    )


@mcp.tool()
async def get_event_vitals(patient_id: str, event_id: str) -> dict[str, Any]:
    """Get the full vitals snapshot for a specific clinical event.

    Returns detailed vital signs (HR, SpO2, BP, temp, resp rate, etc.)
    captured at the time of the event.

    Args:
        patient_id: The patient identifier.
        event_id: The event identifier (e.g. event_1001).
    """
    return await _get(
        f"/events/{patient_id}/{event_id}/vitals", patient_id, "event_vitals"
    )


@mcp.tool()
async def get_event_ecg(patient_id: str, event_id: str) -> dict[str, Any]:
    """Get the 12-second 7-lead ECG waveform data for a specific clinical event.

    Returns ECG arrays (2400 samples per lead at 200Hz) for leads
    ECG1, ECG2, ECG3, aVR, aVL, aVF, vVX.

    Args:
        patient_id: The patient identifier.
        event_id: The event identifier (e.g. event_1001).
    """
    return await _get(
        f"/events/{patient_id}/{event_id}/ecg", patient_id, "event_ecg"
    )


@mcp.tool()
async def initiate_code_blue(patient_id: str, location: str) -> dict[str, Any]:
    """CRITICAL: Initiate a Code Blue for a patient.

    This is a safety-critical action. The tool does NOT auto-execute the
    code blue. Instead it returns a confirmation request object so that
    the calling agent or human can explicitly approve before proceeding.

    Args:
        patient_id: The patient identifier.
        location: The physical location (room / bed) for the code team.
    """
    # Fetch current vitals to attach to the confirmation request.
    vitals = await _get(f"/vitals/{patient_id}", patient_id, "vitals")

    return {
        "action": "code_blue",
        "patient_id": patient_id,
        "location": location,
        "status": "awaiting_confirmation",
        "current_vitals": vitals,
        "message": (
            "Code Blue initiation requested. Review patient vitals and "
            "confirm to proceed. This action will NOT execute automatically."
        ),
    }


# ---------------------------------------------------------------------------
# Health-check endpoint (FastAPI, runs in background thread)
# ---------------------------------------------------------------------------
health_app = FastAPI()


@health_app.get("/health")
async def health():
    return {"status": "ok", "service": "mcp-monitoring"}


def _run_health_server() -> None:
    uvicorn.run(health_app, host="0.0.0.0", port=8000, log_level="warning")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Start health endpoint in a daemon thread
    t = threading.Thread(target=_run_health_server, daemon=True)
    t.start()

    # Run MCP stdio server (blocks)
    mcp.run(transport="stdio")
