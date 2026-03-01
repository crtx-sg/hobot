"""Tool registry, MCP backend dispatch, and critical-tool confirmation gate."""

import json
import os
import re
import uuid
from typing import Any

import httpx

from audit import log_action, log_escalation

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
    "list_wards": (MONITORING_BASE, "GET", "/wards"),
    "list_doctors": (MONITORING_BASE, "GET", "/doctors"),
    "get_ward_patients": (MONITORING_BASE, "GET", "/wards/{ward_id}/patients"),
    "get_doctor_patients": (MONITORING_BASE, "GET", "/doctors/{doctor_id}/patients"),
    "get_patient_events": (MONITORING_BASE, "GET", "/patients/{patient_id}/events"),
    "get_event_vitals": (MONITORING_BASE, "GET", "/events/{event_id}/vitals"),
    "get_event_ecg": (MONITORING_BASE, "GET", "/events/{event_id}/ecg"),
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
    "get_lab_results": (LIS_BASE, "GET", "/results/{patient_id}"),
    "get_lab_order": (LIS_BASE, "GET", "/orders/{order_id}"),
    "order_lab": (LIS_BASE, "POST", "/orders"),
    "get_order_status": (LIS_BASE, "GET", "/orders/{order_id}/status"),
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
    return tools


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

    return await _dispatch(tool_name, params, session)


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
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            if method == "GET":
                resp = await client.get(url, params=remaining if remaining else None)
            else:
                resp = await client.post(url, json=remaining if remaining else params)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as exc:
        return {"error": f"Backend returned {exc.response.status_code}", "detail": exc.response.text[:500]}
    except Exception as exc:
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
