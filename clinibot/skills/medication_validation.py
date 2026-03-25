"""Medication validation skill — wraps check_drug_interactions.

Uses drug_interaction domain model for rule-based supplementation.
"""

import logging

from skills import BaseSkill, SkillInput, SkillOutput

logger = logging.getLogger("clinibot.skills.medication_validation")


class MedicationValidationSkill(BaseSkill):
    """Validates drug interactions using backend + local rules."""

    name = "medication_validation"
    domain = "pharmacy"
    required_context = []
    interprets_tools = ["check_drug_interactions"]

    async def run(self, input: SkillInput) -> SkillOutput:
        drug_model = self.domain_models.get("drug_interaction")
        if not drug_model:
            return SkillOutput(
                status="error", domain=self.domain,
                interpretation="Drug interaction model unavailable",
            )

        # Extract medication names from params or result
        medications = input.params.get("medications", [])
        if not medications and "drugs" in input.tool_result:
            medications = input.tool_result["drugs"]

        result = await drug_model.predict({
            "medications": medications,
            "backend_result": input.tool_result,
        })

        return SkillOutput(
            status="success",
            domain=self.domain,
            interpretation=result.content,
            findings=result.findings or [],
            provider_name=result.model_name,
            model_name=result.model_version,
        )
