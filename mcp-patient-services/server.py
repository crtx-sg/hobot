"""MCP tool server for synthetic-patient-services backend.

Provides tools for housekeeping requests, diet orders, ambulance/transport
dispatch, and request status tracking.
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
PATIENT_SERVICES_BASE = os.environ.get(
    "PATIENT_SERVICES_BASE", "http://synthetic-patient-services:8000"
)

# ---------------------------------------------------------------------------
# Degraded-mode cache: last successful response per cache key
# ---------------------------------------------------------------------------
_cache: dict[str, Any] = {}
_cache_ts: dict[str, float] = {}


def _put_cache(key: str, data: Any) -> None:
    _cache[key] = data
    _cache_ts[key] = time.time()


def _get_cached(key: str) -> dict[str, Any] | None:
    if key not in _cache:
        return None
    staleness_secs = round(time.time() - _cache_ts[key], 1)
    return {
        "data": _cache[key],
        "warning": f"DEGRADED MODE: serving cached data ({staleness_secs}s stale). Live backend unreachable.",
    }


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
async def _post(path: str, payload: dict[str, Any], cache_key: str) -> dict[str, Any]:
    """POST to patient-services backend with degraded-mode fallback."""
    url = f"{PATIENT_SERVICES_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            _put_cache(cache_key, data)
            return data
    except Exception as exc:
        cached = _get_cached(cache_key)
        if cached is not None:
            return cached
        return {"error": f"Backend unreachable and no cached data available: {exc}"}


async def _get(path: str, cache_key: str) -> dict[str, Any]:
    """GET from patient-services backend with degraded-mode fallback."""
    url = f"{PATIENT_SERVICES_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            _put_cache(cache_key, data)
            return data
    except Exception as exc:
        cached = _get_cached(cache_key)
        if cached is not None:
            return cached
        return {"error": f"Backend unreachable and no cached data available: {exc}"}


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
mcp = FastMCP("patient-services")


@mcp.tool()
async def request_housekeeping(
    room: str, request_type: str, priority: str = "normal"
) -> dict[str, Any]:
    """Request housekeeping services for a hospital room.

    Submits a housekeeping request (cleaning, linen change, spill cleanup,
    etc.) to the patient-services backend.

    Args:
        room: Room identifier (e.g. "4B", "ICU-3").
        request_type: Type of service requested (e.g. "cleaning",
            "linen_change", "spill_cleanup", "waste_disposal").
        priority: Priority level — "low", "normal", or "urgent".
    """
    payload = {"room": room, "request_type": request_type, "priority": priority}
    cache_key = f"housekeeping:{room}:{request_type}"
    return await _post("/housekeeping", payload, cache_key)


@mcp.tool()
async def order_diet(
    patient_id: str, diet_type: str, meal: str, restrictions: str = ""
) -> dict[str, Any]:
    """Order a diet/meal for a patient.

    Submits a food or diet order to the patient-services backend.

    Args:
        patient_id: The patient identifier.
        diet_type: Diet category (e.g. "regular", "diabetic", "low_sodium",
            "liquid", "NPO").
        meal: Meal period — "breakfast", "lunch", "dinner", or "snack".
        restrictions: Additional dietary restrictions or allergies
            (comma-separated). Empty string if none.
    """
    payload = {
        "patient_id": patient_id,
        "diet_type": diet_type,
        "meal": meal,
        "restrictions": [r.strip() for r in restrictions.split(",") if r.strip()],
    }
    cache_key = f"diet:{patient_id}:{meal}"
    return await _post("/diet-order", payload, cache_key)


@mcp.tool()
async def request_ambulance(
    patient_id: str,
    from_location: str,
    to_location: str,
    transport_type: str = "wheelchair",
    priority: str = "normal",
) -> dict[str, Any]:
    """CRITICAL: Request patient transport or ambulance dispatch.

    This is a safety-critical action. Dispatches transport for a patient
    between locations (intra-hospital wheelchair/stretcher transport or
    ambulance for inter-facility transfer).

    Args:
        patient_id: The patient identifier.
        from_location: Origin location (e.g. "Ward 3B Bed 2", "ER Bay 5").
        to_location: Destination location (e.g. "Radiology", "ICU",
            "City General Hospital").
        transport_type: Mode of transport — "wheelchair", "stretcher",
            "ambulance", or "helicopter".
        priority: Priority level — "normal", "urgent", or "emergency".
    """
    payload = {
        "patient_id": patient_id,
        "from_location": from_location,
        "to_location": to_location,
        "transport_type": transport_type,
        "priority": priority,
    }
    cache_key = f"transport:{patient_id}:{from_location}:{to_location}"
    return await _post("/transport", payload, cache_key)


@mcp.tool()
async def get_request_status(request_id: str) -> dict[str, Any]:
    """Check the status of a previously submitted service request.

    Returns current status (pending, in_progress, completed, cancelled) and
    details for a housekeeping, diet, or transport request.

    Args:
        request_id: The unique request identifier returned when the request
            was originally submitted.
    """
    cache_key = f"status:{request_id}"
    return await _get(f"/request/{request_id}", cache_key)


# ---------------------------------------------------------------------------
# Health-check endpoint (FastAPI, runs in background thread)
# ---------------------------------------------------------------------------
health_app = FastAPI()


@health_app.get("/health")
async def health():
    return {"status": "ok", "service": "mcp-patient-services"}


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
