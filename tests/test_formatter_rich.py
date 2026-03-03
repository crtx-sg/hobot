"""Unit tests for formatter.py rich response rendering — no stack needed."""

import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nanobot"))

from formatter import (
    format_rich_response,
    render_blocks,
    _render_telegram,
    _render_slack,
    _render_whatsapp,
    _render_webchat,
    load_channels_config,
)

# Load the actual channels config for tests
_config_path = os.path.join(os.path.dirname(__file__), "..", "config", "channels.json")
load_channels_config(_config_path)


# Minimal AgentResult stand-in
class _FakeAgentResult:
    def __init__(self, text, tool_results=None):
        self.text = text
        self.tool_results = tool_results or []


class TestFormatRichResponse:
    def test_no_tool_results(self):
        result = format_rich_response(_FakeAgentResult("Hello"), "webchat")
        assert result["text"] == "Hello"
        assert result["blocks"] is None

    def test_with_blocks(self):
        tr = [{"tool": "get_vitals", "params": {}, "data": {"patient_id": "P001", "heart_rate": 82, "bp_systolic": 120, "bp_diastolic": 80, "spo2": 98, "temperature": 37.2}}]
        result = format_rich_response(_FakeAgentResult("Vitals for P001", tr), "webchat")
        assert result["blocks"] is not None
        assert len(result["blocks"]) > 0

    def test_block_filtering_by_channel(self):
        tr = [{"tool": "get_vitals", "params": {}, "data": {"patient_id": "P001", "heart_rate": 82, "bp_systolic": 120, "bp_diastolic": 80, "spo2": 98, "temperature": 37.2}}]
        # WhatsApp doesn't support actions
        result = format_rich_response(_FakeAgentResult("Vitals", tr), "whatsapp")
        if result["blocks"]:
            types = [b["type"] for b in result["blocks"]]
            assert "actions" not in types  # whatsapp doesn't have actions in supported_blocks

    def test_text_truncation_telegram(self):
        long_text = "x" * 5000
        result = format_rich_response(_FakeAgentResult(long_text), "telegram")
        assert len(result["text"]) <= 4096


class TestTelegramRenderer:
    def test_data_table(self):
        blocks = [{"type": "data_table", "title": "Vitals", "columns": ["Metric", "Value"], "rows": [["HR", "82"]]}]
        rendered = _render_telegram(blocks)
        assert rendered[0]["type"] == "text"
        assert "<b>Vitals</b>" in rendered[0]["html"]
        assert "Metric: HR" in rendered[0]["html"]

    def test_key_value(self):
        blocks = [{"type": "key_value", "title": "Patient", "items": [{"key": "Name", "value": "John"}]}]
        rendered = _render_telegram(blocks)
        assert "<b>Name:</b> John" in rendered[0]["html"]

    def test_alert(self):
        blocks = [{"type": "alert", "severity": "warning", "text": "NEWS elevated"}]
        rendered = _render_telegram(blocks)
        assert "WARNING" in rendered[0]["html"]

    def test_actions_inline_keyboard(self):
        blocks = [{"type": "actions", "buttons": [{"label": "View", "action": "get_vitals", "params": {"patient_id": "P001"}}]}]
        rendered = _render_telegram(blocks)
        assert rendered[0]["type"] == "inline_keyboard"
        cb = json.loads(rendered[0]["buttons"][0]["callback_data"])
        assert cb["action"] == "get_vitals"

    def test_confirmation(self):
        blocks = [{"type": "confirmation", "confirmation_id": "abc", "text": "Confirm?", "buttons": [{"label": "Confirm", "action": "confirm", "params": {"confirmation_id": "abc"}}]}]
        rendered = _render_telegram(blocks)
        assert rendered[0]["type"] == "confirmation"
        assert "Confirmation Required" in rendered[0]["html"]

    def test_image_passthrough(self):
        blocks = [{"type": "image", "url": "http://example.com/xray.png", "alt": "X-ray", "mime_type": "image/png"}]
        rendered = _render_telegram(blocks)
        assert rendered[0]["type"] == "image"
        assert rendered[0]["url"] == "http://example.com/xray.png"

    def test_chart_passthrough(self):
        blocks = [{"type": "chart", "title": "Trend", "chart_type": "line", "series": {}}]
        rendered = _render_telegram(blocks)
        assert rendered[0]["type"] == "chart"

    def test_waveform_passthrough(self):
        blocks = [{"type": "waveform", "title": "ECG", "sampling_rate_hz": 200, "duration_s": 12, "leads": {}}]
        rendered = _render_telegram(blocks)
        assert rendered[0]["type"] == "waveform"


class TestWebchatRenderer:
    def test_passthrough(self):
        blocks = [{"type": "data_table", "title": "X", "columns": [], "rows": []}]
        rendered = _render_webchat(blocks)
        assert rendered == blocks


class TestSlackRenderer:
    def test_data_table_to_section(self):
        blocks = [{"type": "data_table", "title": "Labs", "columns": ["Test", "Value"], "rows": [["WBC", "6.8"]]}]
        rendered = _render_slack(blocks)
        assert rendered[0]["type"] == "section"
        assert "*Labs*" in rendered[0]["text"]["text"]

    def test_actions_to_buttons(self):
        blocks = [{"type": "actions", "buttons": [{"label": "View", "action": "get_vitals", "params": {}}]}]
        rendered = _render_slack(blocks)
        assert rendered[0]["type"] == "actions"
        assert rendered[0]["elements"][0]["type"] == "button"

    def test_image_block(self):
        blocks = [{"type": "image", "url": "http://example.com/img.png", "alt": "scan"}]
        rendered = _render_slack(blocks)
        assert rendered[0]["type"] == "image"
        assert rendered[0]["image_url"] == "http://example.com/img.png"


class TestWhatsappRenderer:
    def test_data_table_to_text(self):
        blocks = [{"type": "data_table", "title": "Vitals", "columns": ["Metric", "Value"], "rows": [["HR", "82"]]}]
        rendered = _render_whatsapp(blocks)
        assert rendered[0]["type"] == "text"
        assert "*Vitals*" in rendered[0]["content"]

    def test_actions_to_numbered_list(self):
        blocks = [{"type": "actions", "buttons": [{"label": "View"}, {"label": "History"}]}]
        rendered = _render_whatsapp(blocks)
        assert "1. View" in rendered[0]["content"]
        assert "2. History" in rendered[0]["content"]

    def test_image_passthrough(self):
        blocks = [{"type": "image", "url": "http://example.com/xray.png", "alt": "X-ray"}]
        rendered = _render_whatsapp(blocks)
        assert rendered[0]["type"] == "image"

    def test_key_value(self):
        blocks = [{"type": "key_value", "title": "Patient", "items": [{"key": "Name", "value": "Jane"}]}]
        rendered = _render_whatsapp(blocks)
        assert "*Patient*" in rendered[0]["content"]
        assert "*Name:*" in rendered[0]["content"]


class TestBlockFiltering:
    """Test that format_rich_response filters blocks by channel supported_blocks."""

    def test_whatsapp_strips_actions(self):
        tr = [{"tool": "get_vitals", "params": {}, "data": {"patient_id": "P001", "heart_rate": 82, "bp_systolic": 120, "bp_diastolic": 80, "spo2": 98, "temperature": 37.2}}]
        result = format_rich_response(_FakeAgentResult("Vitals", tr), "whatsapp")
        if result["blocks"]:
            types = {b["type"] for b in result["blocks"]}
            assert "actions" not in types

    def test_whatsapp_strips_confirmation(self):
        tr = [{"tool": "code_blue", "params": {}, "data": {"status": "awaiting_confirmation", "confirmation_id": "c1", "message": "Critical action 'code_blue' needs confirmation."}}]
        result = format_rich_response(_FakeAgentResult("Confirm?", tr), "whatsapp")
        if result["blocks"]:
            types = {b["type"] for b in result["blocks"]}
            assert "confirmation" not in types

    def test_webchat_keeps_all_blocks(self):
        tr = [{"tool": "get_vitals", "params": {}, "data": {"patient_id": "P001", "heart_rate": 82, "bp_systolic": 120, "bp_diastolic": 80, "spo2": 98, "temperature": 37.2}}]
        result = format_rich_response(_FakeAgentResult("Vitals", tr), "webchat")
        if result["blocks"]:
            types = {b["type"] for b in result["blocks"]}
            # webchat supports everything
            assert "data_table" in types
            assert "actions" in types

    def test_slack_strips_chart_and_waveform(self):
        """Slack doesn't list chart/waveform in supported_blocks."""
        tr = [{"tool": "get_vitals_history", "params": {}, "data": {
            "patient_id": "P001",
            "readings": [
                {"timestamp": "2026-02-25T08:00:00", "heart_rate": 72, "bp_systolic": 118, "spo2": 97, "temperature": 36.8},
                {"timestamp": "2026-02-25T09:00:00", "heart_rate": 75, "bp_systolic": 120, "spo2": 98, "temperature": 37.2},
            ],
        }}]
        result = format_rich_response(_FakeAgentResult("History", tr), "slack")
        if result["blocks"]:
            types = {b["type"] for b in result["blocks"]}
            assert "chart" not in types


class TestUnknownChannelFallback:
    def test_unknown_channel_returns_raw_blocks(self):
        """Unknown channel should fall back to webchat (passthrough)."""
        tr = [{"tool": "get_vitals", "params": {}, "data": {"patient_id": "P001", "heart_rate": 82, "bp_systolic": 120, "bp_diastolic": 80, "spo2": 98, "temperature": 37.2}}]
        result = format_rich_response(_FakeAgentResult("Vitals", tr), "unknown_channel")
        # No supported_blocks config → no filtering, webchat passthrough
        if result["blocks"]:
            types = {b["type"] for b in result["blocks"]}
            assert "data_table" in types
            assert "actions" in types


class TestTelegramRendererEdgeCases:
    def test_empty_blocks(self):
        assert _render_telegram([]) == []

    def test_multiple_tables(self):
        blocks = [
            {"type": "data_table", "title": "Vitals", "columns": ["M", "V"], "rows": [["HR", "82"]]},
            {"type": "data_table", "title": "Labs", "columns": ["T", "V"], "rows": [["WBC", "6.8"]]},
        ]
        rendered = _render_telegram(blocks)
        assert len(rendered) == 2
        assert "<b>Vitals</b>" in rendered[0]["html"]
        assert "<b>Labs</b>" in rendered[1]["html"]

    def test_alert_critical_severity(self):
        blocks = [{"type": "alert", "severity": "critical", "text": "Danger"}]
        rendered = _render_telegram(blocks)
        assert "CRITICAL" in rendered[0]["html"]

    def test_alert_info_severity(self):
        blocks = [{"type": "alert", "severity": "info", "text": "Note"}]
        rendered = _render_telegram(blocks)
        assert "INFO" in rendered[0]["html"]

    def test_text_block(self):
        blocks = [{"type": "text", "content": "Hello world"}]
        rendered = _render_telegram(blocks)
        assert rendered[0]["html"] == "Hello world"


class TestSlackRendererEdgeCases:
    def test_key_value_to_section(self):
        blocks = [{"type": "key_value", "title": "Info", "items": [{"key": "A", "value": "1"}]}]
        rendered = _render_slack(blocks)
        assert rendered[0]["type"] == "section"
        assert "*A:*" in rendered[0]["text"]["text"]

    def test_alert_to_section(self):
        blocks = [{"type": "alert", "severity": "warning", "text": "Watch out"}]
        rendered = _render_slack(blocks)
        assert ":warning:" in rendered[0]["text"]["text"]


class TestRenderedImageIntegration:
    """Test that image rendering produces rendered_image blocks for configured channels."""

    _chart_block = {
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
        },
    }

    _waveform_block = {
        "type": "waveform",
        "title": "ECG — P001",
        "sampling_rate_hz": 200,
        "duration_s": 2.5,
        "leads": {"I": [0.1 * i for i in range(100)]},
    }

    def test_telegram_chart_rendered_as_image(self):
        rendered = _render_telegram([self._chart_block])
        assert len(rendered) == 1
        assert rendered[0]["type"] == "rendered_image"
        assert rendered[0]["original_type"] == "chart"
        assert "image_base64" in rendered[0]
        # Verify it's valid base64-encoded PNG
        import base64
        png_bytes = base64.b64decode(rendered[0]["image_base64"])
        assert png_bytes[:4] == b"\x89PNG"

    def test_telegram_waveform_rendered_as_image(self):
        rendered = _render_telegram([self._waveform_block])
        assert len(rendered) == 1
        assert rendered[0]["type"] == "rendered_image"
        assert rendered[0]["original_type"] == "waveform"

    def test_telegram_data_table_not_rendered_as_image(self):
        """Telegram config only renders chart and waveform, not data_table."""
        table_block = {"type": "data_table", "title": "Vitals", "columns": ["M", "V"], "rows": [["HR", "82"]]}
        rendered = _render_telegram([table_block])
        assert rendered[0]["type"] == "text"  # Falls through to HTML rendering

    def test_webchat_chart_passthrough(self):
        """Webchat has no render_as_image — blocks pass through unchanged."""
        rendered = _render_webchat([self._chart_block])
        assert rendered[0]["type"] == "chart"
        assert "image_base64" not in rendered[0]

    def test_whatsapp_data_table_rendered_as_image(self):
        """WhatsApp renders data_table as image."""
        table_block = {"type": "data_table", "title": "Vitals", "columns": ["M", "V"], "rows": [["HR", "82"]]}
        rendered = _render_whatsapp([table_block])
        assert rendered[0]["type"] == "rendered_image"
        assert rendered[0]["original_type"] == "data_table"

    def test_rendered_image_preserves_title(self):
        rendered = _render_telegram([self._chart_block])
        assert rendered[0]["title"] == "Vitals Trend"
