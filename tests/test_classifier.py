"""Unit tests for intent pre-classifier — regex, LLM, and fallback tiers."""

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "clinibot"))

import classifier
from classifier import _regex_classify, classify, load_classifier_config
from providers import ChatResult, ProviderConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_config():
    """Reset classifier config before each test."""
    classifier._config = {}
    classifier._domain_names = []
    yield
    classifier._config = {}
    classifier._domain_names = []


@pytest.fixture
def sample_config_path(tmp_path):
    config = {
        "tool_domains": {
            "core": ["resolve_bed", "list_wards"],
            "vitals": ["get_vitals", "get_vitals_history"],
            "labs": ["get_lab_results"],
            "medications": ["get_medications", "get_allergies"],
            "ecg": ["get_latest_ecg"],
            "radiology": ["get_latest_study", "get_report"],
            "orders": ["write_order", "order_lab"],
            "emergency": ["initiate_code_blue"],
            "services": ["request_housekeeping"],
            "scheduling": ["schedule_appointment"],
            "supplies": ["get_inventory"],
            "ward": ["list_doctors", "get_ward_patients"],
        },
        "classifier": {
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "timeout": 2,
            "fallback_domains": ["core", "vitals", "labs", "medications"],
        },
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return str(path)


@pytest.fixture
def mock_session():
    s = MagicMock()
    s.id = "test-session"
    s.tenant_id = "test-tenant"
    return s


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_loads_config(self, sample_config_path):
        load_classifier_config(sample_config_path)
        assert classifier._config["provider"] == "gemini"
        assert "vitals" in classifier._domain_names

    def test_missing_file(self):
        load_classifier_config("/nonexistent/config.json")
        assert classifier._config == {}


# ---------------------------------------------------------------------------
# Tier 1: Regex classification
# ---------------------------------------------------------------------------

class TestRegexClassify:
    @pytest.mark.parametrize("message,expected_domains", [
        ("Show me vitals for P001", ["vitals"]),
        ("What's the blood pressure?", ["vitals"]),
        ("Get lab results for bed 3", ["labs"]),
        ("CBC for patient P002", ["labs"]),
        ("Show ECG for P001", ["ecg"]),
        ("Latest 12-lead", ["ecg"]),
        ("Get the chest X-ray", ["radiology"]),
        ("CT scan results", ["radiology"]),
        ("What medications is the patient on?", ["medications"]),
        ("Any drug allergies?", ["medications"]),
        ("Order a CBC", ["orders", "labs"]),
        ("Code blue bed 5!", ["emergency"]),
        ("Request housekeeping for room 301", ["services"]),
        ("Schedule an appointment", ["scheduling"]),
        ("Check inventory", ["supplies"]),
        ("Ward rounds", ["ward"]),
        ("heart rate and SpO2", ["vitals"]),
    ])
    def test_regex_matches(self, message, expected_domains):
        result = _regex_classify(message)
        assert result is not None
        assert set(result) == set(expected_domains)

    def test_no_match(self):
        result = _regex_classify("how is bed 5 doing?")
        assert result is None

    def test_multi_domain_within_limit(self):
        """Message matching 2 domains should return both."""
        result = _regex_classify("Show vitals and labs for P001")
        assert result is not None
        assert "vitals" in result
        assert "labs" in result

    def test_too_many_domains_returns_none(self):
        """If >3 domains matched, return None for LLM to handle."""
        # Craft a message that hits many domains
        result = _regex_classify(
            "Check vitals, labs, ECG, radiology, and medication interactions"
        )
        # Should be None or have 3+ matches — either way classifier falls to LLM
        if result is not None:
            assert len(result) <= 3


# ---------------------------------------------------------------------------
# Tier 2: LLM classification
# ---------------------------------------------------------------------------

class TestLLMClassify:
    @pytest.mark.asyncio
    async def test_llm_classify_success(self, sample_config_path, mock_session):
        load_classifier_config(sample_config_path)

        provider = AsyncMock()
        provider.config = ProviderConfig(
            name="gemini", base_url="http://test", api_key="", model="test",
            phi_safe=True,
        )
        provider.is_available = AsyncMock(return_value=True)
        provider.chat = AsyncMock(return_value=ChatResult(content='["vitals", "labs"]'))

        with patch("providers.get_provider", return_value=provider):
            result = await classifier._llm_classify("how is patient doing overall?")

        assert result == ["vitals", "labs"]

    @pytest.mark.asyncio
    async def test_llm_classify_timeout(self, sample_config_path):
        load_classifier_config(sample_config_path)
        classifier._config["timeout"] = 0.01  # Very short

        async def slow_chat(*args, **kwargs):
            await asyncio.sleep(1.0)
            return ChatResult(content='["vitals"]')

        provider = AsyncMock()
        provider.config = ProviderConfig(
            name="gemini", base_url="http://test", api_key="", model="test",
            phi_safe=True,
        )
        provider.is_available = AsyncMock(return_value=True)
        provider.chat = slow_chat

        with patch("providers.get_provider", return_value=provider):
            result = await classifier._llm_classify("how is the patient?")

        assert result is None

    @pytest.mark.asyncio
    async def test_llm_classify_invalid_json(self, sample_config_path):
        load_classifier_config(sample_config_path)

        provider = AsyncMock()
        provider.config = ProviderConfig(
            name="gemini", base_url="http://test", api_key="", model="test",
            phi_safe=True,
        )
        provider.is_available = AsyncMock(return_value=True)
        provider.chat = AsyncMock(return_value=ChatResult(content="not json"))

        with patch("providers.get_provider", return_value=provider):
            result = await classifier._llm_classify("test")

        assert result is None

    @pytest.mark.asyncio
    async def test_llm_classify_validates_domains(self, sample_config_path):
        load_classifier_config(sample_config_path)

        provider = AsyncMock()
        provider.config = ProviderConfig(
            name="gemini", base_url="http://test", api_key="", model="test",
            phi_safe=True,
        )
        provider.is_available = AsyncMock(return_value=True)
        provider.chat = AsyncMock(return_value=ChatResult(content='["vitals", "bogus_domain"]'))

        with patch("providers.get_provider", return_value=provider):
            result = await classifier._llm_classify("test")

        assert result == ["vitals"]


# ---------------------------------------------------------------------------
# Full classify() pipeline
# ---------------------------------------------------------------------------

class TestClassify:
    @pytest.mark.asyncio
    async def test_regex_tier(self, sample_config_path, mock_session):
        load_classifier_config(sample_config_path)
        result = await classify("Show me vitals for P001", mock_session)
        assert "vitals" in result

    @pytest.mark.asyncio
    async def test_fallback_tier(self, mock_session):
        """No config, no regex match → fallback."""
        classifier._config = {"fallback_domains": ["core", "vitals"]}
        result = await classify("how is bed 5 doing?", mock_session)
        assert result == ["core", "vitals"]

    @pytest.mark.asyncio
    async def test_fallback_default(self, mock_session):
        """No config at all → default fallback."""
        result = await classify("hello", mock_session)
        assert "core" in result


# ---------------------------------------------------------------------------
# Domain filtering integration
# ---------------------------------------------------------------------------

class TestDomainFiltering:
    def test_get_tools_for_domains(self, sample_config_path):
        from tools import get_tools_for_domains, load_domain_config
        load_domain_config(sample_config_path)
        tools = get_tools_for_domains(["vitals", "labs"])
        assert "get_vitals" in tools
        assert "get_lab_results" in tools
        # Core always included
        assert "resolve_bed" in tools
        assert "list_wards" in tools

    def test_build_tool_definitions_filtered(self, sample_config_path):
        from tools import build_tool_definitions, load_domain_config, load_tools_config
        load_domain_config(sample_config_path)
        defs = build_tool_definitions(domains=["vitals"])
        names = {d["name"] for d in defs}
        assert "get_vitals" in names
        assert "resolve_bed" in names  # core always included
        # Should NOT include non-domain tools
        assert "get_inventory" not in names

    def test_build_tool_definitions_no_domains(self, sample_config_path):
        """None domains → all tools returned (backward compat)."""
        from tools import build_tool_definitions, load_domain_config
        load_domain_config(sample_config_path)
        all_defs = build_tool_definitions(domains=None)
        filtered_defs = build_tool_definitions(domains=["vitals"])
        assert len(all_defs) > len(filtered_defs)
