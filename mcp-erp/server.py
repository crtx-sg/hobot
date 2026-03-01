"""MCP tool server for the synthetic ERP backend."""

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

ERP_BASE = os.environ.get("ERP_BASE", "http://synthetic-erp:8000")

# ---------------------------------------------------------------------------
# Degraded-mode cache
# ---------------------------------------------------------------------------

_cache: dict[str, dict[str, Any]] = {}
# Each entry: {"data": <json>, "ts": <epoch float>}


def _put_cache(key: str, data: Any) -> None:
    _cache[key] = {"data": data, "ts": time.time()}


def _get_cache(key: str) -> dict[str, Any] | None:
    return _cache.get(key)


async def _erp_get(path: str, cache_key: str | None = None) -> dict[str, Any]:
    """GET helper with degraded-mode support.

    On success the response is cached.  On failure the last cached response is
    returned together with a staleness warning.
    """
    key = cache_key or path
    url = f"{ERP_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            _put_cache(key, data)
            return data
    except Exception as exc:
        cached = _get_cache(key)
        if cached is not None:
            age_s = round(time.time() - cached["ts"], 1)
            return {
                "degraded_mode": True,
                "staleness_seconds": age_s,
                "warning": f"ERP backend unreachable ({exc}). Returning cached data ({age_s}s old).",
                "data": cached["data"],
            }
        raise RuntimeError(
            f"ERP backend unreachable and no cached data available for {path}: {exc}"
        ) from exc


async def _erp_post(path: str, payload: dict[str, Any], cache_key: str | None = None) -> dict[str, Any]:
    """POST helper with degraded-mode support for writes."""
    key = cache_key or path
    url = f"{ERP_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            _put_cache(key, data)
            return data
    except Exception as exc:
        cached = _get_cache(key)
        if cached is not None:
            age_s = round(time.time() - cached["ts"], 1)
            return {
                "degraded_mode": True,
                "staleness_seconds": age_s,
                "warning": f"ERP backend unreachable ({exc}). Returning cached data ({age_s}s old).",
                "data": cached["data"],
            }
        raise RuntimeError(
            f"ERP backend unreachable and no cached data available for {path}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("mcp-erp")


@mcp.tool()
async def get_inventory() -> dict[str, Any]:
    """Return the current inventory snapshot from the ERP system."""
    return await _erp_get("/inventory")


@mcp.tool()
async def get_equipment_status(equipment_id: str) -> dict[str, Any]:
    """Return the status of a specific piece of equipment.

    Args:
        equipment_id: Unique identifier for the equipment.
    """
    return await _erp_get(f"/equipment/{equipment_id}")


@mcp.tool()
async def place_supply_order(
    item_id: str,
    quantity: int,
    department: str,
    priority: str = "normal",
) -> dict[str, Any]:
    """Place a supply order in the ERP system.

    Args:
        item_id: Identifier for the item to order.
        quantity: Number of units to order.
        department: Department requesting the supplies.
        priority: Order priority — "normal" or "urgent".
    """
    payload = {
        "item_id": item_id,
        "quantity": quantity,
        "department": department,
        "priority": priority,
    }
    return await _erp_post("/supply-order", payload, cache_key=f"supply-order:{item_id}:{department}")


# ---------------------------------------------------------------------------
# Health endpoint (FastAPI on port 8000 in a background thread)
# ---------------------------------------------------------------------------

health_app = FastAPI()


@health_app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "mcp-erp"}


def _run_health_server() -> None:
    uvicorn.run(health_app, host="0.0.0.0", port=8000, log_level="warning")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Start the health endpoint in a daemon thread so it doesn't block MCP.
    t = threading.Thread(target=_run_health_server, daemon=True)
    t.start()

    # Run MCP server over stdio (blocking).
    mcp.run(transport="stdio")
