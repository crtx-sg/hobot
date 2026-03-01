from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Synthetic Pharmacy", version="1.0.0")

# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

ORDERS: dict[str, dict] = {
    "RX-001": {
        "order_id": "RX-001",
        "patient_id": "P001",
        "medication": "Metformin",
        "dose": "500mg",
        "route": "PO",
        "frequency": "BID",
        "status": "active",
        "prescribed_at": "2026-02-25T08:00:00Z",
    },
    "RX-002": {
        "order_id": "RX-002",
        "patient_id": "P001",
        "medication": "Lisinopril",
        "dose": "10mg",
        "route": "PO",
        "frequency": "QD",
        "status": "active",
        "prescribed_at": "2026-02-25T08:05:00Z",
    },
    "RX-003": {
        "order_id": "RX-003",
        "patient_id": "P002",
        "medication": "Aspirin",
        "dose": "81mg",
        "route": "PO",
        "frequency": "QD",
        "status": "active",
        "prescribed_at": "2026-02-24T09:00:00Z",
    },
    "RX-004": {
        "order_id": "RX-004",
        "patient_id": "P002",
        "medication": "Atorvastatin",
        "dose": "20mg",
        "route": "PO",
        "frequency": "QHS",
        "status": "active",
        "prescribed_at": "2026-02-24T09:10:00Z",
    },
    "RX-005": {
        "order_id": "RX-005",
        "patient_id": "P003",
        "medication": "Amlodipine",
        "dose": "5mg",
        "route": "PO",
        "frequency": "QD",
        "status": "active",
        "prescribed_at": "2026-02-23T10:00:00Z",
    },
    "RX-006": {
        "order_id": "RX-006",
        "patient_id": "P003",
        "medication": "Metoprolol",
        "dose": "25mg",
        "route": "PO",
        "frequency": "BID",
        "status": "active",
        "prescribed_at": "2026-02-23T10:05:00Z",
    },
    "RX-007": {
        "order_id": "RX-007",
        "patient_id": "P004",
        "medication": "Omeprazole",
        "dose": "20mg",
        "route": "PO",
        "frequency": "QD",
        "status": "active",
        "prescribed_at": "2026-02-22T07:30:00Z",
    },
    "RX-008": {
        "order_id": "RX-008",
        "patient_id": "P004",
        "medication": "Sertraline",
        "dose": "50mg",
        "route": "PO",
        "frequency": "QD",
        "status": "active",
        "prescribed_at": "2026-02-22T07:35:00Z",
    },
    "RX-009": {
        "order_id": "RX-009",
        "patient_id": "P005",
        "medication": "Insulin Glargine",
        "dose": "20u",
        "route": "SubQ",
        "frequency": "QHS",
        "status": "active",
        "prescribed_at": "2026-02-21T06:00:00Z",
    },
    "RX-010": {
        "order_id": "RX-010",
        "patient_id": "P005",
        "medication": "Metformin",
        "dose": "1000mg",
        "route": "PO",
        "frequency": "BID",
        "status": "active",
        "prescribed_at": "2026-02-21T06:05:00Z",
    },
}

_next_order_num = len(ORDERS) + 1


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class DispenseRequest(BaseModel):
    patient_id: str
    medication: str
    dose: str
    route: str
    frequency: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/medications/{patient_id}")
def get_medications(patient_id: str):
    meds = [o for o in ORDERS.values() if o["patient_id"] == patient_id and o["status"] == "active"]
    if not meds:
        raise HTTPException(status_code=404, detail=f"No active medications for patient {patient_id}")
    return {"patient_id": patient_id, "medications": meds}


@app.get("/order/{order_id}")
def get_order(order_id: str):
    order = ORDERS.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
    return order


@app.post("/dispense", status_code=201)
def dispense(req: DispenseRequest):
    global _next_order_num
    order_id = f"RX-{_next_order_num:03d}"
    _next_order_num += 1

    order = {
        "order_id": order_id,
        "patient_id": req.patient_id,
        "medication": req.medication,
        "dose": req.dose,
        "route": req.route,
        "frequency": req.frequency,
        "status": "dispensed",
        "prescribed_at": datetime.now(timezone.utc).isoformat(),
    }
    ORDERS[order_id] = order
    return order
