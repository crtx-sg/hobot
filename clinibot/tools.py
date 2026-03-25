"""Tool registry, MCP backend dispatch, and critical-tool confirmation gate."""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid

from typing import Any

import httpx

from audit import log_action, log_escalation

logger = logging.getLogger("clinibot.tools")

# ---------------------------------------------------------------------------
# Backend base URLs (from environment)
# ---------------------------------------------------------------------------

MONITORING_BASE = os.environ.get("MONITORING_BASE", "http://synthetic-monitoring:8000")
EHR_BASE = os.environ.get("EHR_BASE", "http://synthetic-ehr:8080")
LIS_BASE = os.environ.get("LIS_BASE", "http://synthetic-lis:8000")
PHARMACY_BASE = os.environ.get("PHARMACY_BASE", "http://synthetic-pharmacy:8000")
RADIOLOGY_BASE = os.environ.get("RADIOLOGY_BASE", "http://synthetic-radiology:8042")

# Orthanc credentials from env (never hardcode)
ORTHANC_USER = os.environ.get("ORTHANC_USER", "orthanc")
ORTHANC_PASS = os.environ.get("ORTHANC_PASS", "orthanc")

def _orthanc_auth() -> tuple[str, str]:
    return (ORTHANC_USER, ORTHANC_PASS)
BLOODBANK_BASE = os.environ.get("BLOODBANK_BASE", "http://synthetic-bloodbank:8000")
ERP_BASE = os.environ.get("ERP_BASE", "http://synthetic-erp:8000")
PATIENT_SERVICES_BASE = os.environ.get("PATIENT_SERVICES_BASE", "http://synthetic-patient-services:8000")

# ---------------------------------------------------------------------------
# Shared httpx.AsyncClient (created/closed via lifespan)
# ---------------------------------------------------------------------------

_http_client: httpx.AsyncClient | None = None


def init_http_client() -> None:
    global _http_client
    _http_client = httpx.AsyncClient(timeout=15.0)


async def close_http_client() -> None:
    global _http_client
    if _http_client:
        await _http_client.aclose()
        _http_client = None


# ---------------------------------------------------------------------------
# Confirmation HMAC signing (in-memory secret, regenerated each startup)
# ---------------------------------------------------------------------------

_CONFIRM_SECRET: bytes = os.urandom(32)

CONFIRMATION_TTL_SECONDS = int(os.environ.get("CONFIRMATION_TTL_SECONDS", "300"))  # 5 min

# ---------------------------------------------------------------------------
# Tool registry — loaded from config/tools.json
# ---------------------------------------------------------------------------

_TOOLS_CONFIG: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Tool domain groups — loaded from config.json
# ---------------------------------------------------------------------------

_domain_config: dict[str, list[str]] = {}


def load_domain_config(config_path: str) -> None:
    """Load tool_domains from config.json, warn about orphan tools."""
    global _domain_config
    if not os.path.exists(config_path):
        return
    with open(config_path) as f:
        data = json.load(f)
    _domain_config = data.get("tool_domains", {})
    if not _domain_config:
        return

    # Validate: warn about tools not in any domain
    assigned = set()
    for tools_list in _domain_config.values():
        assigned.update(tools_list)

    all_tools = set(TOOL_BACKENDS.keys()) | {"escalate"} | set(_GATEWAY_TOOLS.keys())
    orphans = all_tools - assigned
    if orphans:
        logger.warning("Orphan tools (not in any domain): %s", sorted(orphans))

    logger.info("Loaded %d tool domains (%d tools assigned)", len(_domain_config), len(assigned))


def get_tools_for_domains(domains: list[str]) -> list[str]:
    """Return union of tool names for given domains. Always includes 'core'."""
    if not _domain_config:
        return []  # No domains configured, caller should use all tools
    result: set[str] = set()
    # Always include core
    if "core" not in domains:
        domains = ["core"] + list(domains)
    for domain in domains:
        result.update(_domain_config.get(domain, []))
    return list(result)


def load_tools_config(path: str = "/app/config/tools.json") -> None:
    """Load tool criticality definitions from config file."""
    global _TOOLS_CONFIG
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        _TOOLS_CONFIG = data.get("tools", {})


def is_critical(tool_name: str) -> bool:
    return _TOOLS_CONFIG.get(tool_name, {}).get("critical", False)


def validate_params(tool_name: str, params: dict[str, Any]) -> str | None:
    """Validate tool params against schema in tools.json. Returns error string or None."""
    schema = _TOOLS_CONFIG.get(tool_name, {}).get("params")
    if not schema:
        return None
    errors = []
    for param_name, rules in schema.items():
        value = params.get(param_name)
        if rules.get("required") and value is None:
            errors.append(f"missing required param '{param_name}'")
            continue
        if value is None:
            continue
        expected_type = rules.get("type")
        if expected_type == "string" and not isinstance(value, str):
            errors.append(f"'{param_name}' must be a string")
        elif expected_type == "number" and not isinstance(value, (int, float)):
            errors.append(f"'{param_name}' must be a number")
        if "enum" in rules and value not in rules["enum"]:
            errors.append(f"'{param_name}' must be one of {rules['enum']}")
        if "pattern" in rules and isinstance(value, str):
            if not re.match(rules["pattern"], value):
                errors.append(f"'{param_name}' does not match pattern '{rules['pattern']}'")
    return "; ".join(errors) if errors else None


# ---------------------------------------------------------------------------
# Tool → backend mapping
# ---------------------------------------------------------------------------

# Each entry: (base_url, method, path_template)
# Path template uses {param_name} for substitution from tool params.
TOOL_BACKENDS: dict[str, tuple[str, str, str]] = {
    # Monitoring
    "get_vitals": (MONITORING_BASE, "GET", "/vitals/{patient_id}"),
    "get_vitals_history": (MONITORING_BASE, "GET", "/vitals/{patient_id}/history"),
    "get_vitals_trend": (MONITORING_BASE, "GET", "/vitals/{patient_id}/trend"),
    "get_patient_thresholds": (MONITORING_BASE, "GET", "/thresholds/{patient_id}"),
    "list_wards": (MONITORING_BASE, "GET", "/wards"),
    "list_doctors": (MONITORING_BASE, "GET", "/doctors"),
    "get_ward_patients": (MONITORING_BASE, "GET", "/ward/{ward_id}/patients"),
    "get_doctor_patients": (MONITORING_BASE, "GET", "/doctor/{doctor_id}/patients"),
    "get_patient_events": (MONITORING_BASE, "GET", "/events/{patient_id}"),
    "get_event_vitals": (MONITORING_BASE, "GET", "/events/{patient_id}/{event_id}/vitals"),
    "get_event_ecg": (MONITORING_BASE, "GET", "/events/{patient_id}/{event_id}/ecg"),
    "initiate_code_blue": (MONITORING_BASE, "POST", "/code-blue"),
    "get_active_alarms": (MONITORING_BASE, "GET", "/alarms/{patient_id}"),
    "clear_alarm": (MONITORING_BASE, "POST", "/alarms/{alarm_id}/clear"),
    "update_patient_thresholds": (MONITORING_BASE, "PUT", "/thresholds/{patient_id}"),
    # EHR
    "get_patient": (EHR_BASE, "GET", "/fhir/Patient?identifier={patient_id}"),
    "get_medications": (EHR_BASE, "GET", "/fhir/MedicationRequest?patient={patient_id}"),
    "get_allergies": (EHR_BASE, "GET", "/fhir/AllergyIntolerance?patient={patient_id}"),
    "get_orders": (EHR_BASE, "GET", "/fhir/ServiceRequest?patient={patient_id}"),
    "write_order": (EHR_BASE, "POST", "/fhir/ServiceRequest"),
    # Radiology
    "get_studies": (RADIOLOGY_BASE, "GET", "/dicom-web/studies?PatientID={patient_id}"),
    "get_report": (RADIOLOGY_BASE, "GET", "/dicom-web/studies/{study_id}/report"),
    "get_latest_study": (RADIOLOGY_BASE, "GET", "/dicom-web/studies?PatientID={patient_id}&limit=1"),
    # LIS
    "get_lab_results": (LIS_BASE, "GET", "/labs/{patient_id}"),
    "get_lab_order": (LIS_BASE, "GET", "/lab/{order_id}"),
    "order_lab": (LIS_BASE, "POST", "/lab/order"),
    "get_order_status": (LIS_BASE, "GET", "/lab/{order_id}"),
    # Monitoring — ECG
    "get_latest_ecg": (MONITORING_BASE, "GET", "/ecg/{patient_id}/latest"),
    # Monitoring — Conditions
    "get_conditions": (MONITORING_BASE, "GET", "/conditions/{patient_id}"),
    # Pharmacy
    "check_drug_interactions": (PHARMACY_BASE, "POST", "/interactions"),
    "dispense_medication": (PHARMACY_BASE, "POST", "/dispense"),
    # Blood bank
    "get_blood_availability": (BLOODBANK_BASE, "GET", "/availability"),
    "order_blood_crossmatch": (BLOODBANK_BASE, "POST", "/crossmatch"),
    "get_crossmatch_status": (BLOODBANK_BASE, "GET", "/crossmatch/{request_id}"),
    # ERP
    "get_inventory": (ERP_BASE, "GET", "/inventory"),
    "get_equipment_status": (ERP_BASE, "GET", "/equipment/{equipment_id}"),
    "place_supply_order": (ERP_BASE, "POST", "/supply-order"),
    # Patient services
    "request_housekeeping": (PATIENT_SERVICES_BASE, "POST", "/housekeeping"),
    "order_diet": (PATIENT_SERVICES_BASE, "POST", "/diet-order"),
    "request_ambulance": (PATIENT_SERVICES_BASE, "POST", "/transport"),
    "get_request_status": (PATIENT_SERVICES_BASE, "GET", "/request/{request_id}"),
    # Bed resolution (normalization handled by monitoring backend)
    "resolve_bed": (MONITORING_BASE, "GET", "/bed/{bed_id}/patient"),
    # Doctor resolution (fuzzy name match)
    "resolve_doctor": (MONITORING_BASE, "GET", "/doctor/resolve"),
    # Scheduling & reminders
    "schedule_appointment": (PATIENT_SERVICES_BASE, "POST", "/appointment"),
    "set_reminder": (PATIENT_SERVICES_BASE, "POST", "/reminder"),
}

# ---------------------------------------------------------------------------
# Gateway-level tools (handled locally, not dispatched to backends)
# ---------------------------------------------------------------------------

async def _get_ward_rounds(params: dict, session: Any) -> dict:
    """Fetch ward rounds from monitoring, fan-out for meds + latest scan per patient."""
    ward_id = params.get("ward_id", "")
    client = _http_client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await client.get(f"{MONITORING_BASE}/ward/{ward_id}/rounds")
        if resp.status_code != 200:
            return {"error": f"Monitoring returned {resp.status_code}", "detail": resp.text[:500]}
        data = resp.json()
    finally:
        if not _http_client:
            await client.aclose()

    patients = data.get("patients", [])

    async def _enrich_patient(p: dict, c: httpx.AsyncClient) -> None:
        pid = p.get("patient_id", "")
        meds_coro = c.get(f"{EHR_BASE}/fhir/MedicationRequest?patient={pid}")
        scan_coro = c.get(
            f"{RADIOLOGY_BASE}/dicom-web/studies?PatientID={pid}&limit=1",
            auth=_orthanc_auth(),
        )
        results = await asyncio.gather(meds_coro, scan_coro, return_exceptions=True)

        meds_result = results[0]
        if isinstance(meds_result, Exception) or meds_result.status_code != 200:
            p["medications"] = []
        else:
            p["medications"] = meds_result.json().get("medications", [])

        scan_result = results[1]
        if isinstance(scan_result, Exception) or scan_result.status_code != 200:
            p["latest_scan"] = None
        else:
            studies = scan_result.json().get("studies", [])
            p["latest_scan"] = studies[0] if studies else None

    enrich_client = _http_client or httpx.AsyncClient(timeout=15.0)
    try:
        await asyncio.gather(*[_enrich_patient(p, enrich_client) for p in patients])
    finally:
        if not _http_client:
            await enrich_client.aclose()

    return data


async def _request_additional_tools(params: dict, session: Any) -> dict:
    """Request tools from additional domains when current tools are insufficient."""
    domains = params.get("domains", [])
    valid = [d for d in domains if d in _domain_config]
    return {"status": "tools_expanded", "added_domains": valid}


async def _get_care_plan(params: dict, session: Any) -> dict:
    """Fan-out: aggregate orders, appointments, reminders, pending labs for a patient."""
    patient_id = params.get("patient_id", "")
    if not patient_id:
        return {"error": "patient_id is required"}

    client = _http_client or httpx.AsyncClient(timeout=15.0)
    try:
        orders_url, _ = _build_url(EHR_BASE, "/fhir/ServiceRequest?patient={patient_id}", {"patient_id": patient_id})
        labs_url, _ = _build_url(LIS_BASE, "/labs/{patient_id}", {"patient_id": patient_id})

        orders_coro = client.get(orders_url)
        appts_coro = client.get(
            f"{PATIENT_SERVICES_BASE}/appointments", params={"patient_id": patient_id},
        )
        reminders_coro = client.get(
            f"{PATIENT_SERVICES_BASE}/reminders", params={"patient_id": patient_id},
        )
        labs_coro = client.get(labs_url)

        results = await asyncio.gather(
            orders_coro, appts_coro, reminders_coro, labs_coro,
            return_exceptions=True,
        )

        # Orders
        orders_result = results[0]
        if isinstance(orders_result, Exception) or orders_result.status_code != 200:
            orders = []
        else:
            orders = orders_result.json().get("orders", orders_result.json().get("entry", []))

        # Appointments
        appts_result = results[1]
        if isinstance(appts_result, Exception) or appts_result.status_code != 200:
            appointments = []
        else:
            appointments = appts_result.json().get("appointments", [])

        # Reminders
        rem_result = results[2]
        if isinstance(rem_result, Exception) or rem_result.status_code != 200:
            reminders = []
        else:
            reminders = rem_result.json().get("reminders", [])

        # Pending labs
        labs_result = results[3]
        if isinstance(labs_result, Exception) or labs_result.status_code != 200:
            pending_labs = []
        else:
            all_labs = labs_result.json().get("labs", labs_result.json().get("results", []))
            pending_labs = [l for l in all_labs if l.get("status", "").lower() in ("pending", "in_progress", "ordered")]

    finally:
        if not _http_client:
            await client.aclose()

    return {
        "patient_id": patient_id,
        "orders": orders,
        "appointments": appointments,
        "reminders": reminders,
        "pending_labs": pending_labs,
    }


async def _get_ward_risk_ranking(params: dict, session: Any) -> dict:
    """Fan-out: fetch ward patients with vitals/NEWS and rank by risk."""
    ward_id = params.get("ward_id", "")
    if not ward_id:
        return {"error": "ward_id is required"}

    client = _http_client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await client.get(f"{MONITORING_BASE}/ward/{ward_id}/patients")
        if resp.status_code != 200:
            return {"error": f"Monitoring returned {resp.status_code}", "detail": resp.text[:500]}
        data = resp.json()
    finally:
        if not _http_client:
            await client.aclose()

    patients = data.get("patients", [])

    # Sort by NEWS score descending (highest risk first)
    def _news_score(p: dict) -> float:
        # Try multiple locations where NEWS score might live
        score = p.get("news2_score") or p.get("news_score") or 0
        vitals = p.get("latest_vitals") or p.get("vitals") or {}
        if not score and isinstance(vitals, dict):
            score = vitals.get("news2_score", vitals.get("news_score", 0))
        return float(score) if score else 0.0

    patients.sort(key=_news_score, reverse=True)

    ranked = []
    for p in patients:
        score = _news_score(p)
        if score >= 7:
            risk_level = "high"
        elif score >= 5:
            risk_level = "medium"
        else:
            risk_level = "low"
        ranked.append({
            "patient_id": p.get("patient_id", ""),
            "name": p.get("name", ""),
            "bed": p.get("bed", ""),
            "news2_score": score,
            "risk_level": risk_level,
            "latest_vitals": p.get("latest_vitals") or p.get("vitals") or {},
        })

    return {"ward_id": ward_id, "patients": ranked}


_GATEWAY_TOOLS: dict[str, callable] = {
    "get_ward_rounds": _get_ward_rounds,
    "request_additional_tools": _request_additional_tools,
    "get_care_plan": _get_care_plan,
    "get_ward_risk_ranking": _get_ward_risk_ranking,
}


# ---------------------------------------------------------------------------
# Pending critical-tool confirmations
# ---------------------------------------------------------------------------

_pending: dict[str, dict] = {}
_PENDING_MAX_SIZE = int(os.environ.get("PENDING_MAX_SIZE", "1000"))


def get_tool_list(domains: list[str] | None = None) -> list[dict]:
    """Return tool definitions for the agent/LLM, optionally filtered by domains."""
    allowed = set(get_tools_for_domains(domains)) if domains and _domain_config else None
    tools = []
    for name in TOOL_BACKENDS:
        if allowed is not None and name not in allowed:
            continue
        critical = is_critical(name)
        tools.append({"name": name, "critical": critical})
    # Gateway-level tools
    if allowed is None or "escalate" in allowed:
        tools.append({"name": "escalate", "critical": False})
    _always_include = {"request_additional_tools"}
    for name in _GATEWAY_TOOLS:
        if allowed is not None and name not in allowed and name not in _always_include:
            continue
        critical = is_critical(name)
        tools.append({"name": name, "critical": critical})
    return tools


# ---------------------------------------------------------------------------
# Tool descriptions (used in system prompt and native tool definitions)
# ---------------------------------------------------------------------------

_TOOL_DESCRIPTIONS: dict[str, str] = {
    "get_ward_rounds": "Get ward rounds report with vitals, meds, scans per patient",
    "resolve_bed": "Resolve bed number to patient_id (e.g. '2' or 'BED2')",
    "resolve_doctor": "Resolve doctor name to doctor ID",
    "schedule_appointment": "Schedule appointment for a patient with a doctor",
    "set_reminder": "Set a timed reminder (fires via push notification)",
    "get_vitals": "Get latest vitals for a patient",
    "get_vitals_history": "Get vitals history for a patient",
    "get_vitals_trend": "Get vitals trend analysis with EWS scoring",
    "get_patient_thresholds": "Get patient-specific vital sign thresholds (physician orders or hospital defaults)",
    "update_patient_thresholds": "Update patient-specific vital sign thresholds (partial update, merged with existing)",
    "get_active_alarms": "Get active clinical alarms for a patient (vitals breach, ventilator, infusion pump, etc.)",
    "clear_alarm": "Clear/acknowledge a clinical alarm (requires cleared_by and reason)",
    "get_medications": "Get medications for a patient",
    "get_allergies": "Get allergies for a patient",
    "get_conditions": "Get patient conditions and comorbidities",
    "get_lab_results": "Get lab results for a patient",
    "get_patient": "Get patient demographics",
    "escalate": "Escalate to a human clinician",
    "list_wards": "List all wards",
    "list_doctors": "List all doctors",
    "get_ward_patients": "Get patients in a ward",
    "get_doctor_patients": "Get patients for a doctor",
    "get_patient_events": "Get clinical events for a patient",
    "get_event_vitals": "Get vitals for a clinical event",
    "get_event_ecg": "Get ECG data for a clinical event",
    "get_latest_ecg": "Get the most recent ECG for a patient (no event_id needed)",
    "initiate_code_blue": "Initiate a code blue emergency for a patient",
    "get_orders": "Get orders for a patient",
    "write_order": "Write a clinical order for a patient",
    "get_studies": "Get radiology studies for a patient",
    "get_report": "Get radiology report for a study",
    "get_latest_study": "Get latest radiology study for a patient",
    "get_lab_order": "Get details of a lab order",
    "order_lab": "Order a lab test for a patient",
    "get_order_status": "Get status of a lab order",
    "check_drug_interactions": "Check for drug interactions",
    "dispense_medication": "Dispense medication to a patient",
    "get_blood_availability": "Check blood bank availability",
    "order_blood_crossmatch": "Order a blood crossmatch",
    "get_crossmatch_status": "Get crossmatch request status",
    "get_inventory": "Get hospital inventory",
    "get_equipment_status": "Get equipment status",
    "place_supply_order": "Place a supply order",
    "request_housekeeping": "Request housekeeping service for a room (room, request_type, priority)",
    "order_diet": "Order diet for a patient (patient_id, diet_type e.g. veg/regular/diabetic, meal e.g. breakfast/lunch/dinner)",
    "request_ambulance": "Request ambulance/transport for a patient (patient_id, from_location, to_location)",
    "get_request_status": "Get status of a service request",
    "request_additional_tools": (
        "Request tools from additional domains when current tools are insufficient. "
        "Available domains: core, vitals, labs, medications, ecg, radiology, orders, "
        "emergency, services, scheduling, supplies, ward"
    ),
    "get_care_plan": "Get aggregated care plan for a patient (orders, appointments, reminders, pending labs)",
    "get_ward_risk_ranking": "Get patients in a ward ranked by clinical risk (NEWS2 score)",
}


_HARDCODED_SCHEMAS: dict[str, dict] = {
    "request_additional_tools": {
        "type": "object",
        "properties": {
            "domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Domain names to add (e.g. ['radiology', 'ecg'])",
            },
        },
        "required": ["domains"],
    },
    "get_care_plan": {
        "type": "object",
        "properties": {
            "patient_id": {
                "type": "string",
                "description": "Patient identifier",
            },
        },
        "required": ["patient_id"],
    },
    "get_ward_risk_ranking": {
        "type": "object",
        "properties": {
            "ward_id": {
                "type": "string",
                "description": "Ward identifier",
            },
        },
        "required": ["ward_id"],
    },
}


def _schema_to_openai_params(tool_name: str) -> dict:
    """Convert tools.json params schema to OpenAI-style JSON Schema parameters."""
    if tool_name in _HARDCODED_SCHEMAS:
        return _HARDCODED_SCHEMAS[tool_name]
    schema = _TOOLS_CONFIG.get(tool_name, {}).get("params")
    if not schema:
        return {"type": "object", "properties": {}}
    properties = {}
    required = []
    for param_name, rules in schema.items():
        prop: dict[str, Any] = {}
        prop["type"] = rules.get("type", "string")
        if "enum" in rules:
            prop["enum"] = rules["enum"]
        if "pattern" in rules:
            prop["pattern"] = rules["pattern"]
        properties[param_name] = prop
        if rules.get("required"):
            required.append(param_name)
    result: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


def build_tool_definitions(domains: list[str] | None = None) -> list[dict]:
    """Build canonical tool definitions for native function calling.

    If domains provided and domain config loaded, filter to only those tools.
    """
    allowed = set(get_tools_for_domains(domains)) if domains and _domain_config else None
    defs = []
    _always_include = {"request_additional_tools"}
    all_tool_names = list(TOOL_BACKENDS.keys()) + ["escalate"] + list(_GATEWAY_TOOLS.keys())
    for name in all_tool_names:
        if allowed is not None and name not in allowed and name not in _always_include:
            continue
        defs.append({
            "name": name,
            "description": _TOOL_DESCRIPTIONS.get(name, name),
            "parameters": _schema_to_openai_params(name),
        })
    # Skill tool definitions are added by the orchestrator via skill_registry.tool_definitions()
    return defs


# ---------------------------------------------------------------------------
# Parallel tool dispatch
# ---------------------------------------------------------------------------

async def call_tools_parallel(
    calls: list[tuple[str, dict]], session: Any
) -> list[dict]:
    """Dispatch multiple tool calls in parallel via asyncio.gather.

    Each tool goes through call_tool (validation, critical gating preserved).
    One failure does not kill siblings.
    """
    async def _single(name: str, params: dict) -> dict:
        try:
            result = await call_tool(name, params, session)
        except Exception as exc:
            result = {"error": str(exc)}
        return {"tool": name, "params": params, "data": result}

    return list(await asyncio.gather(*[_single(n, p) for n, p in calls]))


async def call_tool(
    tool_name: str,
    params: dict[str, Any],
    session: Any,
) -> dict[str, Any]:
    """Dispatch a tool call. Critical tools are gated behind confirmation."""
    # Validate params against schema
    validation_error = validate_params(tool_name, params)
    if validation_error:
        return {"error": f"Invalid parameters: {validation_error}"}

    # Gateway-level: escalate
    if tool_name == "escalate":
        return await _escalate(params, session)

    # Gateway-level: local tools (rounds, domain expansion)
    if tool_name in _GATEWAY_TOOLS:
        return await _GATEWAY_TOOLS[tool_name](params, session)

    # Inject session context for reminder dispatch (backend needs session_id/channel)
    if tool_name == "set_reminder":
        params = {**params, "session_id": session.id, "channel": session.channel}

    if tool_name not in TOOL_BACKENDS:
        return {"error": f"Unknown tool: {tool_name}"}

    if is_critical(tool_name):
        if len(_pending) >= _PENDING_MAX_SIZE:
            cleanup_expired_confirmations()
        if len(_pending) >= _PENDING_MAX_SIZE:
            return {"error": "Too many pending confirmations. Please try again later."}
        cid = str(uuid.uuid4())
        params_hash = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
        sig = hmac.new(
            _CONFIRM_SECRET,
            f"{cid}:{tool_name}:{params_hash}:{session.user_id}".encode(),
            "sha256",
        ).hexdigest()
        _pending[cid] = {
            "tool_name": tool_name,
            "params": params,
            "session_id": session.id,
            "tenant_id": session.tenant_id,
            "user_id": session.user_id,
            "channel": session.channel,
            "created_at": time.monotonic(),
            "client_id": getattr(session, "_client_id", ""),
            "signature": sig,
        }
        await log_action(
            tenant_id=session.tenant_id,
            session_id=session.id,
            user_id=session.user_id,
            channel=session.channel,
            action="critical_tool_gated",
            tool_name=tool_name,
            params=params,
            confirmation_id=cid,
        )
        return {
            "status": "awaiting_confirmation",
            "confirmation_id": cid,
            "message": f"Critical action '{tool_name}' requires confirmation. POST /confirm/{cid} to execute.",
        }

    return await _dispatch(tool_name, _strip_client_hints(params), session)


# Client-only keys that should never be sent to backends
_CLIENT_HINT_KEYS = {"display"}


def _strip_client_hints(params: dict) -> dict:
    """Remove client-side rendering hints before dispatching to backend."""
    return {k: v for k, v in params.items() if k not in _CLIENT_HINT_KEYS}


async def confirm_tool(confirmation_id: str, session: Any, client_id: str = "") -> dict[str, Any]:
    """Execute a pending critical tool after confirmation."""
    entry = _pending.get(confirmation_id)
    if entry is None:
        return {"error": "Confirmation not found or already executed"}

    # TTL check
    age = time.monotonic() - entry["created_at"]
    if age > CONFIRMATION_TTL_SECONDS:
        _pending.pop(confirmation_id, None)
        return {"error": "Confirmation expired"}

    # Client binding check
    if client_id and entry.get("client_id") and client_id != entry["client_id"]:
        return {"error": "Client mismatch"}

    # HMAC signature verification
    params_hash = hashlib.sha256(json.dumps(entry["params"], sort_keys=True).encode()).hexdigest()[:16]
    expected_sig = hmac.new(
        _CONFIRM_SECRET,
        f"{confirmation_id}:{entry['tool_name']}:{params_hash}:{entry['user_id']}".encode(),
        "sha256",
    ).hexdigest()
    if not hmac.compare_digest(entry.get("signature", ""), expected_sig):
        return {"error": "Signature verification failed"}

    _pending.pop(confirmation_id)
    result = await _dispatch(entry["tool_name"], entry["params"], session)
    await log_action(
        tenant_id=entry["tenant_id"],
        session_id=entry["session_id"],
        user_id=entry["user_id"],
        channel=entry["channel"],
        action="critical_tool_confirmed",
        tool_name=entry["tool_name"],
        params=entry["params"],
        result_summary=_summarize(result),
        confirmation_id=confirmation_id,
    )
    return result


def cleanup_expired_confirmations() -> int:
    """Remove pending confirmations older than TTL. Returns count removed."""
    now = time.monotonic()
    expired = [cid for cid, entry in _pending.items()
               if now - entry.get("created_at", 0) > CONFIRMATION_TTL_SECONDS]
    for cid in expired:
        _pending.pop(cid, None)
    return len(expired)


# ---------------------------------------------------------------------------
# HTTP dispatch to synthetic backends
# ---------------------------------------------------------------------------

def _build_url(base: str, path_template: str, params: dict) -> tuple[str, dict]:
    """Build URL from template, return (url, remaining_params)."""
    used_keys = set()
    def replacer(m):
        key = m.group(1)
        used_keys.add(key)
        return str(params.get(key, ""))
    path = re.sub(r"\{(\w+)\}", replacer, path_template)
    remaining = {k: v for k, v in params.items() if k not in used_keys}
    return f"{base}{path}", remaining


def _trace_headers() -> dict[str, str]:
    """Build X-Request-ID header for downstream tracing."""
    try:
        from main import request_id_var
        rid = request_id_var.get("")
        if rid:
            return {"X-Request-ID": rid}
    except (ImportError, LookupError):
        pass
    return {}


async def _dispatch(tool_name: str, params: dict, session: Any) -> dict:
    """HTTP dispatch to the appropriate synthetic backend."""
    import time as _time
    import metrics as _metrics
    _t0 = _time.time()
    base, method, path_template = TOOL_BACKENDS[tool_name]
    url, remaining = _build_url(base, path_template, params)
    logger.info("[%s] dispatch %s %s", session.id, method, url)
    auth = _orthanc_auth() if base == RADIOLOGY_BASE else None
    headers = _trace_headers()
    client = _http_client or httpx.AsyncClient(timeout=15.0)
    try:
        if method == "GET":
            resp = await client.get(url, params=remaining if remaining else None, auth=auth, headers=headers)
        elif method == "PUT":
            resp = await client.put(url, json=remaining if remaining else params, auth=auth, headers=headers)
        else:
            resp = await client.post(url, json=remaining if remaining else params, auth=auth, headers=headers)
        resp.raise_for_status()
        logger.info("[%s] dispatch %s -> %d", session.id, tool_name, resp.status_code)
        _metrics.TOOL_CALLS.labels(tool_name=tool_name, status="ok").inc()
        _metrics.TOOL_DURATION.labels(tool_name=tool_name).observe(_time.time() - _t0)
        return resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("[%s] dispatch %s -> HTTP %d", session.id, tool_name, exc.response.status_code)
        _metrics.TOOL_CALLS.labels(tool_name=tool_name, status="error").inc()
        _metrics.TOOL_DURATION.labels(tool_name=tool_name).observe(_time.time() - _t0)
        return {"error": f"Backend returned {exc.response.status_code}", "detail": exc.response.text[:500]}
    except Exception as exc:
        logger.error("[%s] dispatch %s -> unreachable: %s", session.id, tool_name, exc)
        _metrics.TOOL_CALLS.labels(tool_name=tool_name, status="error").inc()
        _metrics.TOOL_DURATION.labels(tool_name=tool_name).observe(_time.time() - _t0)
        return {"error": f"Backend unreachable: {exc}"}
    finally:
        if not _http_client:
            await client.aclose()


# ---------------------------------------------------------------------------
# Gateway-level tools
# ---------------------------------------------------------------------------

async def _escalate(params: dict, session: Any) -> dict:
    """Escalate to a human — logs intent, returns confirmation."""
    patient_id = params.get("patient_id", "unknown")
    reason = params.get("reason", "")
    escalate_to = params.get("escalate_to", "on_call_physician")

    audit_id = await log_action(
        tenant_id=session.tenant_id,
        session_id=session.id,
        user_id=session.user_id,
        channel=session.channel,
        action="escalate",
        tool_name="escalate",
        params=params,
        result_summary=f"Escalated to {escalate_to} for patient {patient_id}",
    )
    esc_id = await log_escalation(
        tenant_id=session.tenant_id,
        audit_log_id=audit_id,
        escalated_to=escalate_to,
        reason=reason,
    )
    return {
        "status": "escalated",
        "escalation_id": esc_id,
        "escalated_to": escalate_to,
        "message": f"Escalation logged. {escalate_to} has been notified regarding patient {patient_id}.",
    }


def _summarize(result: dict) -> str:
    """Short summary of a tool result for audit logging."""
    s = json.dumps(result)
    return s[:200] if len(s) > 200 else s
