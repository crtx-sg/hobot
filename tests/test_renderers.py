"""Unit tests for nanobot/renderers.py — server-side image rendering."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nanobot"))

from renderers import render_chart, render_waveform, render_table

PNG_HEADER = b"\x89PNG"


class TestRenderChart:
    def test_single_series(self):
        block = {
            "type": "chart",
            "chart_type": "line",
            "title": "Heart Rate Trend",
            "x_label": "Time",
            "y_label": "BPM",
            "series": {
                "heart_rate": [
                    {"t": "2026-02-25T08:00:00", "v": 72},
                    {"t": "2026-02-25T09:00:00", "v": 78},
                    {"t": "2026-02-25T10:00:00", "v": 75},
                ],
            },
        }
        result = render_chart(block)
        assert isinstance(result, bytes)
        assert result[:4] == PNG_HEADER
        assert len(result) > 1000

    def test_multi_series(self):
        block = {
            "type": "chart",
            "chart_type": "line",
            "title": "Vitals Trend",
            "x_label": "Time",
            "y_label": "Value",
            "series": {
                "heart_rate": [
                    {"t": "2026-02-25T08:00:00", "v": 72},
                    {"t": "2026-02-25T09:00:00", "v": 78},
                ],
                "spo2": [
                    {"t": "2026-02-25T08:00:00", "v": 97},
                    {"t": "2026-02-25T09:00:00", "v": 98},
                ],
                "bp_systolic": [
                    {"t": "2026-02-25T08:00:00", "v": 120},
                    {"t": "2026-02-25T09:00:00", "v": 118},
                ],
            },
        }
        result = render_chart(block)
        assert result[:4] == PNG_HEADER

    def test_empty_series(self):
        block = {
            "type": "chart",
            "title": "Empty Chart",
            "series": {},
        }
        result = render_chart(block)
        assert isinstance(result, bytes)
        assert result[:4] == PNG_HEADER

    def test_series_with_none_values(self):
        block = {
            "type": "chart",
            "title": "Sparse Data",
            "series": {
                "metric": [
                    {"t": "2026-02-25T08:00:00", "v": 72},
                    {"t": "2026-02-25T09:00:00", "v": None},
                    {"t": "2026-02-25T10:00:00", "v": 75},
                ],
            },
        }
        result = render_chart(block)
        assert result[:4] == PNG_HEADER


class TestRenderWaveform:
    def _make_lead_data(self, n_leads: int, n_samples: int = 500):
        import math
        leads = {}
        names = ["I", "II", "III", "aVR", "aVL", "aVF", "V1", "V2", "V3", "V4", "V5", "V6"]
        for i in range(n_leads):
            name = names[i] if i < len(names) else f"L{i}"
            leads[name] = [math.sin(j * 0.05 + i) * 0.5 for j in range(n_samples)]
        return leads

    def test_seven_leads(self):
        block = {
            "type": "waveform",
            "title": "ECG — P001",
            "sampling_rate_hz": 200,
            "duration_s": 2.5,
            "leads": self._make_lead_data(7),
        }
        result = render_waveform(block)
        assert result[:4] == PNG_HEADER
        assert len(result) > 1000

    def test_single_lead(self):
        block = {
            "type": "waveform",
            "title": "Single Lead ECG",
            "sampling_rate_hz": 200,
            "duration_s": 2.5,
            "leads": self._make_lead_data(1),
        }
        result = render_waveform(block)
        assert result[:4] == PNG_HEADER

    def test_empty_leads(self):
        block = {
            "type": "waveform",
            "title": "No Leads",
            "sampling_rate_hz": 200,
            "duration_s": 12,
            "leads": {},
        }
        result = render_waveform(block)
        assert result[:4] == PNG_HEADER


class TestRenderTable:
    def test_vitals_table(self):
        block = {
            "type": "data_table",
            "title": "Vitals — P001",
            "columns": ["Metric", "Value", "Unit"],
            "rows": [
                ["Heart Rate", "82", "bpm"],
                ["BP Systolic", "120", "mmHg"],
                ["SpO2", "98", "%"],
                ["Temperature", "37.2", "°C"],
            ],
        }
        result = render_table(block)
        assert result[:4] == PNG_HEADER
        assert len(result) > 1000

    def test_empty_table(self):
        block = {
            "type": "data_table",
            "title": "Empty",
            "columns": [],
            "rows": [],
        }
        result = render_table(block)
        assert result[:4] == PNG_HEADER

    def test_many_rows(self):
        block = {
            "type": "data_table",
            "title": "Large Table",
            "columns": ["ID", "Name", "Value"],
            "rows": [[str(i), f"Item {i}", str(i * 10)] for i in range(50)],
        }
        result = render_table(block)
        assert result[:4] == PNG_HEADER
