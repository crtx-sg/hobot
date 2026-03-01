"""MCP tool server for the Laboratory Information System (LIS)."""

import json
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

LIS_BASE = os.environ.get("LIS_BASE", "http://synthetic-lis:8000")

mcp = FastMCP("mcp-lis")

# ---------------------------------------------------------------------------
# Degraded-mode cache
# ---------------------------------------------------------------------------

_cache: dict[str, dict] = {}


def _cache_key(endpoint: str) -> str:
    return endpoint


def _cache_put(key: str, data: dict) -> None:
    _cache[key] = {
        "data": data,
        "cached_at": datetime.now(timezone.utc).isoformat(),
    }


def _cache_get(key: str) -> dict | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    return {
        **entry["data"],
        "_degraded": True,
        "_stale_warning": f"Backend unavailable. Returning cached result from {entry['cached_at']}.",
    }


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def _get(path: str) -> dict:
    """GET from the LIS backend with degraded-mode fallback."""
    key = _cache_key(f"GET:{path}")
    try:
        async with httpx.AsyncClient(base_url=LIS_BASE, timeout=10.0) as client:
            resp = await client.get(path)
            resp.raise_for_status()
            data = resp.json()
            _cache_put(key, data)
            return data
    except Exception as exc:
        cached = _cache_get(key)
        if cached is not None:
            return cached
        return {"error": str(exc)}


async def _post(path: str, body: dict) -> dict:
    """POST to the LIS backend with degraded-mode fallback."""
    key = _cache_key(f"POST:{path}:{json.dumps(body, sort_keys=True)}")
    try:
        async with httpx.AsyncClient(base_url=LIS_BASE, timeout=10.0) as client:
            resp = await client.post(path, json=body)
            resp.raise_for_status()
            data = resp.json()
            _cache_put(key, data)
            return data
    except Exception as exc:
        cached = _cache_get(key)
        if cached is not None:
            return cached
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_lab_results(patient_id: str) -> str:
    """Retrieve all lab results for a patient by patient ID."""
    data = await _get(f"/labs/{patient_id}")
    return json.dumps(data, indent=2)


@mcp.tool()
async def get_lab_order(order_id: str) -> str:
    """Retrieve a specific lab order by order ID."""
    data = await _get(f"/lab/{order_id}")
    return json.dumps(data, indent=2)


@mcp.tool()
async def order_lab(patient_id: str, test_type: str, priority: str = "routine") -> str:
    """Place a new lab order for a patient.

    Args:
        patient_id: The patient identifier (e.g. P001).
        test_type: The type of lab test (e.g. CBC, BMP, LFT).
        priority: Order priority — routine, urgent, or stat. Defaults to routine.
    """
    body = {"patient_id": patient_id, "test_type": test_type, "priority": priority}
    data = await _post("/lab/order", body)
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# Health endpoint (FastAPI, runs in background thread)
# ---------------------------------------------------------------------------

health_app = FastAPI(title="mcp-lis health", version="0.1.0")


@health_app.get("/health")
def health():
    return {"status": "ok", "service": "mcp-lis"}


def _run_health_server() -> None:
    uvicorn.run(health_app, host="0.0.0.0", port=8000, log_level="warning")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Start the health endpoint in a background daemon thread
    t = threading.Thread(target=_run_health_server, daemon=True)
    t.start()

    # Run MCP stdio transport as the main loop
    mcp.run(transport="stdio")
