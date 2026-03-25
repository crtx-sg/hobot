"""Integration tests for the new orchestrator."""

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "clinibot"))

from domain_models import ModelResult
from providers import ChatResult, ProviderConfig, ToolCall
from skills import SkillOutput, SkillRegistry
from skills.interpret_vitals import InterpretVitalsSkill
from orchestrator import (
    OrchestratorResult,
    ToolCallStarted,
    ToolResultEvent,
    ConfirmationEvent,
    TextResponseEvent,
    _orchestrator_loop,
    load_system_prompt,
)


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
    s.active_patients = set()
    s.messages = []
    s.last_consolidated = 0
    s.summary = ""
    s.get_context = MagicMock(return_value=[])
    return s


def _mock_provider(
    responses: list[ChatResult],
    phi_safe: bool = True,
    name: str = "anthropic",
):
    """Create a mock provider that returns responses in sequence."""
    provider = AsyncMock()
    provider.config = ProviderConfig(
        name=name, base_url="http://test", api_key="test",
        model="test-model", phi_safe=phi_safe,
    )
    provider.is_available = AsyncMock(return_value=True)
    provider.chat = AsyncMock(side_effect=responses)
    return provider


def _make_registry() -> SkillRegistry:
    """Create a minimal skill registry for testing."""
    registry = SkillRegistry()

    mock_clinical = AsyncMock()
    mock_clinical.is_available = AsyncMock(return_value=True)
    mock_clinical.predict = AsyncMock(return_value=ModelResult(
        content="Vitals interpreted", model_name="gemini", model_version="test",
    ))

    mock_vitals = AsyncMock()
    mock_vitals.is_available = AsyncMock(return_value=True)
    mock_vitals.predict = AsyncMock(return_value=ModelResult(
        content="NEWS2=2",
        score={"news2": 2, "risk": "low"},
        model_name="vitals_anomaly",
        model_version="1.0",
    ))

    skill = InterpretVitalsSkill(
        domain_models={"clinical_reasoning": mock_clinical, "vitals_anomaly": mock_vitals},
    )
    registry.register(skill)
    return registry


# ---------------------------------------------------------------------------
# System prompt loading
# ---------------------------------------------------------------------------

class TestLoadSystemPrompt:
    def test_includes_system_prompt(self):
        with patch("orchestrator._load_file_if_exists") as mock_load:
            mock_load.side_effect = lambda p: "## Rules" if "system_prompt" in str(p) else ""
            with patch("tools.get_tool_list", return_value=[]):
                prompt = load_system_prompt()
        assert "Rules" in prompt

    def test_includes_tools(self):
        mock_tools = [{"name": "get_vitals", "critical": False}]
        with patch("orchestrator._load_file_if_exists", return_value=""):
            with patch("tools.get_tool_list", return_value=mock_tools), \
                 patch("tools._TOOL_DESCRIPTIONS", {"get_vitals": "Get vitals"}):
                prompt = load_system_prompt()
        assert "get_vitals" in prompt


# ---------------------------------------------------------------------------
# Orchestrator loop
# ---------------------------------------------------------------------------

class TestOrchestratorLoop:
    @pytest.mark.asyncio
    async def test_simple_text_response(self, mock_session):
        """Provider returns text immediately — no tool calls."""
        provider = _mock_provider([
            ChatResult(content="Patient vitals are normal."),
        ])
        registry = SkillRegistry()

        events = []
        with patch("orchestrator.load_system_prompt", return_value="system"), \
             patch("orchestrator.build_tool_definitions", return_value=[]), \
             patch("clinical_memory.get_facts", return_value=[]):
            async for event in _orchestrator_loop(
                "How is patient P001?", mock_session, provider, ["core"], registry,
            ):
                events.append(event)

        assert len(events) == 1
        assert isinstance(events[0], TextResponseEvent)
        assert "normal" in events[0].content

    @pytest.mark.asyncio
    async def test_tool_call_then_response(self, mock_session):
        """Provider calls a tool, gets result, then responds."""
        provider = _mock_provider([
            # First call: tool call
            ChatResult(
                content=None,
                tool_calls=[ToolCall(id="tc1", name="get_vitals", params={"patient_id": "P001"})],
                raw_message={"role": "assistant", "content": [
                    {"type": "tool_use", "id": "tc1", "name": "get_vitals", "input": {"patient_id": "P001"}},
                ]},
            ),
            # Second call: text response
            ChatResult(content="Patient P001 vitals: HR 80, BP 120/80, SpO2 98%"),
        ])
        registry = _make_registry()

        mock_vitals = {"heart_rate": 80, "bp_systolic": 120, "spo2": 98}
        mock_demo = [{"fact_type": "demographics", "fact_data": {"age": 50}}]

        async def mock_get_facts(pid, tid, fact_type=None, limit=50):
            if fact_type == "demographics":
                return mock_demo
            return []

        events = []
        with patch("orchestrator.load_system_prompt", return_value="system"), \
             patch("orchestrator.build_tool_definitions", return_value=[]), \
             patch("clinical_memory.get_facts", side_effect=mock_get_facts), \
             patch("orchestrator.call_tools_parallel", return_value=[
                 {"tool": "get_vitals", "params": {"patient_id": "P001"}, "data": mock_vitals},
             ]), \
             patch("orchestrator._post_tool_hooks", return_value=None), \
             patch("skills.clinical_context.fetch_from_backend", return_value=None):
            async for event in _orchestrator_loop(
                "Vitals for P001", mock_session, provider, ["vitals"], registry,
            ):
                events.append(event)

        # Should have: ToolCallStarted, ToolResultEvent, TextResponseEvent
        types = [type(e).__name__ for e in events]
        assert "ToolCallStarted" in types
        assert "ToolResultEvent" in types
        assert "TextResponseEvent" in types

        # Skill should have added _analysis to tool result
        tr_events = [e for e in events if isinstance(e, ToolResultEvent)]
        assert len(tr_events) == 1
        assert "_analysis" in tr_events[0].data

    @pytest.mark.asyncio
    async def test_confirmation_gating(self, mock_session):
        """Critical tools return confirmation event."""
        provider = _mock_provider([
            ChatResult(
                content=None,
                tool_calls=[ToolCall(id="tc1", name="initiate_code_blue", params={"patient_id": "P001"})],
                raw_message={"role": "assistant", "content": []},
            ),
        ])
        registry = SkillRegistry()

        confirmation_data = {
            "status": "awaiting_confirmation",
            "confirmation_id": "abc123",
            "message": "Confirm code blue for P001?",
        }

        events = []
        with patch("orchestrator.load_system_prompt", return_value="system"), \
             patch("orchestrator.build_tool_definitions", return_value=[]), \
             patch("clinical_memory.get_facts", return_value=[]), \
             patch("orchestrator.call_tools_parallel", return_value=[
                 {"tool": "initiate_code_blue", "params": {"patient_id": "P001"}, "data": confirmation_data},
             ]), \
             patch("orchestrator._post_tool_hooks", return_value=None):
            async for event in _orchestrator_loop(
                "Code blue for P001", mock_session, provider, ["emergency"], registry,
            ):
                events.append(event)

        assert any(isinstance(e, ConfirmationEvent) for e in events)

    @pytest.mark.asyncio
    async def test_phi_redaction(self, mock_session):
        """PHI is redacted for non-phi-safe providers."""
        provider = _mock_provider(
            [ChatResult(content="Patient data reviewed.")],
            phi_safe=False,
        )
        registry = SkillRegistry()

        # Add a user message to session context so there's content to redact
        mock_session.get_context = MagicMock(return_value=[
            {"role": "user", "content": "Check vitals for P001"},
        ])

        events = []
        with patch("orchestrator.load_system_prompt", return_value="system"), \
             patch("orchestrator.build_tool_definitions", return_value=[]), \
             patch("clinical_memory.get_facts", return_value=[]):
            async for event in _orchestrator_loop(
                "Vitals for P001", mock_session, provider, ["vitals"], registry,
            ):
                events.append(event)

        # Provider.chat should have been called with redacted messages
        call_args = provider.chat.call_args
        messages = call_args[0][0]
        # User messages should exist and have redacted content
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert len(user_msgs) > 0
        # P001 should be replaced with a placeholder
        for msg in user_msgs:
            assert "P001" not in msg["content"]

    @pytest.mark.asyncio
    async def test_max_iterations(self, mock_session):
        """Orchestrator stops after MAX_ITERATIONS."""
        # Provider always returns tool calls, never text
        tool_response = ChatResult(
            content=None,
            tool_calls=[ToolCall(id="tc1", name="get_vitals", params={"patient_id": "P001"})],
            raw_message={"role": "assistant", "content": []},
        )
        # Return enough responses for MAX_ITERATIONS
        provider = _mock_provider([tool_response] * 15)
        registry = SkillRegistry()

        events = []
        with patch("orchestrator.load_system_prompt", return_value="system"), \
             patch("orchestrator.build_tool_definitions", return_value=[]), \
             patch("clinical_memory.get_facts", return_value=[]), \
             patch("orchestrator.call_tools_parallel", return_value=[
                 {"tool": "get_vitals", "params": {"patient_id": "P001"}, "data": {"heart_rate": 80}},
             ]), \
             patch("orchestrator._post_tool_hooks", return_value=None), \
             patch("orchestrator.MAX_ITERATIONS", 3):  # Override for test speed
            async for event in _orchestrator_loop(
                "Vitals", mock_session, provider, ["vitals"], registry,
            ):
                events.append(event)

        # Last event should be the max-iterations text
        assert isinstance(events[-1], TextResponseEvent)
        assert "maximum" in events[-1].content.lower()

    @pytest.mark.asyncio
    async def test_provider_none_no_results(self, mock_session):
        """Provider returns None with no collected results — error message."""
        provider = _mock_provider([None])
        registry = SkillRegistry()

        events = []
        with patch("orchestrator.load_system_prompt", return_value="system"), \
             patch("orchestrator.build_tool_definitions", return_value=[]), \
             patch("clinical_memory.get_facts", return_value=[]):
            async for event in _orchestrator_loop(
                "Help", mock_session, provider, ["core"], registry,
            ):
                events.append(event)

        assert len(events) == 1
        assert isinstance(events[0], TextResponseEvent)
        assert "trouble" in events[0].content.lower()
