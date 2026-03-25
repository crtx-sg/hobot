"""Tests for clinical memory extractors, context building, and vision message formatting.

These tests cover functionality that was in analyzers.py and is now split across:
- clinical_memory.py (fact extractors)
- skills/clinical_context.py (patient context building)
- domain_models/radiology_model.py (vision message formatting)
"""

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "clinibot"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_session():
    s = MagicMock()
    s.id = "test-session"
    s.tenant_id = "test-tenant"
    s.user_id = "test-user"
    s.channel = "webchat"
    return s


# ---------------------------------------------------------------------------
# ECG extractor (clinical_memory)
# ---------------------------------------------------------------------------

class TestECGExtractor:
    def test_strips_waveform_arrays(self):
        from clinical_memory import _extract_ecg
        result = {
            "patient_id": "P001",
            "sampling_rate_hz": 500,
            "leads": {"I": [0.1, 0.2], "II": [0.3]},
            "waveform": [1, 2, 3],
            "samples": [[1, 2]],
        }
        facts = _extract_ecg(result, "P001")
        assert len(facts) == 1
        assert facts[0]["fact_type"] == "ecg"
        data = facts[0]["fact_data"]
        assert "leads" not in data
        assert "waveform" not in data
        assert "samples" not in data
        assert data["lead_count"] == 2
        assert set(data["lead_names"]) == {"I", "II"}


# ---------------------------------------------------------------------------
# ECG stub preprocessor (now in skills/interpret_ecg.py)
# ---------------------------------------------------------------------------

class TestECGStubPreprocessor:
    def test_strips_arrays(self):
        from skills.interpret_ecg import ecg_stub_preprocessor
        data = {
            "ecg": {
                "sampling_rate_hz": 500,
                "duration_s": 10,
                "leads": {"I": [0.1, 0.2], "II": [0.3, 0.4], "V1": [0.5]},
                "waveform": [1, 2, 3],
                "patient_id": "P001",
            }
        }
        result, capability = ecg_stub_preprocessor(data)
        assert capability == "limited"
        ecg = result["ecg"]
        assert "leads" not in ecg
        assert "waveform" not in ecg
        assert ecg["lead_count"] == 3
        assert set(ecg["lead_names"]) == {"I", "II", "V1"}
        assert ecg["sampling_rate_hz"] == 500


# ---------------------------------------------------------------------------
# _build_patient_context (now in skills/clinical_context.py)
# ---------------------------------------------------------------------------

class TestBuildPatientContext:
    @pytest.mark.asyncio
    async def test_filters_fact_types(self):
        from skills.clinical_context import build_patient_context

        mock_demo = [{"fact_type": "demographics", "fact_data": {"age": 72, "gender": "M"}}]
        mock_meds = [{"fact_type": "medication", "fact_data": {"name": "lisinopril"}}]

        async def mock_get_facts(pid, tid, fact_type=None, limit=50):
            if fact_type == "demographics":
                return mock_demo
            if fact_type == "medication":
                return mock_meds
            return []

        with patch("clinical_memory.get_facts", side_effect=mock_get_facts):
            summary, found = await build_patient_context(
                "P001", "test", ["demographics", "medication", "allergy"]
            )

        assert "demographics" in found
        assert "medication" in found
        assert "allergy" not in found
        assert "72" in summary
        assert "lisinopril" in summary

    @pytest.mark.asyncio
    async def test_backend_fallback(self, mock_session):
        """When memory is empty but session is provided, fetch from backend."""
        from skills.clinical_context import build_patient_context

        async def mock_get_facts(pid, tid, fact_type=None, limit=50):
            return []  # Nothing in memory

        async def mock_fetch_backend(pid, ft, sess):
            if ft == "demographics":
                return {"name": "John", "age": 65}
            return None

        with patch("clinical_memory.get_facts", side_effect=mock_get_facts), \
             patch("skills.clinical_context.fetch_from_backend", side_effect=mock_fetch_backend):
            summary, found = await build_patient_context(
                "P001", "test", ["demographics", "medication"],
                session=mock_session,
            )

        assert "demographics" in found
        assert "medication" not in found
        assert "John" in summary


# ---------------------------------------------------------------------------
# Vision message building (now in domain_models/radiology_model.py)
# ---------------------------------------------------------------------------

class TestBuildVisionMessage:
    def test_gemini_format(self):
        from domain_models.radiology_model import RadiologyModel
        model = RadiologyModel(provider_name="gemini")
        msgs = model._build_vision_message("Describe image", "abc123", "image/png")
        assert len(msgs) == 1
        content = msgs[0]["content"]
        assert len(content) == 2
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_anthropic_format(self):
        from domain_models.radiology_model import RadiologyModel
        model = RadiologyModel(provider_name="anthropic")
        msgs = model._build_vision_message("Describe image", "abc123", "image/png")
        assert len(msgs) == 1
        content = msgs[0]["content"]
        assert len(content) == 2
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "image"
        assert content[1]["source"]["type"] == "base64"
        assert content[1]["source"]["media_type"] == "image/png"
        assert content[1]["source"]["data"] == "abc123"


# ---------------------------------------------------------------------------
# Analyzer fact storage (clinical_memory integration)
# ---------------------------------------------------------------------------

class TestAnalyzerFactStorage:
    def test_analyzer_extractors_registered(self):
        from clinical_memory import EXTRACTORS
        for name in ("analyze_lab_results", "analyze_ecg", "analyze_vitals", "analyze_radiology_image"):
            assert name in EXTRACTORS

    def test_analyzer_extractor_returns_interpretation(self):
        from clinical_memory import EXTRACTORS
        result = {"status": "success", "interpretation": "Normal labs"}
        facts = EXTRACTORS["analyze_lab_results"](result, "P001")
        assert len(facts) == 1
        assert facts[0]["fact_type"] == "lab_interpretation"
        assert facts[0]["fact_data"] == result

    def test_intercept_analysis_stored(self):
        """extract_and_store should store _analysis as separate fact."""
        from clinical_memory import EXTRACTORS
        tool_result = {
            "report_text": "Normal",
            "_analysis": {"status": "success", "interpretation": "Summary"},
        }
        facts = EXTRACTORS["get_report"](tool_result, "P001")
        assert any(f["fact_type"] == "radiology_report" for f in facts)
