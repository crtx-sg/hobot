"""Specialized LLM analyzers — intercept (auto-fire) and tool (LLM-dispatched)."""

import asyncio
import base64
import json
import logging
import os

import httpx

import clinical_memory
import phi
from analyzer_prompts import ANALYZER_PROMPTS
from providers import get_provider

logger = logging.getLogger("clinibot.analyzers")

# ---------------------------------------------------------------------------
# Config loaded from config.json at startup
# ---------------------------------------------------------------------------

_intercept_config: dict[str, dict] = {}
_tool_config: dict[str, dict] = {}
_defaults: dict = {}


def load_analyzer_config(config_path: str) -> None:
    """Load intercept + tool analyzer configs from config.json."""
    global _intercept_config, _tool_config, _defaults
    if not os.path.exists(config_path):
        return
    with open(config_path) as f:
        data = json.load(f)
    analyzers = data.get("analyzers", {})
    _intercept_config = analyzers.get("intercept", {})
    _tool_config = analyzers.get("tools", {})
    _defaults = analyzers.get("defaults", {})
    logger.info(
        "Loaded analyzers: %d intercept, %d tools",
        len(_intercept_config), len(_tool_config),
    )


# ---------------------------------------------------------------------------
# Intercept (auto-fire, self-contained)
# ---------------------------------------------------------------------------

async def maybe_analyze(tool_name: str, tool_result: dict, params: dict, session) -> dict:
    """Check intercept config. If configured, run analyzer and add _analysis. Else passthrough."""
    cfg = _intercept_config.get(tool_name)
    if not cfg:
        return tool_result
    timeout = _defaults.get("timeout", 15)
    try:
        result = await asyncio.wait_for(
            _run_intercept(tool_name, tool_result, cfg, session),
            timeout=timeout,
        )
        return result
    except asyncio.TimeoutError:
        logger.warning("Intercept timeout for %s after %ds", tool_name, timeout)
        return tool_result
    except Exception as exc:
        logger.warning("Intercept failed for %s: %s", tool_name, exc)
        return tool_result


async def _run_intercept(tool_name: str, tool_result: dict, cfg: dict, session) -> dict:
    """Build prompt from template + data, call provider, add _analysis to result."""
    provider_name = cfg.get("provider", _defaults.get("fallback", "gemini"))
    prompt_key = cfg.get("prompt_template", "")
    domain = cfg.get("domain", "")
    template = ANALYZER_PROMPTS.get(prompt_key, "")
    if not template:
        return tool_result

    data_str = json.dumps(tool_result, indent=2)
    prompt = template.format(data=data_str, context="")

    provider = get_provider(provider_name)
    if not provider or not await provider.is_available():
        logger.warning("Intercept provider '%s' unavailable", provider_name)
        return tool_result

    # PHI redaction for non-phi-safe providers
    phi_mapping = None
    if not provider.config.phi_safe:
        prompt, phi_mapping = phi.redact(prompt)

    result = await provider.chat([{"role": "user", "content": prompt}])
    if not result or not result.content:
        return tool_result

    interpretation = result.content
    if phi_mapping:
        interpretation = phi.restore(interpretation, phi_mapping)

    analysis = {
        "status": "success",
        "domain": domain,
        "interpretation": interpretation,
        "findings": [],
        "context_used": [],
        "context_summary": "",
        "analyzer_provider": provider_name,
        "analyzer_model": provider.config.model,
    }
    return {**tool_result, "_analysis": analysis}


# ---------------------------------------------------------------------------
# Tool analyzers (LLM-dispatched, context-dependent)
# ---------------------------------------------------------------------------

async def run_analyzer_tool(analyzer_name: str, params: dict, session) -> dict:
    """Called when LLM dispatches analyze_lab_results, analyze_ecg, etc."""
    cfg = _tool_config.get(analyzer_name)
    if not cfg:
        return {"status": "error", "interpretation": f"Unknown analyzer: {analyzer_name}"}

    patient_id = params.get("patient_id", "")
    if not patient_id:
        return {"status": "error", "interpretation": "patient_id is required"}

    tenant_id = getattr(session, "tenant_id", "default")
    provider_name = cfg.get("provider", _defaults.get("fallback", "gemini"))
    prompt_key = cfg.get("prompt_template", "")
    domain = cfg.get("domain", "")
    context_types = cfg.get("context_types", [])
    source_fact_types = cfg.get("source_fact_types", [])
    preprocessor_name = cfg.get("preprocessor")
    input_type = cfg.get("input_type")

    timeout = _defaults.get("timeout", 15)

    try:
        result = await asyncio.wait_for(
            _run_tool_analyzer(
                analyzer_name, patient_id, tenant_id, provider_name,
                prompt_key, domain, context_types, source_fact_types,
                preprocessor_name, input_type, params, session,
            ),
            timeout=timeout,
        )
        return result
    except asyncio.TimeoutError:
        return {
            "status": "error",
            "status_reason": "timeout",
            "domain": domain,
            "interpretation": "Analysis timed out",
            "findings": [],
            "context_used": [],
            "context_summary": "",
            "analyzer_provider": provider_name,
            "analyzer_model": "",
        }


async def _run_tool_analyzer(
    analyzer_name, patient_id, tenant_id, provider_name,
    prompt_key, domain, context_types, source_fact_types,
    preprocessor_name, input_type, params, session,
) -> dict:
    """Core logic for tool analyzer execution."""
    provider = get_provider(provider_name)
    if not provider or not await provider.is_available():
        return {
            "status": "error",
            "status_reason": "provider_error",
            "domain": domain,
            "interpretation": f"Provider '{provider_name}' unavailable",
            "findings": [],
            "context_used": [],
            "context_summary": "",
            "analyzer_provider": provider_name,
            "analyzer_model": "",
        }

    # Fetch patient context
    context_summary, context_found = await _build_patient_context(
        patient_id, tenant_id, context_types
    )

    # Determine status from context completeness
    status_reasons = []
    missing_context = set(context_types) - set(context_found)
    if missing_context:
        status_reasons.append("incomplete_context")

    # Fetch source data or image
    data_str = ""
    if input_type == "image_wado":
        # Image-based analyzer
        study_id = params.get("study_id", "")
        if not study_id:
            return {
                "status": "error",
                "status_reason": "no_source_data",
                "domain": domain,
                "interpretation": "study_id is required for image analysis",
                "findings": [],
                "context_used": list(context_found),
                "context_summary": context_summary,
                "analyzer_provider": provider_name,
                "analyzer_model": provider.config.model,
            }
        image_data = await _fetch_wado_image(study_id)
        if not image_data:
            return {
                "status": "error",
                "status_reason": "image_fetch_failed",
                "domain": domain,
                "interpretation": "Failed to fetch image from Orthanc",
                "findings": [],
                "context_used": list(context_found),
                "context_summary": context_summary,
                "analyzer_provider": provider_name,
                "analyzer_model": provider.config.model,
            }
        image_b64, media_type = image_data
    else:
        # Data-based analyzer — fetch from clinical_memory
        source_data = await _fetch_source_data(patient_id, tenant_id, source_fact_types)
        if not source_data:
            return {
                "status": "error",
                "status_reason": "no_source_data",
                "domain": domain,
                "interpretation": "No data found to analyze. Fetch the data first.",
                "findings": [],
                "context_used": list(context_found),
                "context_summary": context_summary,
                "analyzer_provider": provider_name,
                "analyzer_model": provider.config.model,
            }

        # Apply preprocessor if configured
        capability = "full"
        if preprocessor_name:
            preprocessor = _PREPROCESSORS.get(preprocessor_name)
            if preprocessor:
                source_data, capability = preprocessor(source_data)

        if capability == "limited":
            status_reasons.append("limited_capability")

        data_str = json.dumps(source_data, indent=2)

    # Build prompt
    template = ANALYZER_PROMPTS.get(prompt_key, "")
    if not template:
        return {
            "status": "error",
            "status_reason": "provider_error",
            "domain": domain,
            "interpretation": f"No prompt template for '{prompt_key}'",
            "findings": [],
            "context_used": list(context_found),
            "context_summary": context_summary,
            "analyzer_provider": provider_name,
            "analyzer_model": provider.config.model,
        }

    prompt = template.format(data=data_str, context=context_summary)

    # PHI redaction
    phi_mapping = None
    if not provider.config.phi_safe:
        prompt, phi_mapping = phi.redact(prompt)
        if input_type != "image_wado":
            context_summary_redacted, _ = phi.redact(context_summary)
        else:
            context_summary_redacted = context_summary
    else:
        context_summary_redacted = context_summary

    # Call provider
    if input_type == "image_wado":
        messages = _build_vision_message(provider_name, prompt, image_b64, media_type)
    else:
        messages = [{"role": "user", "content": prompt}]

    chat_result = await provider.chat(messages)
    if not chat_result or not chat_result.content:
        return {
            "status": "error",
            "status_reason": "provider_error",
            "domain": domain,
            "interpretation": "Provider returned no content",
            "findings": [],
            "context_used": list(context_found),
            "context_summary": context_summary,
            "analyzer_provider": provider_name,
            "analyzer_model": provider.config.model,
        }

    interpretation = chat_result.content
    if phi_mapping:
        interpretation = phi.restore(interpretation, phi_mapping)

    # Build final status
    status = "success" if not status_reasons else "partial"
    status_reason = ",".join(status_reasons) if status_reasons else None

    result = {
        "status": status,
        "domain": domain,
        "interpretation": interpretation,
        "findings": [],
        "context_used": list(context_found),
        "context_summary": context_summary,
        "analyzer_provider": provider_name,
        "analyzer_model": provider.config.model,
    }
    if status_reason:
        result["status_reason"] = status_reason
    return result


# ---------------------------------------------------------------------------
# Patient context builder
# ---------------------------------------------------------------------------

async def _build_patient_context(
    patient_id: str, tenant_id: str, context_types: list[str]
) -> tuple[str, list[str]]:
    """Fetch facts from clinical_memory filtered by context_types.
    Returns (summary, fact_types_found)."""
    if not context_types:
        return "", []

    found_types: set[str] = set()
    context_parts: list[str] = []

    for ct in context_types:
        facts = await clinical_memory.get_facts(patient_id, tenant_id, fact_type=ct, limit=5)
        if facts:
            found_types.add(ct)
            for f in facts:
                context_parts.append(json.dumps(f["fact_data"]))

    summary = "; ".join(context_parts) if context_parts else ""
    # Truncate long summaries
    if len(summary) > 1000:
        summary = summary[:1000] + "..."
    return summary, list(found_types)


# ---------------------------------------------------------------------------
# Source data fetcher
# ---------------------------------------------------------------------------

async def _fetch_source_data(
    patient_id: str, tenant_id: str, source_fact_types: list[str]
) -> dict | None:
    """Fetch most recent facts matching source_fact_types. Return combined data or None."""
    if not source_fact_types:
        return None

    combined: dict = {}
    for ft in source_fact_types:
        facts = await clinical_memory.get_facts(patient_id, tenant_id, fact_type=ft, limit=10)
        if facts:
            if len(facts) == 1:
                combined[ft] = facts[0]["fact_data"]
            else:
                combined[ft] = [f["fact_data"] for f in facts]

    return combined if combined else None


# ---------------------------------------------------------------------------
# Preprocessors
# ---------------------------------------------------------------------------

def _ecg_stub_preprocessor(data: dict) -> tuple[dict, str]:
    """Extract ECG metadata, strip waveform arrays. Returns (metadata, 'limited')."""
    ecg_data = data.get("ecg", data)
    if isinstance(ecg_data, list):
        ecg_data = ecg_data[0] if ecg_data else {}

    meta = {}
    for k, v in ecg_data.items():
        if k not in ("leads", "waveform", "samples"):
            meta[k] = v
    if "leads" in ecg_data and isinstance(ecg_data["leads"], dict):
        meta["lead_names"] = list(ecg_data["leads"].keys())
        meta["lead_count"] = len(ecg_data["leads"])
    elif "lead_names" in ecg_data:
        meta["lead_names"] = ecg_data["lead_names"]
    if "lead_count" not in meta and "lead_names" in meta:
        meta["lead_count"] = len(meta["lead_names"])

    return {"ecg": meta}, "limited"


_PREPROCESSORS: dict[str, callable] = {
    "ecg_stub": _ecg_stub_preprocessor,
}


# ---------------------------------------------------------------------------
# Image handling (WADO / Orthanc)
# ---------------------------------------------------------------------------

ORTHANC_BASE = os.environ.get("ORTHANC_BASE", os.environ.get("RADIOLOGY_BASE", "http://synthetic-radiology:8042"))


async def _fetch_wado_image(study_id: str) -> tuple[str, str] | None:
    """Fetch a preview image from Orthanc for the given study.
    Returns (base64_data, media_type) or None."""
    auth = ("orthanc", "orthanc")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Get series for study
            resp = await client.get(
                f"{ORTHANC_BASE}/studies/{study_id}/series",
                auth=auth,
            )
            if resp.status_code != 200:
                logger.warning("Orthanc studies/%s/series -> %d", study_id, resp.status_code)
                return None
            series = resp.json()
            if not series:
                return None
            series_id = series[0]["ID"] if isinstance(series[0], dict) else series[0]

            # Get instances for first series
            resp = await client.get(
                f"{ORTHANC_BASE}/series/{series_id}/instances",
                auth=auth,
            )
            if resp.status_code != 200:
                return None
            instances = resp.json()
            if not instances:
                return None
            instance_id = instances[0]["ID"] if isinstance(instances[0], dict) else instances[0]

            # Get preview PNG
            resp = await client.get(
                f"{ORTHANC_BASE}/instances/{instance_id}/preview",
                auth=auth,
            )
            if resp.status_code != 200:
                return None

            image_bytes = resp.content
            b64 = base64.b64encode(image_bytes).decode("ascii")
            return b64, "image/png"
    except Exception as exc:
        logger.warning("Failed to fetch WADO image for study %s: %s", study_id, exc)
        return None


def _build_vision_message(
    provider_name: str, prompt: str, image_b64: str, media_type: str
) -> list[dict]:
    """Build messages list with image for vision-capable providers."""
    data_uri = f"data:{media_type};base64,{image_b64}"

    if "anthropic" in provider_name.lower():
        # Anthropic native image format
        return [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image", "source": {
                "type": "base64",
                "media_type": media_type,
                "data": image_b64,
            }},
        ]}]

    # OpenAI / Gemini compatible format
    return [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": data_uri}},
    ]}]


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

ANALYZER_TOOL_DESCRIPTIONS = {
    "analyze_lab_results": "Interpret lab results in context of patient history, medications, and conditions. Call after fetching labs and patient context.",
    "analyze_ecg": "Interpret ECG data in context of patient history and medications. Call after fetching ECG and patient context.",
    "analyze_vitals": "Assess vital signs against patient-specific targets, compute NEWS2 context, and evaluate trends. Call after fetching vitals and patient context.",
    "analyze_radiology_image": "Interpret a medical image (X-ray, CT, etc.) using a vision model with patient context. Requires study_id.",
}

# Parameter schemas per analyzer tool
_ANALYZER_PARAMS = {
    "analyze_lab_results": {
        "type": "object",
        "properties": {
            "patient_id": {"type": "string"},
        },
        "required": ["patient_id"],
    },
    "analyze_ecg": {
        "type": "object",
        "properties": {
            "patient_id": {"type": "string"},
        },
        "required": ["patient_id"],
    },
    "analyze_vitals": {
        "type": "object",
        "properties": {
            "patient_id": {"type": "string"},
        },
        "required": ["patient_id"],
    },
    "analyze_radiology_image": {
        "type": "object",
        "properties": {
            "patient_id": {"type": "string"},
            "study_id": {"type": "string"},
        },
        "required": ["patient_id", "study_id"],
    },
}


def get_analyzer_tool_definitions() -> list[dict]:
    """Return tool definitions for all configured analyzer tools."""
    defs = []
    for name, cfg in _tool_config.items():
        desc = ANALYZER_TOOL_DESCRIPTIONS.get(name, name)
        params = _ANALYZER_PARAMS.get(name, {"type": "object", "properties": {}})
        defs.append({
            "name": name,
            "description": desc,
            "parameters": params,
        })
    return defs
