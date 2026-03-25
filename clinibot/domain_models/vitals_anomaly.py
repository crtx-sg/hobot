"""Vitals anomaly model — configurable threshold checking + NEWS2/MEWS scoring."""

import logging

from domain_models import DomainModel, ModelResult

logger = logging.getLogger("clinibot.domain_models.vitals_anomaly")


# ── Default thresholds (used if not overridden by config or patient-specific) ─

DEFAULT_THRESHOLDS = {
    "heart_rate":       {"low": 60, "high": 100, "critical_low": 40, "critical_high": 150},
    "bp_systolic":      {"low": 90, "high": 140, "critical_low": 70, "critical_high": 220},
    "bp_diastolic":     {"low": 60, "high": 90,  "critical_low": 40, "critical_high": 120},
    "spo2":             {"low": 94, "critical_low": 90},
    "temperature":      {"low": 36.1, "high": 38.0, "critical_low": 35.0, "critical_high": 39.5},
    "respiration_rate": {"low": 12, "high": 20, "critical_low": 8, "critical_high": 30},
}


# ── NEWS2 scoring (Royal College of Physicians) ─────────────────────────────

_NEWS2_RANGES = {
    "respiration_rate": [
        (3, lambda v: v <= 8),
        (1, lambda v: 9 <= v <= 11),
        (0, lambda v: 12 <= v <= 20),
        (2, lambda v: 21 <= v <= 24),
        (3, lambda v: v >= 25),
    ],
    "spo2": [
        (3, lambda v: v <= 91),
        (2, lambda v: 92 <= v <= 93),
        (1, lambda v: 94 <= v <= 95),
        (0, lambda v: v >= 96),
    ],
    "heart_rate": [
        (3, lambda v: v <= 40),
        (1, lambda v: 41 <= v <= 50),
        (0, lambda v: 51 <= v <= 90),
        (1, lambda v: 91 <= v <= 110),
        (2, lambda v: 111 <= v <= 130),
        (3, lambda v: v >= 131),
    ],
    "bp_systolic": [
        (3, lambda v: v <= 90),
        (2, lambda v: 91 <= v <= 100),
        (1, lambda v: 101 <= v <= 110),
        (0, lambda v: 111 <= v <= 219),
        (3, lambda v: v >= 220),
    ],
    "temperature": [
        (3, lambda v: v <= 35.0),
        (1, lambda v: 35.1 <= v <= 36.0),
        (0, lambda v: 36.1 <= v <= 38.0),
        (1, lambda v: 38.1 <= v <= 39.0),
        (2, lambda v: v >= 39.1),
    ],
}


# ── MEWS scoring (Modified Early Warning Score) ─────────────────────────────

_MEWS_RANGES = {
    "respiration_rate": [
        (2, lambda v: v < 9),
        (0, lambda v: 9 <= v <= 14),
        (1, lambda v: 15 <= v <= 20),
        (2, lambda v: 21 <= v <= 29),
        (3, lambda v: v >= 30),
    ],
    "heart_rate": [
        (2, lambda v: v < 40),
        (1, lambda v: 40 <= v <= 50),
        (0, lambda v: 51 <= v <= 100),
        (1, lambda v: 101 <= v <= 110),
        (2, lambda v: 111 <= v <= 129),
        (3, lambda v: v >= 130),
    ],
    "bp_systolic": [
        (2, lambda v: v < 70),
        (2, lambda v: 70 <= v <= 80),
        (1, lambda v: 81 <= v <= 100),
        (0, lambda v: 101 <= v <= 199),
        (2, lambda v: v >= 200),
    ],
    "temperature": [
        (2, lambda v: v < 35.0),
        (0, lambda v: 35.0 <= v <= 38.4),
        (2, lambda v: v >= 38.5),
    ],
}


def _score_param(param: str, value: float, ranges: dict) -> int:
    """Score a single parameter against a scoring system's ranges."""
    for score, check in ranges.get(param, []):
        if check(value):
            return score
    return 0


def _ews_risk_level(total: int, scoring: str) -> str:
    """Determine risk level from total EWS score."""
    if scoring == "mews":
        if total >= 5:
            return "high"
        if total >= 3:
            return "medium"
        if total >= 1:
            return "low"
        return "none"
    else:  # news2
        if total >= 7:
            return "high"
        if total >= 5:
            return "medium"
        if total >= 1:
            return "low"
        return "none"


def check_thresholds(vitals: dict, thresholds: dict) -> list[dict]:
    """Check vitals against thresholds. Returns list of threshold violations.

    Each violation: {"param", "value", "level", "direction", "threshold"}
    level is "warning" (outside low/high) or "critical" (outside critical_low/critical_high).
    """
    violations = []
    for param, limits in thresholds.items():
        value = vitals.get(param)
        if value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue

        # Check critical first (more severe)
        crit_low = limits.get("critical_low")
        crit_high = limits.get("critical_high")
        low = limits.get("low")
        high = limits.get("high")

        if crit_low is not None and value <= crit_low:
            violations.append({
                "param": param, "value": value,
                "level": "critical", "direction": "low", "threshold": crit_low,
            })
        elif crit_high is not None and value >= crit_high:
            violations.append({
                "param": param, "value": value,
                "level": "critical", "direction": "high", "threshold": crit_high,
            })
        elif low is not None and value < low:
            violations.append({
                "param": param, "value": value,
                "level": "warning", "direction": "low", "threshold": low,
            })
        elif high is not None and value > high:
            violations.append({
                "param": param, "value": value,
                "level": "warning", "direction": "high", "threshold": high,
            })

    return violations


def format_thresholds_for_prompt(thresholds: dict) -> str:
    """Format thresholds as human-readable text for LLM prompt."""
    lines = []
    for param, limits in thresholds.items():
        name = param.replace("_", " ").title()
        parts = []
        if "low" in limits and "high" in limits:
            parts.append(f"normal {limits['low']}-{limits['high']}")
        elif "low" in limits:
            parts.append(f"normal ≥{limits['low']}")
        if "critical_low" in limits:
            parts.append(f"critical <{limits['critical_low']}")
        if "critical_high" in limits:
            parts.append(f"critical >{limits['critical_high']}")
        lines.append(f"  {name}: {', '.join(parts)}")
    return "\n".join(lines)


class VitalsAnomalyModel(DomainModel):
    """Rule-based EWS scoring, threshold checking, and trend detection.

    Supports NEWS2 (default) and MEWS scoring systems.
    Thresholds are configurable at hospital level (config) and patient level (input).
    """

    name = "vitals_anomaly"
    version = "2.0"

    async def predict(self, input: dict) -> ModelResult:
        """Compute EWS score, check thresholds, and detect anomalies.

        Input keys:
            vitals: dict with heart_rate, bp_systolic, spo2, temperature, etc.
            history: list[dict] (optional) — for trend detection
            thresholds: dict (optional) — merged hospital+patient thresholds
            scoring: str (optional) — "news2" (default) or "mews"
        """
        vitals = input.get("vitals", {})
        history = input.get("history", [])
        thresholds = input.get("thresholds", DEFAULT_THRESHOLDS)
        scoring = input.get("scoring", "news2")

        findings = []

        # ── 1. Threshold violations ──────────────────────────────────────
        violations = check_thresholds(vitals, thresholds)
        for v in violations:
            name = v["param"].replace("_", " ")
            if v["level"] == "critical":
                findings.append(
                    f"CRITICAL: {name}={v['value']} "
                    f"({'below' if v['direction'] == 'low' else 'above'} "
                    f"critical threshold {v['threshold']})"
                )
            else:
                findings.append(
                    f"Abnormal: {name}={v['value']} "
                    f"({'below' if v['direction'] == 'low' else 'above'} "
                    f"threshold {v['threshold']})"
                )

        # ── 2. EWS scoring ───────────────────────────────────────────────
        ranges = _MEWS_RANGES if scoring == "mews" else _NEWS2_RANGES
        score_label = "mews" if scoring == "mews" else "news2"
        total_score = 0
        param_scores = {}

        for param in ranges:
            value = vitals.get(param)
            if value is not None:
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
                s = _score_param(param, value, ranges)
                param_scores[param] = s
                total_score += s

        risk = _ews_risk_level(total_score, scoring)

        if risk == "high":
            findings.insert(0, f"HIGH clinical risk ({score_label.upper()}={total_score})")
        elif risk == "medium":
            findings.insert(0, f"Medium clinical risk ({score_label.upper()}={total_score})")

        # ── 3. Trend detection ───────────────────────────────────────────
        if history and len(history) >= 2:
            for param in ("heart_rate", "bp_systolic", "spo2"):
                values = [h.get(param) for h in history if h.get(param) is not None]
                if len(values) >= 2:
                    trend = values[-1] - values[0]
                    if abs(trend) > 10:
                        direction = "rising" if trend > 0 else "falling"
                        findings.append(
                            f"{param.replace('_', ' ')} trend: {direction} "
                            f"({values[0]} -> {values[-1]})"
                        )

        interpretation = (
            "; ".join(findings) if findings
            else f"Vitals within normal ranges ({score_label.upper()}={total_score})"
        )

        return ModelResult(
            content=interpretation,
            findings=findings,
            score={
                score_label: total_score,
                "risk": risk,
                "params": param_scores,
                "violations": [
                    {"param": v["param"], "value": v["value"],
                     "level": v["level"], "direction": v["direction"]}
                    for v in violations
                ],
            },
            model_name="vitals_anomaly",
            model_version=self.version,
        )

    async def is_available(self) -> bool:
        return True
