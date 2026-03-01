from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import uuid

app = FastAPI(title="Synthetic Blood Bank", version="1.0.0")

# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

blood_inventory: dict[str, int] = {
    "A+": 12,
    "A-": 4,
    "B+": 8,
    "B-": 3,
    "AB+": 5,
    "AB-": 2,
    "O+": 15,
    "O-": 6,
}

crossmatch_requests: dict[str, dict] = {
    "XM-001": {
        "request_id": "XM-001",
        "patient_id": "PAT-1001",
        "blood_type": "A+",
        "units": 2,
        "priority": "routine",
        "status": "pending",
        "created_at": "2026-02-27T08:00:00Z",
    },
    "XM-002": {
        "request_id": "XM-002",
        "patient_id": "PAT-1002",
        "blood_type": "O-",
        "units": 1,
        "priority": "urgent",
        "status": "completed",
        "created_at": "2026-02-27T07:30:00Z",
    },
    "XM-003": {
        "request_id": "XM-003",
        "patient_id": "PAT-1003",
        "blood_type": "B+",
        "units": 3,
        "priority": "stat",
        "status": "in_progress",
        "created_at": "2026-02-27T09:15:00Z",
    },
}

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class CrossmatchCreate(BaseModel):
    patient_id: str
    blood_type: str
    units: int
    priority: str = "routine"

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/availability")
def availability():
    return {"inventory": blood_inventory}


@app.get("/crossmatch/{request_id}")
def get_crossmatch(request_id: str):
    if request_id not in crossmatch_requests:
        raise HTTPException(status_code=404, detail="Crossmatch request not found")
    return crossmatch_requests[request_id]


@app.post("/crossmatch", status_code=201)
def create_crossmatch(body: CrossmatchCreate):
    request_id = f"XM-{uuid.uuid4().hex[:6].upper()}"
    record = {
        "request_id": request_id,
        "patient_id": body.patient_id,
        "blood_type": body.blood_type,
        "units": body.units,
        "priority": body.priority,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    crossmatch_requests[request_id] = record
    return record
