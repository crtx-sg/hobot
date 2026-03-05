"""Tool result → abstract block mappers for structured rich responses."""

from __future__ import annotations

NEWS_THRESHOLDS = {"heart_rate": (51, 90), "bp_systolic": (101, 179), "spo2": (96, 100), "temperature": (36.1, 38.0)}


def build_blocks(tool_results: list[dict]) -> list[dict]:
    """Convert a list of tool results into abstract UI blocks."""
    blocks: list[dict] = []
    for tr in tool_results:
        tool = tr.get("tool", "")
        data = tr.get("data", {})
        params = tr.get("params", {})
        # Check for confirmation-gated results first (any critical tool)
        if data.get("status") == "awaiting_confirmation":
            blocks.extend(_build_confirmation_blocks(data, params))
            continue
        builder = TOOL_BLOCK_MAP.get(tool)
        if builder:
            blocks.extend(builder(data, params))
    return blocks


# ---------------------------------------------------------------------------
# Per-tool block builders
# ---------------------------------------------------------------------------

def _build_vitals_blocks(data: dict, params: dict) -> list[dict]:
    if "error" in data:
        return []
    pid = data.get("patient_id", params.get("patient_id", ""))
    rows = [
        ["Heart Rate", str(data.get("heart_rate", "")), "bpm"],
        ["BP Systolic", str(data.get("bp_systolic", "")), "mmHg"],
        ["BP Diastolic", str(data.get("bp_diastolic", "")), "mmHg"],
        ["SpO2", str(data.get("spo2", "")), "%"],
        ["Temperature", str(data.get("temperature", "")), "°C"],
    ]
    blocks: list[dict] = [
        {
            "type": "data_table",
            "title": f"Vitals — {pid}",
            "tool": "get_vitals",
            "columns": ["Metric", "Value", "Unit"],
            "rows": rows,
        },
    ]
    # Alert if any vital is outside normal range
    alerts = _check_vital_alerts(data)
    for a in alerts:
        blocks.append({"type": "alert", "severity": "warning", "text": a})
    # Action: view history
    if pid:
        blocks.append({
            "type": "actions",
            "buttons": [{"label": "View History", "action": "get_vitals_history", "params": {"patient_id": pid}}],
        })
    return blocks


def _check_vital_alerts(data: dict) -> list[str]:
    alerts = []
    for key, (lo, hi) in NEWS_THRESHOLDS.items():
        val = data.get(key)
        if val is not None and (val < lo or val > hi):
            label = key.replace("_", " ").title()
            alerts.append(f"{label} abnormal: {val}")
    return alerts


def _build_vitals_history_blocks(data: dict, params: dict) -> list[dict]:
    if "error" in data:
        return []
    readings = data.get("readings", [])
    if not readings:
        return []
    pid = data.get("patient_id", params.get("patient_id", ""))
    rows = []
    for r in readings:
        rows.append([
            r.get("timestamp", "")[:16],
            str(r.get("heart_rate", "")),
            str(r.get("bp_systolic", "")),
            str(r.get("spo2", "")),
            str(r.get("temperature", "")),
        ])
    blocks: list[dict] = [{
        "type": "data_table",
        "title": f"Vitals History — {pid}",
        "tool": "get_vitals_history",
        "columns": ["Time", "HR", "BP Sys", "SpO2", "Temp"],
        "rows": rows,
    }]
    # Chart block for trend visualization
    if len(readings) >= 2:
        series = {}
        for metric in ("heart_rate", "bp_systolic", "spo2", "temperature"):
            series[metric] = [
                {"t": r.get("timestamp", ""), "v": r.get(metric)}
                for r in readings if r.get(metric) is not None
            ]
        blocks.append({
            "type": "chart",
            "chart_type": "line",
            "title": f"Vitals Trend — {pid}",
            "x_label": "Time",
            "y_label": "Value",
            "series": series,
        })
    return blocks


def _build_lab_blocks(data: dict, params: dict) -> list[dict]:
    if "error" in data:
        return []
    labs = data.get("labs", [])
    if not labs:
        return []
    pid = data.get("patient_id", params.get("patient_id", ""))
    blocks: list[dict] = []
    for lab in labs:
        results = lab.get("results", {})
        rows = []
        for test_name, info in results.items():
            rows.append([
                test_name,
                str(info.get("value", "")),
                info.get("unit", ""),
                info.get("ref_range", ""),
            ])
        blocks.append({
            "type": "data_table",
            "title": f"{lab.get('test_type', 'Lab')} — {pid}",
            "tool": "get_lab_results",
            "columns": ["Test", "Value", "Unit", "Ref Range"],
            "rows": rows,
        })
    return blocks


def _build_medications_blocks(data: dict, params: dict) -> list[dict]:
    if "error" in data:
        return []
    meds = data.get("medications", [])
    if not meds:
        return []
    pid = data.get("patient_id", params.get("patient_id", ""))
    rows = []
    for m in meds:
        rows.append([
            m.get("medication", ""),
            m.get("dose", ""),
            m.get("route", ""),
            m.get("frequency", ""),
            m.get("status", ""),
        ])
    return [{
        "type": "data_table",
        "title": f"Medications — {pid}",
        "tool": "get_medications",
        "columns": ["Medication", "Dose", "Route", "Frequency", "Status"],
        "rows": rows,
    }]


def _build_allergies_blocks(data: dict, params: dict) -> list[dict]:
    if "error" in data:
        return []
    allergies = data.get("allergies", [])
    if not allergies:
        return []
    pid = data.get("patient_id", params.get("patient_id", ""))
    rows = []
    for a in allergies:
        reactions = a.get("reactions", [])
        manifestations = ", ".join(reactions[0].get("manifestation", [])) if reactions else ""
        rows.append([
            a.get("substance", ""),
            a.get("criticality", ""),
            manifestations,
            a.get("clinical_status", ""),
        ])
    return [{
        "type": "data_table",
        "title": f"Allergies — {pid}",
        "tool": "get_allergies",
        "columns": ["Substance", "Criticality", "Reactions", "Status"],
        "rows": rows,
    }]


def _build_patient_blocks(data: dict, params: dict) -> list[dict]:
    if "error" in data:
        return []
    items = [
        {"key": "Name", "value": data.get("name", "")},
        {"key": "Patient ID", "value": data.get("patient_id", "")},
        {"key": "Gender", "value": data.get("gender", "")},
        {"key": "DOB", "value": data.get("birth_date", "")},
    ]
    blocks: list[dict] = [{"type": "key_value", "title": "Patient Info", "items": items}]
    pid = data.get("patient_id", params.get("patient_id", ""))
    if pid:
        blocks.append({
            "type": "actions",
            "buttons": [
                {"label": "Vitals", "action": "get_vitals", "params": {"patient_id": pid}},
                {"label": "Medications", "action": "get_medications", "params": {"patient_id": pid}},
                {"label": "Lab Results", "action": "get_lab_results", "params": {"patient_id": pid}},
            ],
        })
    return blocks


def _build_blood_availability_blocks(data: dict, params: dict) -> list[dict]:
    if "error" in data:
        return []
    inventory = data.get("inventory", {})
    if not inventory:
        return []
    rows = [[bt, str(count)] for bt, count in inventory.items()]
    return [{
        "type": "data_table",
        "title": "Blood Bank Inventory",
        "tool": "get_blood_availability",
        "columns": ["Blood Type", "Units"],
        "rows": rows,
    }]


def _build_ward_patients_blocks(data: dict, params: dict) -> list[dict]:
    if "error" in data:
        return []
    patients = data.get("patients", [])
    if not patients:
        return []
    ward_id = data.get("ward_id", params.get("ward_id", ""))
    rows = []
    alerts = []
    for p in patients:
        news = p.get("news_score", 0)
        rows.append([
            p.get("patient_id", ""),
            str(p.get("vitals", {}).get("heart_rate", "")),
            str(p.get("vitals", {}).get("spo2", "")),
            str(news),
        ])
        if news >= 5:
            alerts.append(f"Patient {p.get('patient_id', '')} NEWS={news} — needs review")
    blocks: list[dict] = [{
        "type": "data_table",
        "title": f"Ward {ward_id} Patients",
        "tool": "get_ward_patients",
        "columns": ["Patient", "HR", "SpO2", "NEWS"],
        "rows": rows,
    }]
    for a in alerts:
        blocks.append({"type": "alert", "severity": "warning", "text": a})
    return blocks


def _build_drug_interactions_blocks(data: dict, params: dict) -> list[dict]:
    if "error" in data:
        return []
    interactions = data.get("interactions", [])
    if not interactions:
        return [{"type": "text", "content": "No drug interactions found."}]
    blocks: list[dict] = []
    for ix in interactions:
        blocks.append({
            "type": "alert",
            "severity": "critical" if ix.get("severity") == "high" else "warning",
            "text": f"Interaction ({ix.get('severity', '')}): {', '.join(ix.get('drugs', []))} — {ix.get('description', '')}",
        })
    return blocks


def _build_confirmation_blocks(data: dict, params: dict) -> list[dict]:
    """Build a confirmation block for critical tool results awaiting confirmation."""
    if data.get("status") != "awaiting_confirmation":
        return []
    cid = data.get("confirmation_id", "")
    tool_name = data.get("message", "").split("'")[1] if "'" in data.get("message", "") else "action"
    return [{
        "type": "confirmation",
        "confirmation_id": cid,
        "text": data.get("message", ""),
        "buttons": [
            {"label": "Confirm", "action": "confirm", "params": {"confirmation_id": cid}},
        ],
    }]


def _build_ecg_blocks(data: dict, params: dict) -> list[dict]:
    if "error" in data:
        return []
    leads = data.get("leads", {})
    if not leads:
        return []
    pid = data.get("patient_id", params.get("patient_id", ""))
    event_id = data.get("event_id", params.get("event_id", ""))
    return [{
        "type": "waveform",
        "title": f"ECG — {pid} ({event_id})",
        "sampling_rate_hz": data.get("sampling_rate_hz", 200),
        "duration_s": data.get("duration_s", 12),
        "leads": {name: samples for name, samples in leads.items()},
    }]


def _build_vitals_trend_blocks(data: dict, params: dict) -> list[dict]:
    """Build blocks for vitals trend analysis with EWS scoring."""
    if "error" in data:
        return [{"type": "text", "content": f"Error: {data['error']}"}]
    readings = data.get("readings", [])
    trend = data.get("trend", {})
    pid = data.get("patient_id", params.get("patient_id", ""))
    display = params.get("display", "chart")
    blocks: list[dict] = []

    # Data table — only in raw mode
    if display == "raw" and readings:
        rows = []
        for r in readings:
            rows.append([
                r.get("timestamp", "")[:16],
                str(r.get("heart_rate", "")),
                str(r.get("bp_systolic", "")),
                str(r.get("spo2", "")),
                str(r.get("temperature", "")),
                str(r.get("ews_score", "")),
            ])
        blocks.append({
            "type": "data_table",
            "title": f"Vitals Trend — {pid}",
            "tool": "get_vitals_trend",
            "columns": ["Time", "HR", "BP Sys", "SpO2", "Temp", "EWS"],
            "rows": rows,
        })

    # Chart block — only in chart mode, with all vitals series
    if display == "chart" and len(readings) >= 2:
        series = {
            "heart_rate": [
                {"t": r.get("timestamp", ""), "v": r.get("heart_rate")}
                for r in readings if r.get("heart_rate") is not None
            ],
            "bp_systolic": [
                {"t": r.get("timestamp", ""), "v": r.get("bp_systolic")}
                for r in readings if r.get("bp_systolic") is not None
            ],
            "spo2": [
                {"t": r.get("timestamp", ""), "v": r.get("spo2")}
                for r in readings if r.get("spo2") is not None
            ],
            "temperature": [
                {"t": r.get("timestamp", ""), "v": r.get("temperature")}
                for r in readings if r.get("temperature") is not None
            ],
        }
        blocks.append({
            "type": "chart",
            "chart_type": "line",
            "title": f"Vitals Trend — {pid}",
            "x_label": "Time",
            "y_label": "Value",
            "series": series,
        })

    # Trend summary text
    if trend:
        status = trend.get("patient_status", "unknown").upper()
        confidence = trend.get("confidence", "")
        interpretation = trend.get("clinical_interpretation", "")
        blocks.append({
            "type": "text",
            "content": f"**Trend: {status}** (confidence: {confidence})\n{interpretation}",
        })

    # Alert if deteriorating
    if trend.get("patient_status") == "deteriorating":
        severity = "critical" if trend.get("confidence") == "high" else "warning"
        blocks.append({
            "type": "alert",
            "severity": severity,
            "text": f"Patient {pid} is deteriorating. {trend.get('clinical_interpretation', '')}",
        })

    return blocks


def _build_report_blocks(data: dict, params: dict) -> list[dict]:
    """Build blocks for a radiology report, including image reference if available."""
    if "error" in data:
        return []
    pid = data.get("patient_id", params.get("patient_id", ""))
    study_id = data.get("study_id", params.get("study_id", ""))
    blocks: list[dict] = [{
        "type": "key_value",
        "title": f"Radiology Report — {study_id}",
        "items": [
            {"key": "Patient", "value": data.get("patient_name", pid)},
            {"key": "Modality", "value": data.get("modality", "")},
            {"key": "Date", "value": data.get("date", "")},
            {"key": "Description", "value": data.get("description", "")},
            {"key": "Referring Physician", "value": data.get("referring_physician", "")},
        ],
    }]
    # Image block — the gateway or rendering layer can resolve the URL
    if study_id:
        blocks.append({
            "type": "image",
            "url": f"/studies/{study_id}/image",
            "alt": f"{data.get('modality', 'Study')} — {data.get('description', study_id)}",
            "mime_type": "image/png",
        })
    return blocks


def _build_studies_blocks(data: dict, params: dict) -> list[dict]:
    if "error" in data:
        return []
    studies = data.get("studies", [])
    if not studies:
        return []
    pid = data.get("patient_id", params.get("patient_id", ""))
    rows = []
    for s in studies:
        rows.append([
            s.get("study_id", ""),
            s.get("modality", ""),
            s.get("date", ""),
            s.get("description", ""),
        ])
    return [{
        "type": "data_table",
        "title": f"Imaging Studies — {pid}",
        "tool": "get_studies",
        "columns": ["Study ID", "Modality", "Date", "Description"],
        "rows": rows,
    }]


def _build_ward_rounds_blocks(data: dict, params: dict) -> list[dict]:
    """Build per-patient key_value blocks for ward rounds."""
    if "error" in data:
        return []
    patients = data.get("patients", [])
    if not patients:
        return []
    ward_id = data.get("ward_id", params.get("ward_id", ""))
    blocks: list[dict] = []
    for p in patients:
        vitals = p.get("vitals", {})
        meds = p.get("medications", [])
        meds_summary = ", ".join(m.get("medication", "") for m in meds[:3]) or "None"
        scan = p.get("latest_scan")
        scan_desc = scan.get("description", "N/A") if scan else "None"
        news = p.get("news_score", 0)
        items = [
            {"key": "Bed", "value": p.get("bed", "N/A")},
            {"key": "NEWS", "value": str(news)},
            {"key": "HR", "value": str(vitals.get("heart_rate", ""))},
            {"key": "BP", "value": f"{vitals.get('bp_systolic', '')}/{vitals.get('bp_diastolic', '')}"},
            {"key": "SpO2", "value": str(vitals.get("spo2", ""))},
            {"key": "Temp", "value": str(vitals.get("temperature", ""))},
            {"key": "Doctor", "value": p.get("doctor", "N/A")},
            {"key": "Meds", "value": meds_summary},
            {"key": "Last Scan", "value": scan_desc},
        ]
        blocks.append({
            "type": "key_value",
            "title": f"{p.get('patient_id', '')} — Ward {ward_id}",
            "items": items,
        })
        if news >= 5:
            blocks.append({
                "type": "alert",
                "severity": "warning",
                "text": f"Patient {p.get('patient_id', '')} NEWS={news} — needs urgent review",
            })
    return blocks


def _build_resolve_bed_blocks(data: dict, params: dict) -> list[dict]:
    if "error" in data:
        return []
    return [{"type": "text", "content": f"Bed {data.get('bed_id', '')} → Patient {data.get('patient_id', '')}"}]


def _build_schedule_blocks(data: dict, params: dict) -> list[dict]:
    if "error" in data:
        return []
    items = [
        {"key": "ID", "value": data.get("appointment_id", "")},
        {"key": "Patient", "value": data.get("patient_id", "")},
        {"key": "Doctor", "value": data.get("doctor", "")},
        {"key": "When", "value": data.get("datetime", "")},
        {"key": "Status", "value": data.get("status", "")},
    ]
    return [{"type": "key_value", "title": "Appointment Scheduled", "items": items}]


def _build_reminder_blocks(data: dict, params: dict) -> list[dict]:
    if "error" in data:
        return []
    items = [
        {"key": "ID", "value": data.get("reminder_id", "")},
        {"key": "Message", "value": data.get("message", "")},
        {"key": "Fires at", "value": data.get("trigger_at", "")},
    ]
    return [{"type": "key_value", "title": "Reminder Set", "items": items}]


# ---------------------------------------------------------------------------
# Tool → builder mapping
# ---------------------------------------------------------------------------

TOOL_BLOCK_MAP: dict[str, callable] = {
    "get_vitals": _build_vitals_blocks,
    "get_vitals_history": _build_vitals_history_blocks,
    "get_vitals_trend": _build_vitals_trend_blocks,
    "get_lab_results": _build_lab_blocks,
    "get_medications": _build_medications_blocks,
    "get_allergies": _build_allergies_blocks,
    "get_patient": _build_patient_blocks,
    "get_blood_availability": _build_blood_availability_blocks,
    "get_ward_patients": _build_ward_patients_blocks,
    "check_drug_interactions": _build_drug_interactions_blocks,
    "get_studies": _build_studies_blocks,
    "get_event_ecg": _build_ecg_blocks,
    "get_latest_ecg": _build_ecg_blocks,
    "get_report": _build_report_blocks,
    "get_ward_rounds": _build_ward_rounds_blocks,
    "resolve_bed": _build_resolve_bed_blocks,
    "schedule_appointment": _build_schedule_blocks,
    "set_reminder": _build_reminder_blocks,
}
