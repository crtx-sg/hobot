"""Server-side rendering of chart, waveform, and data_table blocks to PNG images."""

from __future__ import annotations

import io
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# Clinical color palette
_COLORS = ["#2563eb", "#dc2626", "#16a34a", "#d97706", "#7c3aed", "#db2777", "#0891b2", "#65a30d"]


def render_chart(block: dict) -> bytes:
    """Render a chart block to PNG bytes.

    Expected block keys: chart_type, title, x_label, y_label, series
    series is a dict of {name: [{t: timestamp_str, v: value}, ...]}
    """
    series = block.get("series", {})
    title = block.get("title", "")
    x_label = block.get("x_label", "")
    y_label = block.get("y_label", "")

    fig, ax = plt.subplots(figsize=(8, 5), dpi=100)

    color_idx = 0
    for name, points in series.items():
        if not points:
            continue
        times = []
        values = []
        for p in points:
            v = p.get("v")
            if v is None:
                continue
            t_str = p.get("t", "")
            try:
                times.append(datetime.fromisoformat(t_str))
            except (ValueError, TypeError):
                continue
            values.append(v)
        if times and values:
            color = _COLORS[color_idx % len(_COLORS)]
            label = name.replace("_", " ").title()
            ax.plot(times, values, marker="o", markersize=4, linewidth=2,
                    color=color, label=label)
            color_idx += 1

    ax.set_title(title, fontsize=14, fontweight="bold", pad=12)
    ax.set_xlabel(x_label, fontsize=11)
    ax.set_ylabel(y_label, fontsize=11)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    fig.autofmt_xdate(rotation=30)

    if color_idx > 0:
        ax.legend(fontsize=9, loc="best")

    fig.tight_layout()
    return _fig_to_png(fig)


def render_waveform(block: dict) -> bytes:
    """Render a waveform block (e.g. ECG) to PNG bytes.

    Expected block keys: title, sampling_rate_hz, duration_s, leads
    leads is a dict of {lead_name: [sample_values]}
    """
    leads = block.get("leads", {})
    title = block.get("title", "")
    sampling_rate = block.get("sampling_rate_hz", 200)
    duration = block.get("duration_s", 12)

    lead_names = list(leads.keys())
    n_leads = max(len(lead_names), 1)

    fig, axes = plt.subplots(n_leads, 1, figsize=(12, max(2 * n_leads, 4)), dpi=100,
                             sharex=True, squeeze=False)

    for i, lead_name in enumerate(lead_names):
        ax = axes[i][0]
        samples = leads[lead_name]
        if not samples:
            continue
        n_samples = len(samples)
        t = [j / sampling_rate for j in range(n_samples)]
        ax.plot(t, samples, linewidth=0.8, color="#1a1a1a")
        ax.set_ylabel(lead_name, fontsize=9, fontweight="bold")
        # Clinical ECG grid
        ax.grid(True, which="major", color="#ffcccc", linewidth=0.5)
        ax.grid(True, which="minor", color="#ffe6e6", linewidth=0.3)
        ax.minorticks_on()
        ax.set_xlim(0, duration)

    if lead_names:
        axes[-1][0].set_xlabel("Time (s)", fontsize=10)

    fig.suptitle(title, fontsize=14, fontweight="bold", y=1.0)
    fig.tight_layout()
    return _fig_to_png(fig)


def render_table(block: dict) -> bytes:
    """Render a data_table block to PNG bytes.

    Expected block keys: title, columns, rows
    """
    title = block.get("title", "")
    columns = block.get("columns", [])
    rows = block.get("rows", [])

    n_rows = len(rows)
    # Dynamic height: header + rows, min 2 inches
    fig_height = max(1.0 + 0.35 * n_rows, 2.0)
    fig, ax = plt.subplots(figsize=(8, fig_height), dpi=100)
    ax.axis("off")

    if not columns and not rows:
        ax.text(0.5, 0.5, title or "No data", ha="center", va="center", fontsize=14)
        fig.tight_layout()
        return _fig_to_png(fig)

    table = ax.table(cellText=rows, colLabels=columns, loc="center", cellLoc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1, 1.4)

    # Style header row
    for j in range(len(columns)):
        cell = table[0, j]
        cell.set_facecolor("#2563eb")
        cell.set_text_props(color="white", fontweight="bold")

    # Alternating row colors
    for i in range(len(rows)):
        color = "#f0f4ff" if i % 2 == 0 else "#ffffff"
        for j in range(len(columns)):
            table[i + 1, j].set_facecolor(color)

    ax.set_title(title, fontsize=14, fontweight="bold", pad=20)
    fig.tight_layout()
    return _fig_to_png(fig)


def _fig_to_png(fig) -> bytes:
    """Convert a matplotlib figure to PNG bytes and close it."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
