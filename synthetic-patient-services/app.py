from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="Synthetic Patient Services")

# Auto-increment counter
_next_id = 6

# Seed data
requests_db: dict[str, dict] = {
    "SVC-001": {
        "id": "SVC-001",
        "type": "housekeeping",
        "room": "401A",
        "request_type": "terminal_clean",
        "priority": "normal",
        "status": "completed",
    },
    "SVC-002": {
        "id": "SVC-002",
        "type": "diet_order",
        "patient_id": "P001",
        "diet_type": "regular",
        "meal": "lunch",
        "restrictions": [],
        "status": "pending",
    },
    "SVC-003": {
        "id": "SVC-003",
        "type": "transport",
        "patient_id": "P003",
        "from_location": "ward_3B",
        "to_location": "radiology",
        "transport_type": "wheelchair",
        "priority": "normal",
        "status": "in_progress",
    },
    "SVC-004": {
        "id": "SVC-004",
        "type": "housekeeping",
        "room": "302B",
        "request_type": "spill_cleanup",
        "priority": "normal",
        "status": "pending",
    },
    "SVC-005": {
        "id": "SVC-005",
        "type": "transport",
        "patient_id": "P005",
        "from_location": "ER",
        "to_location": "ICU",
        "transport_type": "stretcher",
        "priority": "normal",
        "status": "completed",
    },
}


def _generate_id() -> str:
    global _next_id
    request_id = f"SVC-{_next_id:03d}"
    _next_id += 1
    return request_id


# --- Request models ---

class HousekeepingRequest(BaseModel):
    room: str
    request_type: str
    priority: str = "normal"


class DietOrderRequest(BaseModel):
    patient_id: str
    diet_type: str
    meal: str
    restrictions: list[str] = []


class TransportRequest(BaseModel):
    patient_id: str
    from_location: str
    to_location: str
    transport_type: str
    priority: str = "normal"


# --- Endpoints ---

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/housekeeping")
def create_housekeeping(req: HousekeepingRequest):
    request_id = _generate_id()
    record = {
        "id": request_id,
        "type": "housekeeping",
        "room": req.room,
        "request_type": req.request_type,
        "priority": req.priority,
        "status": "pending",
    }
    requests_db[request_id] = record
    return record


@app.post("/diet-order")
def create_diet_order(req: DietOrderRequest):
    request_id = _generate_id()
    record = {
        "id": request_id,
        "type": "diet_order",
        "patient_id": req.patient_id,
        "diet_type": req.diet_type,
        "meal": req.meal,
        "restrictions": req.restrictions,
        "status": "pending",
    }
    requests_db[request_id] = record
    return record


@app.post("/transport")
def create_transport(req: TransportRequest):
    request_id = _generate_id()
    record = {
        "id": request_id,
        "type": "transport",
        "patient_id": req.patient_id,
        "from_location": req.from_location,
        "to_location": req.to_location,
        "transport_type": req.transport_type,
        "priority": req.priority,
        "status": "pending",
    }
    requests_db[request_id] = record
    return record


@app.get("/request/{request_id}")
def get_request(request_id: str):
    if request_id not in requests_db:
        raise HTTPException(status_code=404, detail="Request not found")
    return requests_db[request_id]


# ---------------------------------------------------------------------------
# Appointments
# ---------------------------------------------------------------------------

_next_appt_id = 1
appointments_db: dict[str, dict] = {}


class AppointmentRequest(BaseModel):
    patient_id: str
    doctor: str
    datetime: str
    notes: str = ""


@app.post("/appointment")
def create_appointment(req: AppointmentRequest):
    global _next_appt_id
    appt_id = f"APPT-{_next_appt_id:04d}"
    _next_appt_id += 1
    record = {
        "appointment_id": appt_id,
        "patient_id": req.patient_id,
        "doctor": req.doctor,
        "datetime": req.datetime,
        "notes": req.notes,
        "status": "scheduled",
    }
    appointments_db[appt_id] = record
    return record


@app.get("/appointment/{appointment_id}")
def get_appointment(appointment_id: str):
    if appointment_id not in appointments_db:
        raise HTTPException(status_code=404, detail="Appointment not found")
    return appointments_db[appointment_id]


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

_next_rem_id = 1
reminders_db: dict[str, dict] = {}


class ReminderRequest(BaseModel):
    message: str
    delay_minutes: int = 60
    session_id: str = ""
    channel: str = ""


@app.post("/reminder")
def create_reminder(req: ReminderRequest):
    global _next_rem_id
    rem_id = f"REM-{_next_rem_id:04d}"
    _next_rem_id += 1
    trigger_at = datetime.now(timezone.utc) + timedelta(minutes=req.delay_minutes)
    record = {
        "reminder_id": rem_id,
        "session_id": req.session_id,
        "channel": req.channel,
        "message": req.message,
        "trigger_at": trigger_at.isoformat(),
        "status": "pending",
    }
    reminders_db[rem_id] = record
    return {
        "reminder_id": rem_id,
        "trigger_at": trigger_at.isoformat(),
        "message": req.message,
        "status": "scheduled",
    }


@app.get("/appointments")
def list_appointments(patient_id: str = ""):
    """Return appointments for a patient (or all if no patient_id)."""
    if not patient_id:
        return {"appointments": list(appointments_db.values())}
    filtered = [a for a in appointments_db.values() if a.get("patient_id") == patient_id]
    return {"appointments": filtered}


@app.get("/reminders")
def list_reminders(patient_id: str = ""):
    """Return reminders for a patient (or all if no patient_id).

    Since reminders are session-scoped (not patient-scoped), this returns all
    reminders when no patient_id filter is given.  When patient_id is supplied
    we still return all — the gateway can filter further if needed.
    """
    items = list(reminders_db.values())
    return {"reminders": items}


@app.get("/reminders/due")
def get_due_reminders():
    """Return all pending reminders whose trigger_at has passed, mark them fired."""
    now = datetime.now(timezone.utc)
    due = []
    for rem_id, rem in reminders_db.items():
        if rem["status"] != "pending":
            continue
        trigger_at = datetime.fromisoformat(rem["trigger_at"])
        if trigger_at.tzinfo is None:
            trigger_at = trigger_at.replace(tzinfo=timezone.utc)
        if now >= trigger_at:
            rem["status"] = "fired"
            due.append(rem)
    return {"reminders": due}
