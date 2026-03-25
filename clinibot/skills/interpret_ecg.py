"""Interpret ECG skill — auto-fires after get_latest_ecg / get_event_ecg.

Extracted from analyzers.py ECG intercept + ecg_stub preprocessor.

ECG waveform model interface
----------------------------
When an ECG waveform model is integrated, it should receive:
  - samples: array of waveform sample values (per lead)
  - lead_names: list of lead name strings (e.g. ["I", "II", "V1", ...])
  - duration_seconds: recording duration in seconds
  - sampling_rate_hz: samples per second

The structured interface dict is emitted in output.metadata["ecg_interface"]
so a future model can consume it directly.
"""

import json
import logging

from analyzer_prompts import ANALYZER_PROMPTS
from skills import BaseSkill, SkillInput, SkillOutput
from skills.clinical_context import build_patient_context

logger = logging.getLogger("clinibot.skills.interpret_ecg")


def ecg_stub_preprocessor(data: dict) -> tuple[dict, str]:
    """Extract ECG metadata, strip waveform arrays. Returns (metadata, 'limited')."""
    ecg_data = data.get("ecg", data)
    if isinstance(ecg_data, list):
        ecg_data = ecg_data[0] if ecg_data else {}

    meta = {}
    for k, v in ecg_data.items():
        if k not in ("leads", "waveform", "samples"):
            meta[k] = v
    if "leads" in ecg_data and isinstance(ecg_data["leads"], dict):
        meta["lead_names"] = list(ecg_data["leads"].keys())
        meta["lead_count"] = len(ecg_data["leads"])
    elif "lead_names" in ecg_data:
        meta["lead_names"] = ecg_data["lead_names"]
    if "lead_count" not in meta and "lead_names" in meta:
        meta["lead_count"] = len(meta["lead_names"])

    return {"ecg": meta}, "limited"


def _build_ecg_interface(data: dict) -> dict:
    """Build structured ECG interface dict from tool_result for future model consumption."""
    ecg_data = data.get("ecg", data)
    if isinstance(ecg_data, list):
        ecg_data = ecg_data[0] if ecg_data else {}

    # Extract lead names
    lead_names = []
    samples_count = 0
    if "leads" in ecg_data and isinstance(ecg_data["leads"], dict):
        lead_names = list(ecg_data["leads"].keys())
        # Count samples from first lead
        first_lead = next(iter(ecg_data["leads"].values()), [])
        if isinstance(first_lead, list):
            samples_count = len(first_lead)
    elif "lead_names" in ecg_data:
        lead_names = ecg_data["lead_names"]

    if not samples_count:
        samples_count = ecg_data.get("samples_per_lead", 0)

    return {
        "samples_count": samples_count,
        "leads": lead_names,
        "duration_seconds": ecg_data.get("duration_s", ecg_data.get("duration_seconds", 0)),
        "sampling_rate_hz": ecg_data.get("sampling_rate_hz", 0),
    }


class InterpretECGSkill(BaseSkill):
    """Interprets ECG data using clinical reasoning model (metadata only)."""

    name = "interpret_ecg"
    domain = "cardiology"
    required_context = ["demographics"]
    interprets_tools = ["get_latest_ecg", "get_event_ecg"]

    async def run(self, input: SkillInput) -> SkillOutput:
        patient_id = input.patient_id
        if not patient_id:
            return SkillOutput(
                status="error", domain=self.domain,
                interpretation="Cannot interpret ECG without patient_id",
            )

        context_summary, context_found = await build_patient_context(
            patient_id, input.tenant_id,
            ["demographics", "medication"],
        )

        if "demographics" not in context_found:
            logger.info("interpret_ecg skipped: missing demographics for %s", patient_id)
            return SkillOutput(
                status="skipped", domain=self.domain,
                interpretation="",
                status_reason="missing_required_context",
            )

        # Apply ECG stub preprocessor
        data, capability = ecg_stub_preprocessor(input.tool_result)

        # Build structured ECG interface for future waveform model consumption
        ecg_interface = _build_ecg_interface(input.tool_result)

        clinical_model = self.domain_models.get("clinical_reasoning")
        if not clinical_model or not await clinical_model.is_available():
            return SkillOutput(
                status="partial", domain=self.domain,
                interpretation="ECG metadata extracted; waveform model not yet integrated.",
                context_used=context_found,
                context_summary=context_summary,
                status_reason="limited_capability",
                metadata={"ecg_interface": ecg_interface},
            )

        data_str = json.dumps(data, indent=2)
        template = ANALYZER_PROMPTS.get("ecg_analysis", "")
        prompt = template.format(data=data_str, context=context_summary)

        result = await clinical_model.predict({"prompt": prompt})

        # Still partial until waveform model is integrated
        return SkillOutput(
            status="partial",
            domain=self.domain,
            interpretation=result.content,
            context_used=context_found,
            context_summary=context_summary,
            provider_name=result.model_name,
            model_name=result.model_version,
            status_reason="limited_capability",
            metadata={"ecg_interface": ecg_interface},
        )
