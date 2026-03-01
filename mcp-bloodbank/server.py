"""MCP Blood Bank tool server.

Exposes blood availability, crossmatch ordering, and crossmatch status
tools over stdio transport. A FastAPI /health endpoint runs in a
background thread on port 8000.
"""

import os
import threading
import time
from datetime import datetime, timezone

import httpx
import uvicorn
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BLOODBANK_BASE = os.environ.get("BLOODBANK_BASE", "http://synthetic-bloodbank:8000")

# ---------------------------------------------------------------------------
# Degraded-mode cache
# ---------------------------------------------------------------------------

_cache: dict[str, dict] = {}


def _cache_set(key: str, data: dict) -> None:
    _cache[key] = {
        "data": data,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _cache_get(key: str) -> dict | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    return {
        "data": entry["data"],
        "cached_at": entry["timestamp"],
        "warning": "DEGRADED MODE: backend unreachable, returning cached data",
    }


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def _get(path: str, cache_key: str | None = None) -> dict:
    """GET request with degraded-mode fallback."""
    key = cache_key or path
    url = f"{BLOODBANK_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            _cache_set(key, data)
            return data
    except Exception:
        cached = _cache_get(key)
        if cached is not None:
            return cached
        raise


async def _post(path: str, body: dict, cache_key: str | None = None) -> dict:
    """POST request with degraded-mode fallback."""
    key = cache_key or path
    url = f"{BLOODBANK_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
            _cache_set(key, data)
            return data
    except Exception:
        cached = _cache_get(key)
        if cached is not None:
            return cached
        raise


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("bloodbank")


@mcp.tool()
async def get_blood_availability() -> dict:
    """Return current blood inventory / availability from the blood bank."""
    return await _get("/availability")


@mcp.tool()
async def order_blood_crossmatch(
    patient_id: str,
    blood_type: str,
    units: int,
    priority: str = "routine",
) -> dict:
    """CRITICAL – Order a blood crossmatch for a patient.

    Args:
        patient_id: The patient identifier.
        blood_type: Required blood type (e.g. "O+", "A-").
        units: Number of units to crossmatch.
        priority: "routine" | "urgent" | "stat". Defaults to "routine".
    """
    return await _post(
        "/crossmatch",
        {
            "patient_id": patient_id,
            "blood_type": blood_type,
            "units": units,
            "priority": priority,
        },
        cache_key=f"crossmatch:{patient_id}:{blood_type}",
    )


@mcp.tool()
async def get_crossmatch_status(request_id: str) -> dict:
    """Check the status of an existing crossmatch request.

    Args:
        request_id: The crossmatch request identifier.
    """
    return await _get(f"/crossmatch/{request_id}")


# ---------------------------------------------------------------------------
# Health endpoint (FastAPI in background thread)
# ---------------------------------------------------------------------------

health_app = FastAPI()
_start_time = time.time()


@health_app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "mcp-bloodbank",
        "uptime_seconds": round(time.time() - _start_time, 1),
    }


def _run_health_server() -> None:
    uvicorn.run(health_app, host="0.0.0.0", port=8000, log_level="warning")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    health_thread = threading.Thread(target=_run_health_server, daemon=True)
    health_thread.start()
    mcp.run(transport="stdio")
