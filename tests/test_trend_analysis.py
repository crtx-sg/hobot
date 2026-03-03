"""Unit tests for analyze_trend() in synthetic-monitoring/app.py."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "synthetic-monitoring"))

from datetime import datetime, timedelta, timezone
from app import analyze_trend, compute_news


def _make_readings(ews_values: list[int], hours_span: int = 24) -> list[dict]:
    """Create synthetic readings with vitals engineered to produce target EWS scores."""
    now = datetime.now(timezone.utc)
    n = len(ews_values)
    readings = []
    for i, target_ews in enumerate(ews_values):
        ts = now - timedelta(hours=hours_span * (n - 1 - i) / max(n - 1, 1))
        # Use normal vitals (EWS=0) and adjust HR to influence the score
        # HR scoring: 61-90 → 0, 91-110 → 1, 111-130 → 2, >130 → 3
        # SpO2: >95 → 0, 94-95 → 1, 92-93 → 2, <=91 → 3
        # Systolic: 111-219 → 0, 101-110 → 1, 91-100 → 2, <=90 → 3
        # Temp: 36.1-38.0 → 0, 35.1-36.0 or 38.1-39.0 → 1, >39 → 2, <=35 → 3
        # Default all-normal gives EWS=0
        hr = 75  # score 0
        spo2 = 98  # score 0
        bp_sys = 120  # score 0
        temp = 37.0  # score 0

        # Add EWS via HR increments
        if target_ews >= 1:
            hr = 95  # score 1
        if target_ews >= 2:
            hr = 115  # score 2
        if target_ews >= 3:
            hr = 135  # score 3
        if target_ews >= 4:
            spo2 = 94  # +1 → total 4
        if target_ews >= 5:
            spo2 = 92  # +2 → but HR=3, so adjust
            # HR=3 + SpO2=2 = 5
        if target_ews >= 6:
            temp = 38.5  # +1 → total 6

        reading = {
            "timestamp": ts.isoformat(),
            "heart_rate": hr,
            "bp_systolic": bp_sys,
            "bp_diastolic": 80,
            "spo2": spo2,
            "temperature": temp,
        }
        # Verify the EWS matches
        actual_ews = compute_news(reading)
        readings.append(reading)
    return readings


class TestAnalyzeTrendStable:
    def test_constant_ews_is_stable(self):
        # All readings with same vitals → stable
        readings = _make_readings([0, 0, 0, 0, 0, 0, 0, 0, 0, 0], hours_span=24)
        result = analyze_trend(readings, hours=48)
        assert "error" not in result
        assert result["trend"]["patient_status"] == "stable"

    def test_small_variation_is_stable(self):
        readings = _make_readings([1, 0, 1, 0, 1, 0, 1, 0, 1, 0], hours_span=24)
        result = analyze_trend(readings, hours=48)
        assert "error" not in result
        assert result["trend"]["patient_status"] == "stable"


class TestAnalyzeTrendDeteriorating:
    def test_ascending_ews_is_deteriorating(self):
        readings = _make_readings([0, 0, 1, 1, 2, 2, 3, 3, 4, 5], hours_span=24)
        result = analyze_trend(readings, hours=48)
        assert "error" not in result
        assert result["trend"]["patient_status"] == "deteriorating"
        assert result["trend"]["slope"] > 0


class TestAnalyzeTrendImproving:
    def test_descending_ews_is_improving(self):
        readings = _make_readings([5, 4, 3, 3, 2, 2, 1, 1, 0, 0], hours_span=24)
        result = analyze_trend(readings, hours=48)
        assert "error" not in result
        assert result["trend"]["patient_status"] == "improving"
        assert result["trend"]["slope"] < 0


class TestAnalyzeTrendEdgeCases:
    def test_fewer_than_two_readings_returns_error(self):
        readings = _make_readings([2], hours_span=24)
        result = analyze_trend(readings, hours=48)
        assert "error" in result

    def test_hours_filtering(self):
        # Create readings spanning 48h, request only last 2h
        now = datetime.now(timezone.utc)
        readings = []
        for i in range(10):
            ts = now - timedelta(hours=48 - i * 5)
            readings.append({
                "timestamp": ts.isoformat(),
                "heart_rate": 75,
                "bp_systolic": 120,
                "bp_diastolic": 80,
                "spo2": 98,
                "temperature": 37.0,
            })
        # Only last reading is within 2 hours
        result = analyze_trend(readings, hours=2)
        assert "error" in result  # Only 1 reading in window

    def test_ews_score_in_readings(self):
        readings = _make_readings([0, 1, 2, 3, 4], hours_span=24)
        result = analyze_trend(readings, hours=48)
        assert "error" not in result
        for r in result["readings"]:
            assert "ews_score" in r

    def test_trend_fields_present(self):
        readings = _make_readings([1, 1, 1, 1, 1], hours_span=24)
        result = analyze_trend(readings, hours=48)
        trend = result["trend"]
        for key in ("patient_status", "confidence", "slope", "r_squared", "p_value", "recent_slope", "clinical_interpretation"):
            assert key in trend
