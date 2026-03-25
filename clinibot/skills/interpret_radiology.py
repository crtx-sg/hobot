"""Interpret radiology skill — auto-fires after get_report.

Extracted from analyzers.py intercept for get_report.
Also provides analyze_radiology_image as an LLM-dispatched tool.
"""

import json
import logging

from analyzer_prompts import ANALYZER_PROMPTS
from skills import BaseSkill, SkillInput, SkillOutput
from skills.clinical_context import build_patient_context

logger = logging.getLogger("clinibot.skills.interpret_radiology")


class InterpretRadiologySkill(BaseSkill):
    """Summarizes radiology reports (auto-intercept) and interprets images (tool)."""

    name = "interpret_radiology"
    domain = "radiology"
    required_context = []  # Report summary needs no context
    interprets_tools = ["get_report"]

    async def run(self, input: SkillInput) -> SkillOutput:
        # For get_report: summarize the radiology report
        patient_id = input.patient_id
        context_summary = ""
        context_found = []

        if patient_id:
            context_summary, context_found = await build_patient_context(
                patient_id, input.tenant_id,
                ["demographics", "medication", "imaging_study"],
            )

        clinical_model = self.domain_models.get("clinical_reasoning")
        if not clinical_model or not await clinical_model.is_available():
            return SkillOutput(
                status="error", domain=self.domain,
                interpretation="Clinical reasoning model unavailable",
                status_reason="provider_unavailable",
            )

        data_str = json.dumps(input.tool_result, indent=2)
        template = ANALYZER_PROMPTS.get("radiology_report_summary", "")
        prompt = template.format(data=data_str, context=context_summary)

        result = await clinical_model.predict({"prompt": prompt})

        return SkillOutput(
            status="success",
            domain=self.domain,
            interpretation=result.content,
            context_used=context_found,
            context_summary=context_summary,
            provider_name=result.model_name,
            model_name=result.model_version,
        )


class AnalyzeRadiologyImageSkill(BaseSkill):
    """Vision-based radiology image interpretation (LLM-dispatched tool)."""

    name = "analyze_radiology_image"
    domain = "radiology"
    required_context = []
    interprets_tools = []  # Not auto-invoked, dispatched by LLM as a tool

    async def run(self, input: SkillInput) -> SkillOutput:
        patient_id = input.patient_id
        study_id = input.params.get("study_id", "")

        if not patient_id:
            return SkillOutput(
                status="error", domain=self.domain,
                interpretation="patient_id is required",
            )
        if not study_id:
            return SkillOutput(
                status="error", domain=self.domain,
                interpretation="study_id is required for image analysis",
            )

        context_summary, context_found = await build_patient_context(
            patient_id, input.tenant_id,
            ["demographics", "medication", "imaging_study"],
        )

        radiology_model = self.domain_models.get("radiology_model")
        if not radiology_model or not await radiology_model.is_available():
            return SkillOutput(
                status="error", domain=self.domain,
                interpretation="Radiology vision model unavailable",
                context_used=context_found,
                context_summary=context_summary,
                status_reason="provider_unavailable",
            )

        template = ANALYZER_PROMPTS.get("radiology_image_analysis", "")
        prompt = template.format(data="", context=context_summary)

        result = await radiology_model.predict({
            "study_id": study_id,
            "prompt": prompt,
        })

        status_reasons = []
        missing = {"demographics", "medication", "imaging_study"} - set(context_found)
        if missing:
            status_reasons.append("incomplete_context")

        return SkillOutput(
            status="success" if not status_reasons else "partial",
            domain=self.domain,
            interpretation=result.content,
            context_used=context_found,
            context_summary=context_summary,
            provider_name=result.model_name,
            model_name=result.model_version,
            status_reason=",".join(status_reasons) if status_reasons else None,
        )

    def tool_definition(self) -> dict:
        """Expose as a callable tool for the LLM."""
        return {
            "name": "analyze_radiology_image",
            "description": "Interpret a medical image (X-ray, CT, etc.) using a vision model with patient context. Requires study_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_id": {"type": "string"},
                    "study_id": {"type": "string"},
                },
                "required": ["patient_id", "study_id"],
            },
        }
