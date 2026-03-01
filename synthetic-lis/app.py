from datetime import datetime, timedelta
from typing import Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Synthetic LIS", version="0.1.0")

# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

_now = datetime(2026, 2, 25, 8, 0, 0)


def _ts(offset_hours: int = 0) -> str:
    return (_now + timedelta(hours=offset_hours)).isoformat()


def _cbc(wbc, rbc, hgb, hct, plt, order_id, patient_id, hours_offset):
    return {
        "order_id": order_id,
        "patient_id": patient_id,
        "test_type": "CBC",
        "status": "completed",
        "collected_at": _ts(hours_offset),
        "results": {
            "WBC": {"value": wbc, "unit": "x10^3/uL", "ref_range": "4.0-11.0"},
            "RBC": {"value": rbc, "unit": "x10^6/uL", "ref_range": "4.5-5.5"},
            "Hgb": {"value": hgb, "unit": "g/dL", "ref_range": "12.0-17.0"},
            "Hct": {"value": hct, "unit": "%", "ref_range": "36-50"},
            "Platelets": {"value": plt, "unit": "x10^3/uL", "ref_range": "150-400"},
        },
    }


def _bmp(na, k, cl, co2, bun, cr, glu, order_id, patient_id, hours_offset):
    return {
        "order_id": order_id,
        "patient_id": patient_id,
        "test_type": "BMP",
        "status": "completed",
        "collected_at": _ts(hours_offset),
        "results": {
            "Na": {"value": na, "unit": "mEq/L", "ref_range": "136-145"},
            "K": {"value": k, "unit": "mEq/L", "ref_range": "3.5-5.0"},
            "Cl": {"value": cl, "unit": "mEq/L", "ref_range": "98-106"},
            "CO2": {"value": co2, "unit": "mEq/L", "ref_range": "23-29"},
            "BUN": {"value": bun, "unit": "mg/dL", "ref_range": "7-20"},
            "Cr": {"value": cr, "unit": "mg/dL", "ref_range": "0.7-1.3"},
            "Glucose": {"value": glu, "unit": "mg/dL", "ref_range": "70-100"},
        },
    }


def _lft(alt, ast, alp, bili, order_id, patient_id, hours_offset):
    return {
        "order_id": order_id,
        "patient_id": patient_id,
        "test_type": "LFT",
        "status": "completed",
        "collected_at": _ts(hours_offset),
        "results": {
            "ALT": {"value": alt, "unit": "U/L", "ref_range": "7-56"},
            "AST": {"value": ast, "unit": "U/L", "ref_range": "10-40"},
            "ALP": {"value": alp, "unit": "U/L", "ref_range": "44-147"},
            "Bilirubin": {"value": bili, "unit": "mg/dL", "ref_range": "0.1-1.2"},
        },
    }


# fmt: off
LABS: dict[str, dict] = {}

_seed = [
    # Patient P001
    _cbc(6.8,  4.9, 14.2, 42.1, 245, "LAB-001", "P001", 0),
    _bmp(140,  4.2, 102,  25,   14,  0.9, 88,  "LAB-002", "P001", 1),
    _lft(22,   28,  72,   0.8,  "LAB-003", "P001", 2),
    # Patient P002
    _cbc(8.1,  5.1, 15.6, 46.3, 310, "LAB-004", "P002", 0),
    _bmp(138,  4.5, 100,  26,   12,  1.0, 92,  "LAB-005", "P002", 1),
    _lft(18,   24,  65,   0.6,  "LAB-006", "P002", 2),
    # Patient P003
    _cbc(5.2,  4.7, 13.1, 39.0, 198, "LAB-007", "P003", 0),
    _bmp(142,  3.8, 104,  27,   16,  1.1, 78,  "LAB-008", "P003", 1),
    _lft(32,   35,  110,  1.0,  "LAB-009", "P003", 2),
    # Patient P004
    _cbc(7.4,  5.3, 16.0, 48.2, 275, "LAB-010", "P004", 0),
    _bmp(136,  4.8, 99,   24,   10,  0.8, 95,  "LAB-011", "P004", 1),
    _lft(15,   19,  55,   0.4,  "LAB-012", "P004", 2),
    # Patient P005
    _cbc(9.5,  4.6, 12.8, 37.5, 180, "LAB-013", "P005", 0),
    _bmp(144,  3.6, 105,  28,   18,  1.2, 82,  "LAB-014", "P005", 1),
    _lft(45,   38,  130,  1.1,  "LAB-015", "P005", 2),
]
# fmt: on

for _lab in _seed:
    LABS[_lab["order_id"]] = _lab

_order_counter = len(_seed)

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class LabOrderRequest(BaseModel):
    patient_id: str
    test_type: str
    priority: Optional[str] = "routine"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/labs/{patient_id}")
def get_labs_for_patient(patient_id: str):
    results = [lab for lab in LABS.values() if lab["patient_id"] == patient_id]
    if not results:
        raise HTTPException(status_code=404, detail=f"No labs found for patient {patient_id}")
    return {"patient_id": patient_id, "labs": results}


@app.get("/lab/{order_id}")
def get_lab_order(order_id: str):
    lab = LABS.get(order_id)
    if not lab:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
    return lab


@app.post("/lab/order", status_code=201)
def create_lab_order(req: LabOrderRequest):
    global _order_counter
    _order_counter += 1
    order_id = f"LAB-{_order_counter:03d}"
    order = {
        "order_id": order_id,
        "patient_id": req.patient_id,
        "test_type": req.test_type,
        "priority": req.priority,
        "status": "pending",
        "collected_at": None,
        "results": None,
    }
    LABS[order_id] = order
    return order
