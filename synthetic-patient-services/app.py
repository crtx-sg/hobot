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
