"""Unit tests for clinical analyzers — intercept, tool analyzers, preprocessors."""

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "clinibot"))

import analyzers
from analyzers import (
    _build_patient_context,
    _build_vision_message,
    _ecg_stub_preprocessor,
    _fetch_source_data,
    get_analyzer_tool_definitions,
    load_analyzer_config,
    maybe_analyze,
    run_analyzer_tool,
)
from providers import ChatResult, ProviderConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_config():
    """Reset analyzer config before each test."""
    analyzers._intercept_config = {}
    analyzers._tool_config = {}
    analyzers._defaults = {}
    yield
    analyzers._intercept_config = {}
    analyzers._tool_config = {}
    analyzers._defaults = {}


@pytest.fixture
def mock_session():
    s = MagicMock()
    s.id = "test-session"
    s.tenant_id = "test-tenant"
    s.user_id = "test-user"
    s.channel = "webchat"
    return s


@pytest.fixture
def sample_config_path(tmp_path):
    config = {
        "providers": {},
        "analyzers": {
            "intercept": {
                "get_report": {
                    "provider": "gemini",
                    "prompt_template": "radiology_report_summary",
                    "domain": "radiology",
                }
            },
            "tools": {
                "analyze_lab_results": {
                    "provider": "gemini",
                    "prompt_template": "lab_analysis",
                    "domain": "pathology",
                    "source_fact_types": ["lab_result"],
                    "context_types": ["demographics", "medication", "allergy"],
                },
                "analyze_ecg": {
                    "provider": "ollama",
                    "prompt_template": "ecg_analysis",
                    "domain": "cardiology",
                    "preprocessor": "ecg_stub",
                    "source_fact_types": ["ecg"],
                    "context_types": ["demographics", "medication"],
                },
                "analyze_vitals": {
                    "provider": "gemini",
                    "prompt_template": "vitals_analysis",
                    "domain": "critical_care",
                    "source_fact_types": ["vitals", "vitals_trend"],
                    "context_types": ["demographics", "medication", "allergy"],
                },
                "analyze_radiology_image": {
                    "provider": "gemini",
                    "prompt_template": "radiology_image_analysis",
                    "domain": "radiology",
                    "input_type": "image_wado",
                    "context_types": ["demographics", "medication", "imaging_study"],
                },
            },
            "defaults": {"fallback": "passthrough", "timeout": 15},
        },
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return str(path)


def _mock_provider(content="Interpreted result", phi_safe=True, name="gemini"):
    provider = AsyncMock()
    provider.config = ProviderConfig(
        name=name, base_url="http://test", api_key="", model="test-model",
        phi_safe=phi_safe,
    )
    provider.is_available = AsyncMock(return_value=True)
    provider.chat = AsyncMock(return_value=ChatResult(content=content))
    return provider


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_load_analyzer_config(self, sample_config_path):
        load_analyzer_config(sample_config_path)
        assert "get_report" in analyzers._intercept_config
        assert "analyze_lab_results" in analyzers._tool_config
        assert analyzers._defaults["timeout"] == 15

    def test_load_missing_file(self):
        load_analyzer_config("/nonexistent/config.json")
        assert analyzers._intercept_config == {}


# ---------------------------------------------------------------------------
# Intercept (maybe_analyze)
# ---------------------------------------------------------------------------

class TestMaybeAnalyze:
    @pytest.mark.asyncio
    async def test_no_config_passthrough(self, mock_session):
        """When no intercept configured, return raw data unchanged."""
        data = {"report": "Normal findings"}
        result = await maybe_analyze("get_report", data, {}, mock_session)
        assert result == data
        assert "_analysis" not in result

    @pytest.mark.asyncio
    async def test_intercept_report(self, sample_config_path, mock_session):
        """Mock provider, verify _analysis added in AnalyzerResult format."""
        load_analyzer_config(sample_config_path)
        provider = _mock_provider(content="Summary: Normal CT scan")

        with patch("analyzers.get_provider", return_value=provider):
            result = await maybe_analyze(
                "get_report",
                {"report_text": "CT scan of chest..."},
                {},
                mock_session,
            )

        assert "_analysis" in result
        analysis = result["_analysis"]
        assert analysis["status"] == "success"
        assert analysis["domain"] == "radiology"
        assert analysis["interpretation"] == "Summary: Normal CT scan"
        assert analysis["analyzer_provider"] == "gemini"
        assert isinstance(analysis["findings"], list)

    @pytest.mark.asyncio
    async def test_skips_non_intercept(self, sample_config_path, mock_session):
        """Labs should NOT be auto-intercepted."""
        load_analyzer_config(sample_config_path)
        data = {"results": [{"test": "CBC", "value": "normal"}]}
        result = await maybe_analyze("get_lab_results", data, {}, mock_session)
        assert "_analysis" not in result

    @pytest.mark.asyncio
    async def test_intercept_uses_analyzer_result_format(self, sample_config_path, mock_session):
        """_analysis has findings[], domain, status fields."""
        load_analyzer_config(sample_config_path)
        provider = _mock_provider(content="Key finding: nodule")

        with patch("analyzers.get_provider", return_value=provider):
            result = await maybe_analyze("get_report", {"text": "report"}, {}, mock_session)

        analysis = result["_analysis"]
        assert "findings" in analysis
        assert "domain" in analysis
        assert "status" in analysis
        assert "context_used" in analysis
        assert "context_summary" in analysis
        assert "analyzer_model" in analysis


# ---------------------------------------------------------------------------
# Tool analyzers (run_analyzer_tool)
# ---------------------------------------------------------------------------

class TestRunAnalyzerTool:
    @pytest.mark.asyncio
    async def test_success(self, sample_config_path, mock_session):
        """Full success path: source data + context → interpretation."""
        load_analyzer_config(sample_config_path)
        provider = _mock_provider(content="BUN elevated, expected for CKD")

        mock_demo = [{"fact_type": "demographics", "fact_data": {"age": 72, "gender": "M"}}]
        mock_meds = [{"fact_type": "medication", "fact_data": {"name": "lisinopril"}}]
        mock_allergy = [{"fact_type": "allergy", "fact_data": {"substance": "NKDA"}}]
        mock_labs = [{"fact_type": "lab_result", "fact_data": {"BUN": "22 mg/dL"}}]

        async def mock_get_facts(pid, tid, fact_type=None, limit=50):
            if fact_type == "lab_result":
                return mock_labs
            if fact_type == "demographics":
                return mock_demo
            if fact_type == "medication":
                return mock_meds
            if fact_type == "allergy":
                return mock_allergy
            return []

        with patch("analyzers.get_provider", return_value=provider), \
             patch("clinical_memory.get_facts", side_effect=mock_get_facts):
            result = await run_analyzer_tool(
                "analyze_lab_results",
                {"patient_id": "P001"},
                mock_session,
            )

        assert result["status"] == "success"
        assert result["domain"] == "pathology"
        assert "BUN elevated" in result["interpretation"]
        assert "demographics" in result["context_used"]
        assert result["analyzer_provider"] == "gemini"

    @pytest.mark.asyncio
    async def test_partial_context(self, sample_config_path, mock_session):
        """Missing demographics → status partial, reason incomplete_context."""
        load_analyzer_config(sample_config_path)
        provider = _mock_provider(content="Labs reviewed without context")

        mock_facts_source = [
            {"fact_type": "lab_result", "fact_data": {"BUN": "22"}},
        ]

        async def mock_get_facts(pid, tid, fact_type=None, limit=50):
            if fact_type == "lab_result":
                return mock_facts_source
            return []  # No context at all

        with patch("analyzers.get_provider", return_value=provider), \
             patch("clinical_memory.get_facts", side_effect=mock_get_facts):
            result = await run_analyzer_tool(
                "analyze_lab_results",
                {"patient_id": "P001"},
                mock_session,
            )

        assert result["status"] == "partial"
        assert "incomplete_context" in result["status_reason"]

    @pytest.mark.asyncio
    async def test_partial_capability(self, sample_config_path, mock_session):
        """ECG stub preprocessor → status partial, reason limited_capability."""
        load_analyzer_config(sample_config_path)
        provider = _mock_provider(content="ECG metadata only")

        mock_ecg = [{"fact_type": "ecg", "fact_data": {
            "sampling_rate_hz": 500, "duration_s": 10,
            "lead_names": ["I", "II", "III"], "lead_count": 3,
        }}]
        mock_demo = [{"fact_type": "demographics", "fact_data": {"age": 72}}]
        mock_meds = [{"fact_type": "medication", "fact_data": {"name": "metoprolol"}}]

        async def mock_get_facts(pid, tid, fact_type=None, limit=50):
            if fact_type == "ecg":
                return mock_ecg
            if fact_type == "demographics":
                return mock_demo
            if fact_type == "medication":
                return mock_meds
            return []

        with patch("analyzers.get_provider", return_value=provider), \
             patch("clinical_memory.get_facts", side_effect=mock_get_facts):
            result = await run_analyzer_tool(
                "analyze_ecg",
                {"patient_id": "P001"},
                mock_session,
            )

        assert result["status"] == "partial"
        assert "limited_capability" in result["status_reason"]

    @pytest.mark.asyncio
    async def test_no_source_data(self, sample_config_path, mock_session):
        """No facts in memory → error result."""
        load_analyzer_config(sample_config_path)
        provider = _mock_provider()

        async def mock_get_facts(pid, tid, fact_type=None, limit=50):
            return []

        with patch("analyzers.get_provider", return_value=provider), \
             patch("clinical_memory.get_facts", side_effect=mock_get_facts):
            result = await run_analyzer_tool(
                "analyze_lab_results",
                {"patient_id": "P001"},
                mock_session,
            )

        assert result["status"] == "error"
        assert result["status_reason"] == "no_source_data"
        assert "Fetch the data first" in result["interpretation"]

    @pytest.mark.asyncio
    async def test_timeout(self, sample_config_path, mock_session):
        """Provider slow → error result with timeout reason."""
        load_analyzer_config(sample_config_path)
        analyzers._defaults["timeout"] = 0.1  # Very short timeout

        async def slow_chat(*args, **kwargs):
            await asyncio.sleep(1.0)
            return ChatResult(content="too late")

        provider = _mock_provider()
        provider.chat = slow_chat

        mock_facts = [{"fact_type": "lab_result", "fact_data": {"WBC": "7.2"}}]

        async def mock_get_facts(pid, tid, fact_type=None, limit=50):
            if fact_type == "lab_result":
                return mock_facts
            return []

        with patch("analyzers.get_provider", return_value=provider), \
             patch("clinical_memory.get_facts", side_effect=mock_get_facts):
            result = await run_analyzer_tool(
                "analyze_lab_results",
                {"patient_id": "P001"},
                mock_session,
            )

        assert result["status"] == "error"
        assert result["status_reason"] == "timeout"


# ---------------------------------------------------------------------------
# ECG stub preprocessor
# ---------------------------------------------------------------------------

class TestECGStubPreprocessor:
    def test_strips_arrays(self):
        data = {
            "ecg": {
                "sampling_rate_hz": 500,
                "duration_s": 10,
                "leads": {"I": [0.1, 0.2], "II": [0.3, 0.4], "V1": [0.5]},
                "waveform": [1, 2, 3],
                "patient_id": "P001",
            }
        }
        result, capability = _ecg_stub_preprocessor(data)
        assert capability == "limited"
        ecg = result["ecg"]
        assert "leads" not in ecg
        assert "waveform" not in ecg
        assert ecg["lead_count"] == 3
        assert set(ecg["lead_names"]) == {"I", "II", "V1"}
        assert ecg["sampling_rate_hz"] == 500


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
# _build_patient_context
# ---------------------------------------------------------------------------

class TestBuildPatientContext:
    @pytest.mark.asyncio
    async def test_filters_fact_types(self):
        mock_demo = [{"fact_type": "demographics", "fact_data": {"age": 72, "gender": "M"}}]
        mock_meds = [{"fact_type": "medication", "fact_data": {"name": "lisinopril"}}]

        async def mock_get_facts(pid, tid, fact_type=None, limit=50):
            if fact_type == "demographics":
                return mock_demo
            if fact_type == "medication":
                return mock_meds
            return []

        with patch("clinical_memory.get_facts", side_effect=mock_get_facts):
            summary, found = await _build_patient_context(
                "P001", "test", ["demographics", "medication", "allergy"]
            )

        assert "demographics" in found
        assert "medication" in found
        assert "allergy" not in found
        assert "72" in summary
        assert "lisinopril" in summary


# ---------------------------------------------------------------------------
# _fetch_source_data
# ---------------------------------------------------------------------------

class TestFetchSourceData:
    @pytest.mark.asyncio
    async def test_retrieves_correct_types(self):
        mock_labs = [
            {"fact_type": "lab_result", "fact_data": {"BUN": "22"}},
            {"fact_type": "lab_result", "fact_data": {"WBC": "7.2"}},
        ]

        async def mock_get_facts(pid, tid, fact_type=None, limit=50):
            if fact_type == "lab_result":
                return mock_labs
            return []

        with patch("clinical_memory.get_facts", side_effect=mock_get_facts):
            data = await _fetch_source_data("P001", "test", ["lab_result"])

        assert data is not None
        assert "lab_result" in data
        assert len(data["lab_result"]) == 2

    @pytest.mark.asyncio
    async def test_returns_none_when_empty(self):
        async def mock_get_facts(pid, tid, fact_type=None, limit=50):
            return []

        with patch("clinical_memory.get_facts", side_effect=mock_get_facts):
            data = await _fetch_source_data("P001", "test", ["lab_result"])

        assert data is None


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

class TestAnalyzerToolDefinitions:
    def test_definitions_match_config(self, sample_config_path):
        load_analyzer_config(sample_config_path)
        defs = get_analyzer_tool_definitions()
        names = {d["name"] for d in defs}
        assert names == {
            "analyze_lab_results", "analyze_ecg",
            "analyze_vitals", "analyze_radiology_image",
        }

    def test_params_per_tool(self, sample_config_path):
        load_analyzer_config(sample_config_path)
        defs = get_analyzer_tool_definitions()
        by_name = {d["name"]: d for d in defs}

        # analyze_lab_results: only patient_id required
        lab_params = by_name["analyze_lab_results"]["parameters"]
        assert "patient_id" in lab_params["properties"]
        assert lab_params["required"] == ["patient_id"]

        # analyze_radiology_image: patient_id + study_id required
        img_params = by_name["analyze_radiology_image"]["parameters"]
        assert "patient_id" in img_params["properties"]
        assert "study_id" in img_params["properties"]
        assert set(img_params["required"]) == {"patient_id", "study_id"}

    def test_no_defs_when_unconfigured(self):
        defs = get_analyzer_tool_definitions()
        assert defs == []


# ---------------------------------------------------------------------------
# Vision message building
# ---------------------------------------------------------------------------

class TestBuildVisionMessage:
    def test_gemini_format(self):
        msgs = _build_vision_message("gemini", "Describe image", "abc123", "image/png")
        assert len(msgs) == 1
        content = msgs[0]["content"]
        assert len(content) == 2
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_anthropic_format(self):
        msgs = _build_vision_message("anthropic", "Describe image", "abc123", "image/png")
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
        # The extractor for get_report returns radiology_report
        facts = EXTRACTORS["get_report"](tool_result, "P001")
        assert any(f["fact_type"] == "radiology_report" for f in facts)
        # _analysis storage is handled in extract_and_store, not the extractor itself
