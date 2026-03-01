"""Channel-aware response formatting."""

import json
import os
import re

_CHANNELS: dict[str, dict] = {}


def load_channels_config(path: str = "/app/config/channels.json") -> None:
    """Load channel capabilities from config file."""
    global _CHANNELS
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        _CHANNELS = data.get("channels", {})


def format_response(content: str, channel: str) -> str:
    """Format response content according to channel capabilities."""
    cfg = _CHANNELS.get(channel, {})

    # Strip markdown tables if channel doesn't support them
    if not cfg.get("tables", True):
        content = _strip_tables(content)

    # Convert image embeds to links if channel doesn't support images
    if not cfg.get("images", True):
        content = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"[\1](\2)", content)

    # Truncate to max message length
    max_len = cfg.get("max_msg_length")
    if max_len and len(content) > max_len:
        content = content[: max_len - 3] + "..."

    return content


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
            # Separator row (e.g. |---|---|)
            if all(re.match(r"^-+:?$|^:?-+$|^:?-+:?$", c) for c in cells if c):
                in_table = True
                continue
            if not in_table:
                # This is the header row
                headers = cells
                in_table = True
                continue
            # Data row — format as "Header: Value" pairs
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
