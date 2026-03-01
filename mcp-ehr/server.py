"""
mcp-ehr: MCP tool server for EHR (Electronic Health Records) via FHIR R4.

Connects to a HAPI FHIR backend and exposes patient demographics, medications,
allergies, orders, and order-writing as MCP tools over stdio transport.

Includes a /health HTTP endpoint on port 8000 served in a background thread.
Implements degraded_mode: when the FHIR server is unreachable, returns last
known good results with a staleness warning.
"""

import json
import logging
import os
import threading
import datetime
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FHIR_BASE = os.environ.get("FHIR_BASE", "http://synthetic-ehr:8080/fhir")
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "8000"))

logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp":"%(asctime)s","service":"mcp-ehr","level":"%(levelname)s","message":"%(message)s"}',
)
logger = logging.getLogger("mcp-ehr")

# ---------------------------------------------------------------------------
# In-memory cache for degraded mode
# ---------------------------------------------------------------------------

_cache: dict[str, dict[str, Any]] = {}


def _cache_key(tool: str, patient_id: str) -> str:
    return f"{tool}:{patient_id}"


def _cache_put(tool: str, patient_id: str, data: Any) -> None:
    _cache[_cache_key(tool, patient_id)] = {
        "data": data,
        "cached_at": datetime.datetime.utcnow().isoformat() + "Z",
    }


def _cache_get(tool: str, patient_id: str) -> dict[str, Any] | None:
    return _cache.get(_cache_key(tool, patient_id))


def _degraded_response(tool: str, patient_id: str, error: str) -> dict:
    """Return a degraded-mode response with cached data if available."""
    cached = _cache_get(tool, patient_id)
    return {
        "degraded": True,
        "error": error,
        "cached": cached,
    }


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

_http_client: httpx.AsyncClient | None = None


async def _client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    return _http_client


async def _fhir_get(path: str) -> httpx.Response:
    client = await _client()
    url = f"{FHIR_BASE}/{path}"
    logger.info("FHIR GET %s", url)
    return await client.get(url, headers={"Accept": "application/fhir+json"})


async def _fhir_post(path: str, body: dict) -> httpx.Response:
    client = await _client()
    url = f"{FHIR_BASE}/{path}"
    logger.info("FHIR POST %s", url)
    return await client.post(
        url,
        json=body,
        headers={
            "Content-Type": "application/fhir+json",
            "Accept": "application/fhir+json",
        },
    )


# ---------------------------------------------------------------------------
# MCP Tool Server
# ---------------------------------------------------------------------------

mcp = FastMCP("mcp-ehr")


@mcp.tool()
async def get_patient(patient_id: str) -> dict:
    """Look up patient demographics by identifier (e.g. UHID).

    Returns name, gender, date of birth, address, and identifiers from the EHR.
    """
    try:
        resp = await _fhir_get(f"Patient?identifier={patient_id}")
        resp.raise_for_status()
        bundle = resp.json()

        entries = bundle.get("entry", [])
        if not entries:
            return {"error": f"No patient found with identifier {patient_id}"}

        resource = entries[0].get("resource", {})
        names = resource.get("name", [])
        name_display = ""
        if names:
            given = " ".join(names[0].get("given", []))
            family = names[0].get("family", "")
            name_display = f"{given} {family}".strip()

        result = {
            "patient_id": patient_id,
            "fhir_id": resource.get("id"),
            "name": name_display,
            "gender": resource.get("gender"),
            "birth_date": resource.get("birthDate"),
            "identifier": [
                {"system": ident.get("system"), "value": ident.get("value")}
                for ident in resource.get("identifier", [])
            ],
            "address": resource.get("address", []),
        }
        _cache_put("get_patient", patient_id, result)
        return result

    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.error("get_patient failed: %s", exc)
        return _degraded_response("get_patient", patient_id, str(exc))


@mcp.tool()
async def get_medications(patient_id: str) -> dict:
    """Retrieve active medication requests for a patient.

    Returns a list of medications including drug name, dosage, status, and dates.
    """
    try:
        resp = await _fhir_get(f"MedicationRequest?patient={patient_id}")
        resp.raise_for_status()
        bundle = resp.json()

        medications = []
        for entry in bundle.get("entry", []):
            res = entry.get("resource", {})
            med_concept = res.get("medicationCodeableConcept", {})
            med_name = med_concept.get("text", "")
            if not med_name and med_concept.get("coding"):
                med_name = med_concept["coding"][0].get("display", "")
            medications.append({
                "id": res.get("id"),
                "medication": med_name,
                "status": res.get("status"),
                "intent": res.get("intent"),
                "authored_on": res.get("authoredOn"),
                "dosage_instruction": res.get("dosageInstruction", []),
            })

        result = {"patient_id": patient_id, "medications": medications}
        _cache_put("get_medications", patient_id, result)
        return result

    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.error("get_medications failed: %s", exc)
        return _degraded_response("get_medications", patient_id, str(exc))


@mcp.tool()
async def get_allergies(patient_id: str) -> dict:
    """Retrieve allergy and intolerance records for a patient.

    Returns known allergies including substance, reaction, severity, and status.
    """
    try:
        resp = await _fhir_get(f"AllergyIntolerance?patient={patient_id}")
        resp.raise_for_status()
        bundle = resp.json()

        allergies = []
        for entry in bundle.get("entry", []):
            res = entry.get("resource", {})
            code = res.get("code", {})
            substance = code.get("text", "")
            if not substance and code.get("coding"):
                substance = code["coding"][0].get("display", "")

            reactions = []
            for rxn in res.get("reaction", []):
                manifestations = []
                for m in rxn.get("manifestation", []):
                    manifestations.append(m.get("text", m.get("coding", [{}])[0].get("display", "")))
                reactions.append({
                    "manifestation": manifestations,
                    "severity": rxn.get("severity"),
                })

            allergies.append({
                "id": res.get("id"),
                "substance": substance,
                "clinical_status": res.get("clinicalStatus", {}).get("coding", [{}])[0].get("code"),
                "verification_status": res.get("verificationStatus", {}).get("coding", [{}])[0].get("code"),
                "category": res.get("category", []),
                "criticality": res.get("criticality"),
                "reactions": reactions,
            })

        result = {"patient_id": patient_id, "allergies": allergies}
        _cache_put("get_allergies", patient_id, result)
        return result

    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.error("get_allergies failed: %s", exc)
        return _degraded_response("get_allergies", patient_id, str(exc))


@mcp.tool()
async def get_orders(patient_id: str) -> dict:
    """Retrieve service requests (orders) for a patient.

    Returns lab orders, imaging orders, and other service requests with status.
    """
    try:
        resp = await _fhir_get(f"ServiceRequest?patient={patient_id}")
        resp.raise_for_status()
        bundle = resp.json()

        orders = []
        for entry in bundle.get("entry", []):
            res = entry.get("resource", {})
            code = res.get("code", {})
            order_name = code.get("text", "")
            if not order_name and code.get("coding"):
                order_name = code["coding"][0].get("display", "")
            orders.append({
                "id": res.get("id"),
                "order": order_name,
                "status": res.get("status"),
                "intent": res.get("intent"),
                "priority": res.get("priority"),
                "authored_on": res.get("authoredOn"),
                "category": [
                    cat.get("text", cat.get("coding", [{}])[0].get("display", ""))
                    for cat in res.get("category", [])
                ],
            })

        result = {"patient_id": patient_id, "orders": orders}
        _cache_put("get_orders", patient_id, result)
        return result

    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.error("get_orders failed: %s", exc)
        return _degraded_response("get_orders", patient_id, str(exc))


@mcp.tool()
async def write_order(patient_id: str, order_type: str, details: str) -> dict:
    """CRITICAL: Write a new service request (order) to the EHR for a patient.

    This tool creates a new ServiceRequest in the FHIR server. It is classified
    as a critical action and requires clinician confirmation before execution.

    Args:
        patient_id: The patient's FHIR resource ID.
        order_type: Type of order (e.g. "laboratory", "imaging", "procedure").
        details: Human-readable description of what is being ordered.
    """
    service_request = {
        "resourceType": "ServiceRequest",
        "status": "active",
        "intent": "order",
        "category": [
            {
                "coding": [
                    {
                        "system": "http://snomed.info/sct",
                        "display": order_type,
                    }
                ],
                "text": order_type,
            }
        ],
        "code": {
            "text": details,
        },
        "subject": {
            "reference": f"Patient/{patient_id}",
        },
        "authoredOn": datetime.datetime.utcnow().isoformat() + "Z",
    }

    try:
        resp = await _fhir_post("ServiceRequest", service_request)
        resp.raise_for_status()
        created = resp.json()

        result = {
            "status": "created",
            "order_id": created.get("id"),
            "patient_id": patient_id,
            "order_type": order_type,
            "details": details,
        }
        logger.info("Order created: %s", json.dumps(result))
        return result

    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.error("write_order failed: %s", exc)
        return {
            "degraded": True,
            "error": str(exc),
            "message": "Failed to write order. The EHR may be unavailable. Please retry or enter the order manually.",
        }


# ---------------------------------------------------------------------------
# Health endpoint (FastAPI, served in background thread)
# ---------------------------------------------------------------------------

health_app = FastAPI(title="mcp-ehr-health", docs_url=None, redoc_url=None)


@health_app.get("/health")
async def health():
    """Check FHIR server connectivity."""
    try:
        client = await _client()
        resp = await client.get(
            f"{FHIR_BASE}/metadata",
            headers={"Accept": "application/fhir+json"},
            timeout=5.0,
        )
        fhir_ok = resp.status_code == 200
    except Exception:
        fhir_ok = False

    status = "healthy" if fhir_ok else "degraded"
    return {
        "service": "mcp-ehr",
        "status": status,
        "fhir_backend": "ok" if fhir_ok else "unreachable",
        "fhir_base": FHIR_BASE,
    }


def _run_health_server() -> None:
    """Run the health endpoint in a background thread."""
    uvicorn.run(
        health_app,
        host="0.0.0.0",
        port=HEALTH_PORT,
        log_level="warning",
    )


# ---------------------------------------------------------------------------
# Main: start health server in background, then run MCP stdio server
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("Starting mcp-ehr health endpoint on port %d", HEALTH_PORT)
    health_thread = threading.Thread(target=_run_health_server, daemon=True)
    health_thread.start()

    logger.info("Starting mcp-ehr MCP stdio server")
    mcp.run(transport="stdio")
