"""Clinical summary skill — generates comprehensive patient summary from facts."""

import json
import logging

import clinical_memory
from skills import BaseSkill, SkillInput, SkillOutput

logger = logging.getLogger("clinibot.skills.clinical_summary")


class ClinicalSummarySkill(BaseSkill):
    """Generates a structured patient summary from all clinical facts."""

    name = "clinical_summary"
    domain = "core"
    required_context = []
    interprets_tools = []  # Invoked explicitly by orchestrator

    async def run(self, input: SkillInput) -> SkillOutput:
        patient_id = input.patient_id
        if not patient_id:
            return SkillOutput(
                status="error", domain=self.domain,
                interpretation="patient_id required for clinical summary",
            )

        facts = await clinical_memory.get_facts(patient_id, input.tenant_id, limit=50)
        if not facts:
            return SkillOutput(
                status="partial", domain=self.domain,
                interpretation="No clinical facts available for this patient.",
            )

        # Group by type
        by_type: dict[str, list] = {}
        for f in facts:
            ft = f["fact_type"]
            by_type.setdefault(ft, []).append(f["fact_data"])

        # Build summary
        clinical_model = self.domain_models.get("clinical_reasoning")
        if clinical_model and await clinical_model.is_available():
            summary_data = json.dumps(by_type, indent=2)
            if len(summary_data) > 4000:
                summary_data = summary_data[:4000] + "\n... (truncated)"
            prompt = (
                "Generate a concise clinical summary for this patient. "
                "Organize by: demographics, current vitals, medications, allergies, "
                "lab results, imaging, and any active issues.\n\n"
                f"Clinical facts:\n{summary_data}"
            )
            result = await clinical_model.predict({"prompt": prompt})
            return SkillOutput(
                status="success",
                domain=self.domain,
                interpretation=result.content,
                context_used=list(by_type.keys()),
                provider_name=result.model_name,
                model_name=result.model_version,
            )

        # Fallback: structured text summary
        parts = []
        for ft, items in by_type.items():
            parts.append(f"**{ft}** ({len(items)} records)")
        interpretation = "Patient has records for: " + ", ".join(parts)

        return SkillOutput(
            status="partial",
            domain=self.domain,
            interpretation=interpretation,
            context_used=list(by_type.keys()),
            status_reason="no_llm_provider",
        )
