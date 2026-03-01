"""Generate synthetic HDF5 waveform/event files per patient."""

import os
import random
import uuid
from datetime import datetime, timedelta, timezone

import h5py
import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DATA_DIR = os.environ.get("HDF5_DATA_DIR", "/data/hdf5")
ECG_HZ = 200
PPG_HZ = 75
RESP_HZ = 33  # ~33.33 Hz
ECG_DURATION_S = 12
PPG_DURATION_S = 12
RESP_DURATION_S = 12

ECG_SAMPLES = ECG_HZ * ECG_DURATION_S          # 2400
PPG_SAMPLES = PPG_HZ * PPG_DURATION_S          # 900
RESP_SAMPLES = RESP_HZ * RESP_DURATION_S       # ~396

ECG_LEADS = ["ECG1", "ECG2", "ECG3", "aVR", "aVL", "aVF", "vVX"]

CONDITIONS = ["N", "ST", "SB", "VT", "AFIB"]
CONDITION_WEIGHTS = [0.40, 0.20, 0.15, 0.15, 0.10]

# Condition-specific HR ranges
CONDITION_HR = {
    "N":    (60, 100),
    "ST":   (101, 150),
    "SB":   (35, 55),
    "VT":   (140, 220),
    "AFIB": (80, 160),
}

EVENTS_PER_PATIENT = (8, 12)

# ---------------------------------------------------------------------------
# Waveform synthesis helpers
# ---------------------------------------------------------------------------

def _ecg_lead(hr: float, condition: str, n_samples: int, lead_idx: int) -> np.ndarray:
    """Generate a synthetic ECG-like waveform for one lead."""
    t = np.linspace(0, ECG_DURATION_S, n_samples, dtype=np.float32)
    freq = hr / 60.0  # beats per second

    # Base QRS-like composite
    qrs = np.sin(2 * np.pi * freq * t)
    # T-wave harmonic
    t_wave = 0.3 * np.sin(2 * np.pi * freq * 0.5 * t + np.pi / 4)
    # Lead-dependent phase offset
    phase = lead_idx * np.pi / 7
    sig = qrs * np.cos(phase) + t_wave * np.sin(phase)

    # Condition-specific modulation
    if condition == "ST":
        sig += 0.2 * np.ones_like(t)  # ST elevation
    elif condition == "VT":
        sig *= 1.5
        sig += 0.4 * np.sin(2 * np.pi * freq * 2 * t)
    elif condition == "AFIB":
        sig += 0.15 * np.random.randn(n_samples).astype(np.float32)  # irregular baseline
    elif condition == "SB":
        sig *= 0.8

    # Small noise
    sig += 0.02 * np.random.randn(n_samples).astype(np.float32)
    return sig


def _ppg_wave(hr: float, n_samples: int) -> np.ndarray:
    t = np.linspace(0, PPG_DURATION_S, n_samples, dtype=np.float32)
    freq = hr / 60.0
    sig = 0.6 * np.sin(2 * np.pi * freq * t) + 0.2 * np.sin(4 * np.pi * freq * t)
    sig += 0.01 * np.random.randn(n_samples).astype(np.float32)
    return sig


def _resp_wave(rr: float, n_samples: int) -> np.ndarray:
    t = np.linspace(0, RESP_DURATION_S, n_samples, dtype=np.float32)
    freq = rr / 60.0
    sig = np.sin(2 * np.pi * freq * t)
    sig += 0.02 * np.random.randn(n_samples).astype(np.float32)
    return sig


# ---------------------------------------------------------------------------
# Vitals generation per event
# ---------------------------------------------------------------------------

def _event_vitals(hr: int, condition: str) -> dict:
    """Generate a vitals snapshot for a single event."""
    if condition == "N":
        spo2 = random.randint(96, 100)
        systolic = random.randint(110, 135)
        diastolic = random.randint(65, 85)
        rr = random.randint(12, 20)
        temp = round(random.uniform(36.2, 37.5), 1)
    elif condition == "ST":
        spo2 = random.randint(93, 98)
        systolic = random.randint(130, 170)
        diastolic = random.randint(80, 100)
        rr = random.randint(18, 28)
        temp = round(random.uniform(36.5, 38.5), 1)
    elif condition == "SB":
        spo2 = random.randint(94, 99)
        systolic = random.randint(90, 115)
        diastolic = random.randint(55, 75)
        rr = random.randint(10, 16)
        temp = round(random.uniform(35.5, 37.0), 1)
    elif condition == "VT":
        spo2 = random.randint(85, 94)
        systolic = random.randint(70, 100)
        diastolic = random.randint(40, 65)
        rr = random.randint(22, 35)
        temp = round(random.uniform(36.0, 38.0), 1)
    else:  # AFIB
        spo2 = random.randint(92, 98)
        systolic = random.randint(100, 160)
        diastolic = random.randint(60, 95)
        rr = random.randint(14, 26)
        temp = round(random.uniform(36.3, 37.8), 1)

    return {
        "heart_rate": {"value": hr, "units": "bpm"},
        "pulse": {"value": hr + random.randint(-3, 3), "units": "bpm"},
        "spo2": {"value": spo2, "units": "%"},
        "systolic": {"value": systolic, "units": "mmHg"},
        "diastolic": {"value": diastolic, "units": "mmHg"},
        "resp_rate": {"value": rr, "units": "breaths/min"},
        "temperature": {"value": temp, "units": "°C"},
        "xl_posture": {"value": random.choice(["Supine", "Left", "Right", "Prone"]), "units": ""},
    }


# ---------------------------------------------------------------------------
# HDF5 file writer
# ---------------------------------------------------------------------------

def _write_patient_file(patient_id: str, filepath: str) -> list[dict]:
    """Write one HDF5 file and return event summaries for the index."""
    now = datetime.now(timezone.utc)
    n_events = random.randint(*EVENTS_PER_PATIENT)
    event_summaries = []

    with h5py.File(filepath, "w") as f:
        # Metadata group
        meta = f.create_group("metadata")
        meta.attrs["patient_id"] = patient_id
        meta.attrs["ecg_sampling_rate"] = ECG_HZ
        meta.attrs["ppg_sampling_rate"] = PPG_HZ
        meta.attrs["resp_sampling_rate"] = RESP_HZ
        meta.attrs["device_info"] = "SyntheticMonitor v1.0"
        meta.attrs["created_utc"] = now.isoformat()

        for i in range(n_events):
            event_id = f"event_{1001 + i}"
            # Spread events over last 24h
            offset_h = random.uniform(0, 24)
            event_ts = now - timedelta(hours=offset_h)
            condition = random.choices(CONDITIONS, weights=CONDITION_WEIGHTS, k=1)[0]
            hr_lo, hr_hi = CONDITION_HR[condition]
            hr = random.randint(hr_lo, hr_hi)

            eg = f.create_group(event_id)
            eg.attrs["condition"] = condition
            eg.attrs["heart_rate"] = hr
            eg.attrs["event_timestamp"] = event_ts.isoformat()

            # Timestamp / UUID datasets
            eg.create_dataset("timestamp", data=event_ts.isoformat())
            eg.create_dataset("uuid", data=str(uuid.uuid4()))

            # ECG group — 7 leads
            ecg_g = eg.create_group("ecg")
            for li, lead in enumerate(ECG_LEADS):
                ecg_g.create_dataset(lead, data=_ecg_lead(hr, condition, ECG_SAMPLES, li))

            # PPG
            eg.create_dataset("ppg", data=_ppg_wave(hr, PPG_SAMPLES))

            # Resp
            rr = random.randint(10, 30)
            eg.create_dataset("resp", data=_resp_wave(rr, RESP_SAMPLES))

            # Vitals group
            vitals = _event_vitals(hr, condition)
            vg = eg.create_group("vitals")
            for vname, vdata in vitals.items():
                ds = vg.create_group(vname)
                # Store value as string to handle mixed types
                ds.attrs["value"] = str(vdata["value"])
                ds.attrs["units"] = vdata["units"]
                ds.attrs["timestamp"] = event_ts.isoformat()

            event_summaries.append({
                "event_id": event_id,
                "timestamp": event_ts.isoformat(),
                "condition": condition,
                "heart_rate": hr,
            })

    return event_summaries


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_all(patient_ids: list[str]) -> dict[str, list[dict]]:
    """Generate HDF5 files for all patients, return event index."""
    os.makedirs(DATA_DIR, exist_ok=True)
    index: dict[str, list[dict]] = {}

    for pid in patient_ids:
        filename = f"{pid}_2026-02.h5"
        filepath = os.path.join(DATA_DIR, filename)
        summaries = _write_patient_file(pid, filepath)
        # Sort by timestamp descending (most recent first)
        summaries.sort(key=lambda e: e["timestamp"], reverse=True)
        index[pid] = summaries

    return index
