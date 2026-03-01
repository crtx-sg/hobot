"""
mcp-radiology — MCP tool server for radiology/imaging data.

Connects to Orthanc PACS backend via REST API.
Provides tools: get_studies, get_report, get_latest_study.
Includes degraded_mode with caching and a /health endpoint on port 8000.
"""

import os
import time
import threading
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ORTHANC_BASE = os.environ.get("ORTHANC_BASE", "http://synthetic-radiology:8042")
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "8000"))

logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp":"%(asctime)s","service":"mcp-radiology","level":"%(levelname)s","message":"%(message)s"}',
)
logger = logging.getLogger("mcp-radiology")

# ---------------------------------------------------------------------------
# Cache for degraded mode
# ---------------------------------------------------------------------------

_cache: dict[str, dict] = {}


def _cache_key(prefix: str, *parts: str) -> str:
    return f"{prefix}:" + ":".join(parts)


def _cache_put(key: str, data: dict) -> None:
    _cache[key] = {"data": data, "ts": time.time()}


def _cache_get(key: str) -> Optional[dict]:
    entry = _cache.get(key)
    if entry is None:
        return None
    staleness_s = time.time() - entry["ts"]
    return {
        "data": entry["data"],
        "stale": True,
        "staleness_seconds": round(staleness_s),
    }


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

_http_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(base_url=ORTHANC_BASE, timeout=10.0)
    return _http_client


# ---------------------------------------------------------------------------
# FastAPI health endpoint (runs in background thread)
# ---------------------------------------------------------------------------

health_app = FastAPI()


@health_app.get("/health")
async def health():
    try:
        resp = await _get_client().get("/system")
        backend_ok = resp.status_code == 200
    except Exception:
        backend_ok = False

    status = "healthy" if backend_ok else "degraded"
    return {"status": status, "orthanc": "ok" if backend_ok else "unreachable"}


def _run_health_server() -> None:
    """Run the health endpoint in a daemon thread."""
    uvicorn.run(health_app, host="0.0.0.0", port=HEALTH_PORT, log_level="warning")


# ---------------------------------------------------------------------------
# MCP Tool Server
# ---------------------------------------------------------------------------

mcp = FastMCP("mcp-radiology")


@mcp.tool()
async def get_studies(patient_id: str) -> dict:
    """Get all imaging studies for a patient.

    Returns a list of studies with modality, date, and description.
    Falls back to cached data when Orthanc is unreachable.
    """
    cache_key = _cache_key("studies", patient_id)
    try:
        client = _get_client()
        resp = await client.get(f"/patients/{patient_id}/studies")
        resp.raise_for_status()
        raw_studies = resp.json()

        studies = []
        for study in raw_studies:
            main_tags = study.get("MainDicomTags", {})
            studies.append({
                "study_id": study.get("ID", ""),
                "modality": main_tags.get("ModalitiesInStudy", ""),
                "date": main_tags.get("StudyDate", ""),
                "description": main_tags.get("StudyDescription", ""),
                "accession_number": main_tags.get("AccessionNumber", ""),
            })

        result = {"patient_id": patient_id, "studies": studies, "stale": False}
        _cache_put(cache_key, result)
        return result

    except Exception as exc:
        logger.warning("get_studies failed for %s: %s — trying cache", patient_id, exc)
        cached = _cache_get(cache_key)
        if cached:
            logger.info("Returning cached studies for %s (stale %ds)", patient_id, cached["staleness_seconds"])
            return cached["data"] | {"stale": True, "staleness_seconds": cached["staleness_seconds"]}
        return {"error": f"Orthanc unreachable and no cached data for patient {patient_id}"}


@mcp.tool()
async def get_report(study_id: str) -> dict:
    """Get study details and any attached radiology report for a given study ID.

    Falls back to cached data when Orthanc is unreachable.
    """
    cache_key = _cache_key("report", study_id)
    try:
        client = _get_client()
        resp = await client.get(f"/studies/{study_id}")
        resp.raise_for_status()
        study = resp.json()

        main_tags = study.get("MainDicomTags", {})
        patient_tags = study.get("PatientMainDicomTags", {})

        result = {
            "study_id": study_id,
            "patient_id": patient_tags.get("PatientID", ""),
            "patient_name": patient_tags.get("PatientName", ""),
            "modality": main_tags.get("ModalitiesInStudy", ""),
            "date": main_tags.get("StudyDate", ""),
            "description": main_tags.get("StudyDescription", ""),
            "referring_physician": main_tags.get("ReferringPhysicianName", ""),
            "institution": main_tags.get("InstitutionName", ""),
            "series_count": len(study.get("Series", [])),
            "report": None,
            "stale": False,
        }

        # Orthanc stores attached PDFs / reports as instances.
        # Attempt to find a report in the study's instances.
        for series_id in study.get("Series", []):
            try:
                series_resp = await client.get(f"/series/{series_id}")
                series_resp.raise_for_status()
                series_data = series_resp.json()
                series_modality = series_data.get("MainDicomTags", {}).get("Modality", "")
                # SR = Structured Report, DOC = Document
                if series_modality in ("SR", "DOC"):
                    result["report"] = {
                        "series_id": series_id,
                        "modality": series_modality,
                        "description": series_data.get("MainDicomTags", {}).get("SeriesDescription", ""),
                    }
                    break
            except Exception:
                continue

        _cache_put(cache_key, result)
        return result

    except Exception as exc:
        logger.warning("get_report failed for %s: %s — trying cache", study_id, exc)
        cached = _cache_get(cache_key)
        if cached:
            logger.info("Returning cached report for %s (stale %ds)", study_id, cached["staleness_seconds"])
            return cached["data"] | {"stale": True, "staleness_seconds": cached["staleness_seconds"]}
        return {"error": f"Orthanc unreachable and no cached data for study {study_id}"}


@mcp.tool()
async def get_latest_study(patient_id: str, modality: str | None = None) -> dict:
    """Get the most recent imaging study for a patient.

    Optionally filter by modality (XR, CT, MR, US).
    Falls back to cached data when Orthanc is unreachable.
    """
    cache_key = _cache_key("latest", patient_id, modality or "any")
    try:
        client = _get_client()
        resp = await client.get(f"/patients/{patient_id}/studies")
        resp.raise_for_status()
        raw_studies = resp.json()

        studies = []
        for study in raw_studies:
            main_tags = study.get("MainDicomTags", {})
            study_modality = main_tags.get("ModalitiesInStudy", "")
            study_date = main_tags.get("StudyDate", "")

            if modality and modality.upper() not in study_modality.upper():
                continue

            studies.append({
                "study_id": study.get("ID", ""),
                "modality": study_modality,
                "date": study_date,
                "description": main_tags.get("StudyDescription", ""),
                "accession_number": main_tags.get("AccessionNumber", ""),
            })

        if not studies:
            filter_msg = f" with modality {modality}" if modality else ""
            return {"error": f"No studies found for patient {patient_id}{filter_msg}"}

        # Sort by date descending, pick latest
        studies.sort(key=lambda s: s.get("date", ""), reverse=True)
        latest = studies[0]

        result = {
            "patient_id": patient_id,
            "latest_study": latest,
            "total_matching": len(studies),
            "stale": False,
        }
        _cache_put(cache_key, result)
        return result

    except Exception as exc:
        logger.warning("get_latest_study failed for %s: %s — trying cache", patient_id, exc)
        cached = _cache_get(cache_key)
        if cached:
            logger.info("Returning cached latest study for %s (stale %ds)", patient_id, cached["staleness_seconds"])
            return cached["data"] | {"stale": True, "staleness_seconds": cached["staleness_seconds"]}
        return {"error": f"Orthanc unreachable and no cached data for patient {patient_id}"}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Start health endpoint in background daemon thread
    health_thread = threading.Thread(target=_run_health_server, daemon=True)
    health_thread.start()
    logger.info("Health endpoint started on port %d", HEALTH_PORT)

    # Run MCP server on stdio (blocking)
    logger.info("Starting mcp-radiology MCP server (stdio transport)")
    mcp.run(transport="stdio")
