"""Unit tests for providers — ChatResult, ToolCall, _to_openai_tool, native parsing."""

import json
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "clinibot"))

from providers import (
    ChatResult,
    ToolCall,
    ProviderConfig,
    OllamaProvider,
    OpenAICompatibleProvider,
    _to_openai_tool,
)
from tools import build_tool_definitions, load_tools_config, _TOOL_DESCRIPTIONS


# ---------------------------------------------------------------------------
# ChatResult / ToolCall dataclass tests
# ---------------------------------------------------------------------------


class TestChatResult:
    def test_defaults(self):
        r = ChatResult()
        assert r.content is None
        assert r.tool_calls == []
        assert r.raw_message is None

    def test_content_only(self):
        r = ChatResult(content="hello")
        assert r.content == "hello"
        assert r.tool_calls == []

    def test_with_tool_calls(self):
        tc = ToolCall(id="call_1", name="get_vitals", params={"patient_id": "P001"})
        r = ChatResult(content=None, tool_calls=[tc], raw_message={"role": "assistant"})
        assert len(r.tool_calls) == 1
        assert r.tool_calls[0].name == "get_vitals"
        assert r.raw_message is not None


# ---------------------------------------------------------------------------
# _to_openai_tool tests
# ---------------------------------------------------------------------------


class TestToOpenaiTool:
    def test_basic_conversion(self):
        tool_def = {
            "name": "get_vitals",
            "description": "Get latest vitals for a patient",
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_id": {"type": "string"},
                },
                "required": ["patient_id"],
            },
        }
        result = _to_openai_tool(tool_def)
        assert result["type"] == "function"
        assert result["function"]["name"] == "get_vitals"
        assert result["function"]["description"] == "Get latest vitals for a patient"
        assert result["function"]["parameters"]["required"] == ["patient_id"]

    def test_missing_description(self):
        tool_def = {"name": "foo", "parameters": {"type": "object", "properties": {}}}
        result = _to_openai_tool(tool_def)
        assert result["function"]["description"] == ""

    def test_missing_parameters(self):
        tool_def = {"name": "bar", "description": "test"}
        result = _to_openai_tool(tool_def)
        assert result["function"]["parameters"] == {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# OpenAICompatibleProvider.chat — mock response parsing
# ---------------------------------------------------------------------------


class TestOpenAIProviderParsesToolCalls:
    @pytest.mark.asyncio
    async def test_parses_tool_calls_from_response(self, httpx_mock):
        """Provider should parse tool_calls array from response."""
        config = ProviderConfig(
            name="test-openai",
            base_url="http://localhost:9999",
            api_key="test-key",
            model="gpt-4",
            phi_safe=True,
        )
        provider = OpenAICompatibleProvider(config)

        mock_response = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {
                                "name": "get_vitals",
                                "arguments": '{"patient_id": "P001"}',
                            },
                        },
                        {
                            "id": "call_def",
                            "type": "function",
                            "function": {
                                "name": "get_medications",
                                "arguments": '{"patient_id": "P001"}',
                            },
                        },
                    ],
                },
            }],
        }

        httpx_mock.add_response(
            url="http://localhost:9999/v1/chat/completions",
            json=mock_response,
        )

        result = await provider.chat(
            [{"role": "user", "content": "test"}],
            tools=[{"name": "get_vitals", "description": "test", "parameters": {"type": "object", "properties": {}}}],
        )

        assert result is not None
        assert result.content is None
        assert len(result.tool_calls) == 2
        assert result.tool_calls[0].id == "call_abc"
        assert result.tool_calls[0].name == "get_vitals"
        assert result.tool_calls[0].params == {"patient_id": "P001"}
        assert result.tool_calls[1].name == "get_medications"
        assert result.raw_message is not None

    @pytest.mark.asyncio
    async def test_content_only_response(self, httpx_mock):
        """Provider returns ChatResult with content when no tool_calls."""
        config = ProviderConfig(
            name="test-openai",
            base_url="http://localhost:9999",
            api_key="test-key",
            model="gpt-4",
            phi_safe=True,
        )
        provider = OpenAICompatibleProvider(config)

        httpx_mock.add_response(
            url="http://localhost:9999/v1/chat/completions",
            json={"choices": [{"message": {"role": "assistant", "content": "Hello!"}}]},
        )

        result = await provider.chat([{"role": "user", "content": "hi"}])
        assert result is not None
        assert result.content == "Hello!"
        assert result.tool_calls == []
        assert result.raw_message is None


# ---------------------------------------------------------------------------
# OllamaProvider ignores tools
# ---------------------------------------------------------------------------


class TestOllamaIgnoresTools:
    @pytest.mark.asyncio
    async def test_returns_chat_result_ignoring_tools(self, httpx_mock):
        config = ProviderConfig(
            name="test-ollama",
            base_url="http://localhost:11434",
            api_key="",
            model="llama3",
            phi_safe=True,
        )
        provider = OllamaProvider(config)

        httpx_mock.add_response(
            url="http://localhost:11434/api/chat",
            json={"message": {"content": "Ollama response"}},
        )

        # Pass tools — should be ignored
        result = await provider.chat(
            [{"role": "user", "content": "test"}],
            tools=[{"name": "get_vitals", "description": "test", "parameters": {}}],
        )
        assert result is not None
        assert result.content == "Ollama response"
        assert result.tool_calls == []


# ---------------------------------------------------------------------------
# build_tool_definitions
# ---------------------------------------------------------------------------


class TestBuildToolDefinitions:
    def test_all_tools_have_required_fields(self):
        # Load the real tools config
        config_path = os.path.join(os.path.dirname(__file__), "..", "config", "tools.json")
        if os.path.exists(config_path):
            load_tools_config(config_path)

        defs = build_tool_definitions()
        assert len(defs) > 0

        for d in defs:
            assert "name" in d
            assert "description" in d
            assert "parameters" in d
            assert isinstance(d["description"], str)
            assert len(d["description"]) > 0
            assert d["parameters"]["type"] == "object"

    def test_descriptions_cover_all_tools(self):
        config_path = os.path.join(os.path.dirname(__file__), "..", "config", "tools.json")
        if os.path.exists(config_path):
            load_tools_config(config_path)

        defs = build_tool_definitions()
        names = {d["name"] for d in defs}
        for name in _TOOL_DESCRIPTIONS:
            assert name in names, f"{name} in _TOOL_DESCRIPTIONS but not in build_tool_definitions()"
