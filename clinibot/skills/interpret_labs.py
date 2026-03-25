"""Interpret labs skill — auto-fires after get_lab_results.

Extracted from analyzers.py intercept for get_lab_results.
"""

import json
import logging

from analyzer_prompts import ANALYZER_PROMPTS
from skills import BaseSkill, SkillInput, SkillOutput
from skills.clinical_context import build_patient_context

logger = logging.getLogger("clinibot.skills.interpret_labs")


class InterpretLabsSkill(BaseSkill):
    """Interprets lab results using clinical reasoning model."""

    name = "interpret_labs"
    domain = "pathology"
    required_context = ["demographics"]
    interprets_tools = ["get_lab_results"]

    async def run(self, input: SkillInput) -> SkillOutput:
        patient_id = input.patient_id
        if not patient_id:
            return SkillOutput(
                status="error", domain=self.domain,
                interpretation="Cannot interpret labs without patient_id",
            )

        context_summary, context_found = await build_patient_context(
            patient_id, input.tenant_id,
            ["demographics", "medication", "allergy"],
        )

        if "demographics" not in context_found:
            logger.info("interpret_labs skipped: missing demographics for %s", patient_id)
            return SkillOutput(
                status="skipped", domain=self.domain,
                interpretation="",
                status_reason="missing_required_context",
            )

        clinical_model = self.domain_models.get("clinical_reasoning")
        if not clinical_model or not await clinical_model.is_available():
            return SkillOutput(
                status="error", domain=self.domain,
                interpretation="Clinical reasoning model unavailable",
                context_used=context_found,
                context_summary=context_summary,
                status_reason="provider_unavailable",
            )

        data_str = json.dumps(input.tool_result, indent=2)
        template = ANALYZER_PROMPTS.get("lab_analysis", "")
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
