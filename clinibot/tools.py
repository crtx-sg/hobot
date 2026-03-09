"""Tool registry, MCP backend dispatch, and critical-tool confirmation gate."""

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
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
BLOODBANK_BASE = os.environ.get("BLOODBANK_BASE", "http://synthetic-bloodbank:8000")
ERP_BASE = os.environ.get("ERP_BASE", "http://synthetic-erp:8000")
PATIENT_SERVICES_BASE = os.environ.get("PATIENT_SERVICES_BASE", "http://synthetic-patient-services:8000")

# ---------------------------------------------------------------------------
# Tool registry — loaded from config/tools.json
# ---------------------------------------------------------------------------

_TOOLS_CONFIG: dict[str, dict] = {}


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
    "list_wards": (MONITORING_BASE, "GET", "/wards"),
    "list_doctors": (MONITORING_BASE, "GET", "/doctors"),
    "get_ward_patients": (MONITORING_BASE, "GET", "/ward/{ward_id}/patients"),
    "get_doctor_patients": (MONITORING_BASE, "GET", "/doctor/{doctor_id}/patients"),
    "get_patient_events": (MONITORING_BASE, "GET", "/events/{patient_id}"),
    "get_event_vitals": (MONITORING_BASE, "GET", "/events/{patient_id}/{event_id}/vitals"),
    "get_event_ecg": (MONITORING_BASE, "GET", "/events/{patient_id}/{event_id}/ecg"),
    "initiate_code_blue": (MONITORING_BASE, "POST", "/code-blue"),
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
}

# ---------------------------------------------------------------------------
# In-memory stores for scheduling & reminders
# ---------------------------------------------------------------------------

_appointments: dict[str, dict] = {}
_reminders: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Gateway-level tools (handled locally, not dispatched to backends)
# ---------------------------------------------------------------------------

async def _get_ward_rounds(params: dict, session: Any) -> dict:
    """Fetch ward rounds from monitoring, fan-out for meds + latest scan per patient."""
    ward_id = params.get("ward_id", "")
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{MONITORING_BASE}/ward/{ward_id}/rounds")
        if resp.status_code != 200:
            return {"error": f"Monitoring returned {resp.status_code}", "detail": resp.text[:500]}
        data = resp.json()

    patients = data.get("patients", [])

    async def _enrich_patient(p: dict, client: httpx.AsyncClient) -> None:
        pid = p.get("patient_id", "")
        meds_coro = client.get(f"{EHR_BASE}/fhir/MedicationRequest?patient={pid}")
        scan_coro = client.get(
            f"{RADIOLOGY_BASE}/dicom-web/studies?PatientID={pid}&limit=1",
            auth=("orthanc", "orthanc"),
        )
        results = await asyncio.gather(meds_coro, scan_coro, return_exceptions=True)

        # Medications
        meds_result = results[0]
        if isinstance(meds_result, Exception) or meds_result.status_code != 200:
            p["medications"] = []
        else:
            p["medications"] = meds_result.json().get("medications", [])

        # Latest scan
        scan_result = results[1]
        if isinstance(scan_result, Exception) or scan_result.status_code != 200:
            p["latest_scan"] = None
        else:
            studies = scan_result.json().get("studies", [])
            p["latest_scan"] = studies[0] if studies else None

    async with httpx.AsyncClient(timeout=15.0) as client:
        await asyncio.gather(*[_enrich_patient(p, client) for p in patients])

    return data


async def _resolve_bed(params: dict, session: Any) -> dict:
    """Resolve bed ID to patient ID via monitoring service."""
    bed_id = params.get("bed_id", "").strip().upper()
    # Normalize: "2" → "BED2", "bed2" → "BED2"
    if bed_id and not bed_id.startswith("BED"):
        bed_id = f"BED{bed_id}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{MONITORING_BASE}/bed/{bed_id}/patient")
        if resp.status_code != 200:
            return {"error": f"Bed {bed_id} not found"}
        return resp.json()


async def _schedule_appointment(params: dict, session: Any) -> dict:
    """Schedule an appointment (in-memory)."""
    appt_id = f"APPT-{uuid.uuid4().hex[:8].upper()}"
    appt = {
        "appointment_id": appt_id,
        "patient_id": params.get("patient_id", ""),
        "doctor": params.get("doctor", ""),
        "datetime": params.get("datetime", ""),
        "notes": params.get("notes", ""),
        "status": "scheduled",
    }
    _appointments[appt_id] = appt
    return appt


async def _set_reminder(params: dict, session: Any) -> dict:
    """Set a reminder (in-memory). Fires after delay_minutes via background loop."""
    rem_id = f"REM-{uuid.uuid4().hex[:8].upper()}"
    delay = params.get("delay_minutes", 60)
    trigger_at = datetime.now(timezone.utc) + timedelta(minutes=delay)
    reminder = {
        "reminder_id": rem_id,
        "session_id": session.id,
        "channel": session.channel,
        "message": params.get("message", ""),
        "trigger_at": trigger_at.isoformat(),
        "status": "pending",
    }
    _reminders[rem_id] = reminder
    return {
        "reminder_id": rem_id,
        "trigger_at": trigger_at.isoformat(),
        "message": reminder["message"],
        "status": "scheduled",
    }


_GATEWAY_TOOLS: dict[str, callable] = {
    "get_ward_rounds": _get_ward_rounds,
    "resolve_bed": _resolve_bed,
    "schedule_appointment": _schedule_appointment,
    "set_reminder": _set_reminder,
}


# ---------------------------------------------------------------------------
# Pending critical-tool confirmations
# ---------------------------------------------------------------------------

_pending: dict[str, dict] = {}


def get_tool_list() -> list[dict]:
    """Return tool definitions for the agent/LLM."""
    tools = []
    for name in TOOL_BACKENDS:
        critical = is_critical(name)
        tools.append({"name": name, "critical": critical})
    # Gateway-level tools
    tools.append({"name": "escalate", "critical": False})
    for name in _GATEWAY_TOOLS:
        critical = is_critical(name)
        tools.append({"name": name, "critical": critical})
    return tools


# ---------------------------------------------------------------------------
# Tool descriptions (used in system prompt and native tool definitions)
# ---------------------------------------------------------------------------

_TOOL_DESCRIPTIONS: dict[str, str] = {
    "get_ward_rounds": "Get ward rounds report with vitals, meds, scans per patient",
    "resolve_bed": "Resolve bed number to patient_id (bed_id: just the number e.g. '2' or 'BED2')",
    "schedule_appointment": "Schedule appointment for a patient with a doctor",
    "set_reminder": "Set a timed reminder (fires via Telegram push)",
    "get_vitals": "Get latest vitals for a patient",
    "get_vitals_history": "Get vitals history for a patient",
    "get_vitals_trend": "Get vitals trend analysis with EWS scoring",
    "get_medications": "Get medications for a patient",
    "get_allergies": "Get allergies for a patient",
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
}


def _schema_to_openai_params(tool_name: str) -> dict:
    """Convert tools.json params schema to OpenAI-style JSON Schema parameters."""
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


def build_tool_definitions() -> list[dict]:
    """Build canonical tool definitions for native function calling."""
    defs = []
    all_tool_names = list(TOOL_BACKENDS.keys()) + ["escalate"] + list(_GATEWAY_TOOLS.keys())
    for name in all_tool_names:
        defs.append({
            "name": name,
            "description": _TOOL_DESCRIPTIONS.get(name, name),
            "parameters": _schema_to_openai_params(name),
        })
    # Append analyzer tool definitions
    import analyzers
    defs.extend(analyzers.get_analyzer_tool_definitions())
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

    # Analyzer tools
    if tool_name.startswith("analyze_"):
        import analyzers
        return await analyzers.run_analyzer_tool(tool_name, params, session)

    # Gateway-level: escalate
    if tool_name == "escalate":
        return await _escalate(params, session)

    # Gateway-level: local tools (rounds, beds, scheduling, reminders)
    if tool_name in _GATEWAY_TOOLS:
        return await _GATEWAY_TOOLS[tool_name](params, session)

    if tool_name not in TOOL_BACKENDS:
        return {"error": f"Unknown tool: {tool_name}"}

    if is_critical(tool_name):
        cid = str(uuid.uuid4())
        _pending[cid] = {
            "tool_name": tool_name,
            "params": params,
            "session_id": session.id,
            "tenant_id": session.tenant_id,
            "user_id": session.user_id,
            "channel": session.channel,
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


async def confirm_tool(confirmation_id: str, session: Any) -> dict[str, Any]:
    """Execute a pending critical tool after confirmation."""
    entry = _pending.pop(confirmation_id, None)
    if entry is None:
        return {"error": "Confirmation not found or already executed"}

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


async def _dispatch(tool_name: str, params: dict, session: Any) -> dict:
    """HTTP dispatch to the appropriate synthetic backend."""
    base, method, path_template = TOOL_BACKENDS[tool_name]
    url, remaining = _build_url(base, path_template, params)
    logger.info("[%s] dispatch %s %s", session.id, method, url)
    # Orthanc (radiology) requires basic auth
    auth = ("orthanc", "orthanc") if base == RADIOLOGY_BASE else None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            if method == "GET":
                resp = await client.get(url, params=remaining if remaining else None, auth=auth)
            else:
                resp = await client.post(url, json=remaining if remaining else params, auth=auth)
            resp.raise_for_status()
            logger.info("[%s] dispatch %s -> %d", session.id, tool_name, resp.status_code)
            return resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("[%s] dispatch %s -> HTTP %d", session.id, tool_name, exc.response.status_code)
        return {"error": f"Backend returned {exc.response.status_code}", "detail": exc.response.text[:500]}
    except Exception as exc:
        logger.error("[%s] dispatch %s -> unreachable: %s", session.id, tool_name, exc)
        return {"error": f"Backend unreachable: {exc}"}


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
