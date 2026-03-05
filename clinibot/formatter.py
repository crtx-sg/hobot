"""Channel-aware response formatting with structured block rendering."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent import AgentResult

logger = logging.getLogger(__name__)

_CHANNELS: dict[str, dict] = {}


def load_channels_config(path: str = "/app/config/channels.json") -> None:
    """Load channel capabilities from config file."""
    global _CHANNELS
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        _CHANNELS = data.get("channels", {})


def format_response(content: str, channel: str) -> str:
    """Format response content according to channel capabilities (text only)."""
    cfg = _CHANNELS.get(channel, {})

    if not cfg.get("tables", True):
        content = _strip_tables(content)

    if not cfg.get("images", True):
        content = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"[\1](\2)", content)

    max_len = cfg.get("max_msg_length")
    if max_len and len(content) > max_len:
        content = content[: max_len - 3] + "..."

    return content


def format_rich_response(agent_result: AgentResult, channel: str) -> dict:
    """Build a full rich response dict with text + blocks for the given channel."""
    from blocks import build_blocks

    text = format_response(agent_result.text, channel)
    blocks = build_blocks(agent_result.tool_results)

    # Filter blocks by channel-supported types
    cfg = _CHANNELS.get(channel, {})
    supported = cfg.get("supported_blocks")
    if supported is not None:
        blocks = [b for b in blocks if b.get("type") in supported]

    # Render blocks for the specific channel
    rendered = render_blocks(blocks, channel)

    return {"text": text, "blocks": rendered if rendered else None}


# ---------------------------------------------------------------------------
# Block rendering per channel
# ---------------------------------------------------------------------------

def render_blocks(blocks: list[dict], channel: str) -> list[dict]:
    """Render abstract blocks into channel-native format."""
    renderer = _RENDERERS.get(channel, _render_webchat)
    return renderer(blocks)


def _render_webchat(blocks: list[dict]) -> list[dict]:
    """Webchat: pass blocks through as-is for frontend rendering."""
    return blocks


def _render_telegram(blocks: list[dict]) -> list[dict]:
    """Telegram: convert blocks to HTML text + inline keyboard dicts."""
    rendered = []
    for block in blocks:
        btype = block.get("type")
        # Try server-side image rendering for eligible block types
        if btype in ("chart", "waveform", "data_table"):
            if _try_render_image(block, rendered, "telegram"):
                continue
        if btype == "data_table":
            rendered.append(_tg_data_table(block))
        elif btype == "key_value":
            rendered.append(_tg_key_value(block))
        elif btype == "alert":
            rendered.append(_tg_alert(block))
        elif btype == "text":
            rendered.append({"type": "text", "html": block.get("content", "")})
        elif btype == "actions":
            rendered.append(_tg_actions(block))
        elif btype == "confirmation":
            rendered.append(_tg_confirmation(block))
        elif btype == "image":
            rendered.append({
                "type": "image",
                "url": block.get("url", ""),
                "alt": block.get("alt", ""),
                "mime_type": block.get("mime_type", "image/png"),
            })
        elif btype == "chart":
            rendered.append({
                "type": "chart",
                "title": block.get("title", ""),
                "chart_type": block.get("chart_type", "line"),
                "series": block.get("series", {}),
            })
        elif btype == "waveform":
            rendered.append({
                "type": "waveform",
                "title": block.get("title", ""),
                "sampling_rate_hz": block.get("sampling_rate_hz"),
                "duration_s": block.get("duration_s"),
                "leads": block.get("leads", {}),
            })
        else:
            rendered.append(block)
    return rendered


def _tg_data_table(block: dict) -> dict:
    """Render a data_table as Telegram HTML."""
    title = block.get("title", "")
    cols = block.get("columns", [])
    rows = block.get("rows", [])
    lines = [f"<b>{title}</b>"]
    # Build aligned text table
    for row in rows:
        pairs = []
        for i, cell in enumerate(row):
            label = cols[i] if i < len(cols) else ""
            pairs.append(f"{label}: {cell}")
        lines.append("  ".join(pairs))
    return {"type": "text", "html": "\n".join(lines)}


def _tg_key_value(block: dict) -> dict:
    """Render key_value block as Telegram HTML."""
    title = block.get("title", "")
    items = block.get("items", [])
    lines = [f"<b>{title}</b>"]
    for item in items:
        lines.append(f"<b>{item['key']}:</b> {item['value']}")
    return {"type": "text", "html": "\n".join(lines)}


def _tg_alert(block: dict) -> dict:
    """Render alert as Telegram HTML with emoji."""
    severity = block.get("severity", "info")
    emoji = {"critical": "\u26a0\ufe0f", "warning": "\u26a0\ufe0f", "info": "\u2139\ufe0f"}.get(severity, "\u2139\ufe0f")
    text = block.get("text", "")
    return {"type": "text", "html": f"{emoji} <b>{severity.upper()}:</b> {text}"}


def _tg_actions(block: dict) -> dict:
    """Render actions as Telegram inline keyboard."""
    buttons = block.get("buttons", [])
    keyboard = []
    for btn in buttons:
        cb = json.dumps({"a": btn["action"], "p": btn.get("params", {})}, separators=(",", ":"))
        keyboard.append({
            "text": btn["label"],
            "callback_data": cb,
        })
    return {"type": "inline_keyboard", "buttons": keyboard}


def _tg_confirmation(block: dict) -> dict:
    """Render confirmation block as text + inline keyboard."""
    text = block.get("text", "")
    cid = block.get("confirmation_id", "")
    return {
        "type": "confirmation",
        "html": f"\u26a0\ufe0f <b>Confirmation Required</b>\n{text}",
        "buttons": [{
            "text": "Confirm",
            "callback_data": json.dumps({"a": "confirm", "p": {"confirmation_id": cid}}, separators=(",", ":")),
        }],
    }


def _render_slack(blocks: list[dict]) -> list[dict]:
    """Slack: convert to Block Kit sections. Stub — returns simplified blocks."""
    rendered = []
    for block in blocks:
        btype = block.get("type")
        # Try server-side image rendering for eligible block types
        if btype in ("chart", "waveform", "data_table"):
            if _try_render_image(block, rendered, "slack"):
                continue
        if btype == "data_table":
            title = block.get("title", "")
            cols = block.get("columns", [])
            rows = block.get("rows", [])
            lines = [f"*{title}*"]
            for row in rows:
                pairs = [f"{cols[i]}: {cell}" if i < len(cols) else cell for i, cell in enumerate(row)]
                lines.append("  ".join(pairs))
            rendered.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
        elif btype == "key_value":
            title = block.get("title", "")
            items = block.get("items", [])
            lines = [f"*{title}*"] + [f"*{it['key']}:* {it['value']}" for it in items]
            rendered.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
        elif btype == "alert":
            emoji = ":warning:" if block.get("severity") in ("critical", "warning") else ":information_source:"
            rendered.append({"type": "section", "text": {"type": "mrkdwn", "text": f"{emoji} *{block.get('severity', '').upper()}:* {block.get('text', '')}"}})
        elif btype == "actions":
            elements = []
            for btn in block.get("buttons", []):
                elements.append({"type": "button", "text": {"type": "plain_text", "text": btn["label"]}, "value": json.dumps({"action": btn["action"], "params": btn.get("params", {})})})
            rendered.append({"type": "actions", "elements": elements})
        elif btype == "image":
            rendered.append({"type": "image", "image_url": block.get("url", ""), "alt_text": block.get("alt", "")})
        else:
            rendered.append(block)
    return rendered


def _render_whatsapp(blocks: list[dict]) -> list[dict]:
    """WhatsApp: plain text with numbered lists for actions."""
    rendered = []
    for block in blocks:
        btype = block.get("type")
        # Try server-side image rendering for eligible block types
        if btype in ("chart", "waveform", "data_table"):
            if _try_render_image(block, rendered, "whatsapp"):
                continue
        if btype == "data_table":
            title = block.get("title", "")
            cols = block.get("columns", [])
            rows = block.get("rows", [])
            lines = [f"*{title}*"]
            for row in rows:
                pairs = [f"{cols[i]}: {cell}" if i < len(cols) else cell for i, cell in enumerate(row)]
                lines.append("  ".join(pairs))
            rendered.append({"type": "text", "content": "\n".join(lines)})
        elif btype == "key_value":
            items = block.get("items", [])
            lines = [f"*{block.get('title', '')}*"] + [f"*{it['key']}:* {it['value']}" for it in items]
            rendered.append({"type": "text", "content": "\n".join(lines)})
        elif btype == "actions":
            lines = []
            for i, btn in enumerate(block.get("buttons", []), 1):
                lines.append(f"{i}. {btn['label']}")
            rendered.append({"type": "text", "content": "\n".join(lines)})
        elif btype == "image":
            rendered.append({"type": "image", "url": block.get("url", ""), "alt": block.get("alt", "")})
        else:
            rendered.append(block)
    return rendered


def _get_render_as_image(channel: str) -> set[str]:
    """Return the set of block types that should be rendered as images for this channel."""
    cfg = _CHANNELS.get(channel, {})
    return set(cfg.get("render_as_image", []))


def _render_block_to_image(block: dict) -> bytes:
    """Render a chart/waveform/data_table block to PNG bytes."""
    from renderers import render_chart, render_waveform, render_table

    btype = block.get("type")
    if btype == "chart":
        return render_chart(block)
    elif btype == "waveform":
        return render_waveform(block)
    elif btype == "data_table":
        return render_table(block)
    raise ValueError(f"Cannot render block type: {btype}")


def _try_render_image(block: dict, rendered: list[dict], channel: str) -> bool:
    """Attempt to render a block as an image. Returns True if handled."""
    btype = block.get("type", "")
    render_as_image = _get_render_as_image(channel)
    if btype not in render_as_image:
        return False
    try:
        png_bytes = _render_block_to_image(block)
        rendered.append({
            "type": "rendered_image",
            "image_base64": base64.b64encode(png_bytes).decode(),
            "title": block.get("title", ""),
            "original_type": btype,
        })
        return True
    except Exception:
        logger.exception("Failed to render %s block as image", btype)
        return False


_RENDERERS = {
    "webchat": _render_webchat,
    "telegram": _render_telegram,
    "slack": _render_slack,
    "whatsapp": _render_whatsapp,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_tables(text: str) -> str:
    """Convert markdown tables to plain-text key:value lines."""
    lines = text.split("\n")
    result = []
    headers = []
    in_table = False

    for line in lines:
        stripped = line.strip()
        if "|" in stripped and not stripped.startswith("```"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(re.match(r"^-+:?$|^:?-+$|^:?-+:?$", c) for c in cells if c):
                in_table = True
                continue
            if not in_table:
                headers = cells
                in_table = True
                continue
            pairs = []
            for i, cell in enumerate(cells):
                label = headers[i] if i < len(headers) else f"col{i}"
                pairs.append(f"{label}: {cell}")
            result.append("  ".join(pairs))
        else:
            in_table = False
            headers = []
            result.append(line)

    return "\n".join(result)
