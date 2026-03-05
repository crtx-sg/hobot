import random
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import h5py
import numpy as np
from scipy.stats import linregress
from fastapi import FastAPI, HTTPException, Query

from hdf5_generator import DATA_DIR, ECG_LEADS, generate_all

# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

PATIENT_IDS = ["P001", "P002", "P003", "P004", "P005"]
NUM_READINGS = 24


def _random_vitals() -> dict:
    return {
        "heart_rate": random.randint(60, 100),
        "bp_systolic": random.randint(100, 140),
        "bp_diastolic": random.randint(60, 90),
        "spo2": random.randint(94, 100),
        "temperature": round(random.uniform(36.5, 38.0), 1),
    }


def _generate_history(n: int) -> list[dict]:
    now = datetime.now(timezone.utc)
    readings = []
    for i in range(n):
        ts = now - timedelta(hours=24 * (n - 1 - i) / (n - 1))
        entry = {
            "timestamp": ts.isoformat(),
            **_random_vitals(),
        }
        readings.append(entry)
    return readings


def _generate_deteriorating(n: int) -> list[dict]:
    """Generate deteriorating vitals: HR rising, SpO2 dropping, temp rising."""
    now = datetime.now(timezone.utc)
    readings = []
    for i in range(n):
        frac = i / max(n - 1, 1)
        ts = now - timedelta(hours=24 * (n - 1 - i) / max(n - 1, 1))
        readings.append({
            "timestamp": ts.isoformat(),
            "heart_rate": int(70 + 40 * frac + random.gauss(0, 2)),
            "bp_systolic": random.randint(100, 140),
            "bp_diastolic": random.randint(60, 90),
            "spo2": int(98 - 6 * frac + random.gauss(0, 0.5)),
            "temperature": round(37.0 + 2.0 * frac + random.gauss(0, 0.1), 1),
        })
    return readings


def _generate_improving(n: int) -> list[dict]:
    """Generate improving vitals: HR falling, SpO2 rising."""
    now = datetime.now(timezone.utc)
    readings = []
    for i in range(n):
        frac = i / max(n - 1, 1)
        ts = now - timedelta(hours=24 * (n - 1 - i) / max(n - 1, 1))
        readings.append({
            "timestamp": ts.isoformat(),
            "heart_rate": int(105 - 30 * frac + random.gauss(0, 2)),
            "bp_systolic": random.randint(100, 140),
            "bp_diastolic": random.randint(60, 90),
            "spo2": int(93 + 5 * frac + random.gauss(0, 0.5)),
            "temperature": round(random.uniform(36.5, 37.5), 1),
        })
    return readings


# Clinical scenario map: patient_id -> generator
_SCENARIO_GENERATORS: dict[str, callable] = {
    "P003": _generate_deteriorating,
    "P005": _generate_improving,
}

# In-memory store: patient_id -> list of readings (oldest first)
VITALS_DB: dict[str, list[dict]] = {
    pid: _SCENARIO_GENERATORS.get(pid, _generate_history)(NUM_READINGS)
    for pid in PATIENT_IDS
}

# ---------------------------------------------------------------------------
# Ward / Doctor mappings
# ---------------------------------------------------------------------------

WARD_MAP: dict[str, list[str]] = {
    "ICU-A": ["P001", "P002"],
    "ICU-B": ["P003"],
    "CARDIAC": ["P004", "P005"],
}

DOCTOR_MAP: dict[str, list[str]] = {
    "DR-SMITH": ["P001", "P003"],
    "DR-JONES": ["P002", "P004"],
    "DR-PATEL": ["P005"],
}

# Bed mapping (static seed data)
BED_MAP: dict[str, str] = {
    "BED1": "P001", "BED2": "P002", "BED3": "P003",
    "BED4": "P004", "BED5": "P005",
}
BED_PATIENT: dict[str, str] = {v: k for k, v in BED_MAP.items()}

# Reverse maps
PATIENT_WARD: dict[str, str] = {}
for ward, pids in WARD_MAP.items():
    for pid in pids:
        PATIENT_WARD[pid] = ward

PATIENT_DOCTOR: dict[str, str] = {}
for doc, pids in DOCTOR_MAP.items():
    for pid in pids:
        PATIENT_DOCTOR[pid] = doc

# ---------------------------------------------------------------------------
# NEWS2 (simplified) scoring
# ---------------------------------------------------------------------------

def _news_hr(hr: int) -> int:
    if hr <= 40:
        return 3
    if hr <= 50:
        return 2
    if hr <= 60:
        return 1
    if hr <= 90:
        return 0
    if hr <= 110:
        return 1
    if hr <= 130:
        return 2
    return 3


def _news_spo2(spo2: int) -> int:
    if spo2 <= 91:
        return 3
    if spo2 <= 93:
        return 2
    if spo2 <= 95:
        return 1
    return 0


def _news_systolic(sys: int) -> int:
    if sys <= 90:
        return 3
    if sys <= 100:
        return 2
    if sys <= 110:
        return 1
    if sys <= 219:
        return 0
    return 3


def _news_temp(temp: float) -> int:
    if temp <= 35.0:
        return 3
    if temp <= 36.0:
        return 1
    if temp <= 38.0:
        return 0
    if temp <= 39.0:
        return 1
    return 2


def compute_news(vitals: dict) -> int:
    """Compute simplified NEWS2 score from a vitals dict."""
    score = 0
    score += _news_hr(vitals.get("heart_rate", 75))
    score += _news_spo2(vitals.get("spo2", 98))
    score += _news_systolic(vitals.get("bp_systolic", 120))
    score += _news_temp(vitals.get("temperature", 37.0))
    return score


# ---------------------------------------------------------------------------
# HDF5 event index (populated at startup)
# ---------------------------------------------------------------------------

EVENT_INDEX: dict[str, list[dict]] = {}


# ---------------------------------------------------------------------------
# Lifespan — generate HDF5 data on startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global EVENT_INDEX
    EVENT_INDEX = generate_all(PATIENT_IDS)
    yield


app = FastAPI(title="Synthetic Monitoring Service", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Existing endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/vitals/{patient_id}")
def get_latest_vitals(patient_id: str):
    history = VITALS_DB.get(patient_id)
    if history is None:
        raise HTTPException(status_code=404, detail=f"Patient {patient_id} not found")
    return {"patient_id": patient_id, **history[-1]}


@app.get("/vitals/{patient_id}/history")
def get_vitals_history(patient_id: str):
    history = VITALS_DB.get(patient_id)
    if history is None:
        raise HTTPException(status_code=404, detail=f"Patient {patient_id} not found")
    return {"patient_id": patient_id, "readings": history}


# ---------------------------------------------------------------------------
# Vitals trend analysis
# ---------------------------------------------------------------------------

def analyze_trend(readings: list[dict], hours: int = 24) -> dict:
    """Filter readings by time window, compute EWS per reading, run linear regression."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    filtered = []
    for r in readings:
        ts = datetime.fromisoformat(r["timestamp"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            filtered.append({**r, "_ts": ts})

    if len(filtered) < 2:
        return {"error": f"Need at least 2 readings in the last {hours}h, got {len(filtered)}"}

    # Compute EWS per reading
    scored_readings = []
    ews_scores = []
    for r in filtered:
        ews = compute_news(r)
        entry = {k: v for k, v in r.items() if k != "_ts"}
        entry["ews_score"] = ews
        scored_readings.append(entry)
        ews_scores.append(ews)

    # Hours elapsed from first reading for regression x-axis
    t0 = filtered[0]["_ts"]
    hours_elapsed = [(r["_ts"] - t0).total_seconds() / 3600.0 for r in filtered]

    # Linear regression on EWS scores
    result = linregress(hours_elapsed, ews_scores)
    slope, r_squared, p_value = result.slope, result.rvalue ** 2, result.pvalue

    # Recent slope from last 3 readings
    recent_slope = 0.0
    if len(ews_scores) >= 3:
        recent_x = hours_elapsed[-3:]
        recent_y = ews_scores[-3:]
        recent_result = linregress(recent_x, recent_y)
        recent_slope = recent_result.slope

    # Classify trend
    if slope > 0.1 and p_value < 0.05:
        status = "deteriorating"
    elif slope < -0.1 and p_value < 0.05:
        status = "improving"
    else:
        status = "stable"

    confidence = "high" if r_squared > 0.5 and p_value < 0.05 else "low"

    avg_ews = round(sum(ews_scores) / len(ews_scores), 1)
    latest_ews = ews_scores[-1]
    interpretation = f"Average EWS: {avg_ews}, Latest: {latest_ews}. EWS {status}."

    return {
        "readings": scored_readings,
        "trend": {
            "patient_status": status,
            "confidence": confidence,
            "slope": round(slope, 4),
            "r_squared": round(r_squared, 4),
            "p_value": round(p_value, 4),
            "recent_slope": round(recent_slope, 4),
            "clinical_interpretation": interpretation,
        },
    }


@app.get("/vitals/{patient_id}/trend")
def get_vitals_trend(patient_id: str, hours: int = Query(default=24, ge=1, le=168)):
    history = VITALS_DB.get(patient_id)
    if history is None:
        raise HTTPException(status_code=404, detail=f"Patient {patient_id} not found")
    result = analyze_trend(history, hours)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {"patient_id": patient_id, "hours": hours, **result}


# ---------------------------------------------------------------------------
# Ward / Doctor endpoints
# ---------------------------------------------------------------------------

@app.get("/wards")
def list_wards():
    return [
        {"ward_id": wid, "patient_count": len(pids)}
        for wid, pids in WARD_MAP.items()
    ]


@app.get("/doctors")
def list_doctors():
    return [
        {"doctor_id": did, "patient_count": len(pids)}
        for did, pids in DOCTOR_MAP.items()
    ]


def _patient_summary(patient_id: str) -> dict:
    """Build a patient summary with latest vitals and NEWS score."""
    history = VITALS_DB.get(patient_id)
    if not history:
        return {"patient_id": patient_id, "vitals": None, "news_score": 0}
    latest = history[-1]
    return {
        "patient_id": patient_id,
        "bed": BED_PATIENT.get(patient_id),
        "ward": PATIENT_WARD.get(patient_id),
        "doctor": PATIENT_DOCTOR.get(patient_id),
        "vitals": latest,
        "news_score": compute_news(latest),
    }


@app.get("/ward/{ward_id}/patients")
def get_ward_patients(ward_id: str):
    pids = WARD_MAP.get(ward_id)
    if pids is None:
        raise HTTPException(status_code=404, detail=f"Ward {ward_id} not found")
    patients = [_patient_summary(pid) for pid in pids]
    patients.sort(key=lambda p: p["news_score"], reverse=True)
    return {"ward_id": ward_id, "patients": patients}


@app.get("/doctor/{doctor_id}/patients")
def get_doctor_patients(doctor_id: str):
    pids = DOCTOR_MAP.get(doctor_id)
    if pids is None:
        raise HTTPException(status_code=404, detail=f"Doctor {doctor_id} not found")
    patients = [_patient_summary(pid) for pid in pids]
    patients.sort(key=lambda p: p["news_score"], reverse=True)
    return {"doctor_id": doctor_id, "patients": patients}


# ---------------------------------------------------------------------------
# Bed / Rounds endpoints
# ---------------------------------------------------------------------------

@app.get("/bed/{bed_id}/patient")
def get_bed_patient(bed_id: str):
    bed_key = bed_id.upper().replace(" ", "")
    pid = BED_MAP.get(bed_key)
    if pid is None:
        raise HTTPException(status_code=404, detail=f"Bed {bed_id} not found")
    return {"bed_id": bed_key, "patient_id": pid}


@app.get("/ward/{ward_id}/rounds")
def get_ward_rounds(ward_id: str):
    pids = WARD_MAP.get(ward_id)
    if pids is None:
        raise HTTPException(status_code=404, detail=f"Ward {ward_id} not found")
    now = datetime.now(timezone.utc)
    cutoff_4h = now - timedelta(hours=4)
    patients = []
    for pid in pids:
        summary = _patient_summary(pid)
        history = VITALS_DB.get(pid, [])
        vitals_4h = []
        for r in history:
            ts = datetime.fromisoformat(r["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff_4h:
                vitals_4h.append(r)
        summary["vitals_4h"] = vitals_4h
        patients.append(summary)
    patients.sort(key=lambda p: p["news_score"], reverse=True)
    return {"ward_id": ward_id, "patients": patients}


# ---------------------------------------------------------------------------
# Event endpoints (backed by HDF5)
# ---------------------------------------------------------------------------

@app.get("/events/{patient_id}")
def get_patient_events(patient_id: str, hours: int = Query(default=24, ge=1, le=168)):
    events = EVENT_INDEX.get(patient_id)
    if events is None:
        raise HTTPException(status_code=404, detail=f"Patient {patient_id} not found")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    filtered = [
        e for e in events
        if datetime.fromisoformat(e["timestamp"]) >= cutoff
    ]
    return {"patient_id": patient_id, "hours": hours, "events": filtered}


def _hdf5_path(patient_id: str) -> str:
    import os
    return os.path.join(DATA_DIR, f"{patient_id}_2026-02.h5")


def _read_event_vitals(patient_id: str, event_id: str) -> dict:
    """Read vitals from an HDF5 event group."""
    path = _hdf5_path(patient_id)
    try:
        with h5py.File(path, "r") as f:
            if event_id not in f:
                raise HTTPException(status_code=404, detail=f"Event {event_id} not found")
            eg = f[event_id]
            vitals = {}
            vg = eg["vitals"]
            for vname in vg:
                ds = vg[vname]
                vitals[vname] = {
                    "value": ds.attrs["value"],
                    "units": ds.attrs["units"],
                    "timestamp": ds.attrs["timestamp"],
                }
            result = {
                "patient_id": patient_id,
                "event_id": event_id,
                "condition": eg.attrs["condition"],
                "timestamp": eg.attrs["event_timestamp"],
                "vitals": vitals,
            }
            return result
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"HDF5 file not found for {patient_id}")


def _read_event_ecg(patient_id: str, event_id: str) -> dict:
    """Read 7-lead ECG arrays from an HDF5 event group."""
    path = _hdf5_path(patient_id)
    try:
        with h5py.File(path, "r") as f:
            if event_id not in f:
                raise HTTPException(status_code=404, detail=f"Event {event_id} not found")
            eg = f[event_id]
            ecg_g = eg["ecg"]
            leads = {}
            for lead in ECG_LEADS:
                leads[lead] = ecg_g[lead][:].tolist()
            return {
                "patient_id": patient_id,
                "event_id": event_id,
                "condition": eg.attrs["condition"],
                "sampling_rate_hz": 200,
                "duration_s": 12,
                "samples_per_lead": 2400,
                "leads": leads,
            }
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"HDF5 file not found for {patient_id}")


@app.get("/ecg/{patient_id}/latest")
def get_latest_ecg(patient_id: str):
    """Return the most recent ECG for a patient (no event_id needed)."""
    events = EVENT_INDEX.get(patient_id)
    if not events:
        raise HTTPException(status_code=404, detail=f"Patient {patient_id} not found")
    # Sort by timestamp descending, take the first
    sorted_events = sorted(events, key=lambda e: e["timestamp"], reverse=True)
    latest = sorted_events[0]
    return _read_event_ecg(patient_id, latest["event_id"])


@app.get("/events/{patient_id}/{event_id}/vitals")
def get_event_vitals(patient_id: str, event_id: str):
    return _read_event_vitals(patient_id, event_id)


@app.get("/events/{patient_id}/{event_id}/ecg")
def get_event_ecg(patient_id: str, event_id: str):
    return _read_event_ecg(patient_id, event_id)
