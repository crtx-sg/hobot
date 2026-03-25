"""Care plan summary skill — summarizes aggregated care plan data."""

import json
import logging

from skills import BaseSkill, SkillInput, SkillOutput

logger = logging.getLogger("clinibot.skills.care_plan_summary")


class CarePlanSummarySkill(BaseSkill):
    """Summarizes aggregated care plan (orders, appointments, reminders, pending labs)."""

    name = "care_plan_summary"
    domain = "core"
    required_context = []
    interprets_tools = ["get_care_plan"]

    async def run(self, input: SkillInput) -> SkillOutput:
        patient_id = input.patient_id or input.params.get("patient_id", "")
        data = input.tool_result
        if not data or "error" in data:
            return SkillOutput(
                status="error",
                domain=self.domain,
                interpretation=data.get("error", "No care plan data available"),
            )

        orders = data.get("orders", [])
        appointments = data.get("appointments", [])
        reminders = data.get("reminders", [])
        pending_labs = data.get("pending_labs", [])

        # Try LLM summarization
        clinical_model = self.domain_models.get("clinical_reasoning")
        if clinical_model and await clinical_model.is_available():
            summary_data = json.dumps(data, indent=2)
            if len(summary_data) > 4000:
                summary_data = summary_data[:4000] + "\n... (truncated)"
            prompt = (
                f"Summarize the following care plan for patient {patient_id}. "
                "Highlight urgent items, upcoming tasks, and pending results.\n\n"
                f"{summary_data}"
            )
            try:
                result = await clinical_model.predict({"prompt": prompt})
                return SkillOutput(
                    status="success",
                    domain=self.domain,
                    interpretation=result.content,
                    findings=[
                        f"{len(orders)} active orders",
                        f"{len(appointments)} appointments",
                        f"{len(reminders)} reminders",
                        f"{len(pending_labs)} pending lab results",
                    ],
                    provider_name=result.model_name,
                    model_name=result.model_version,
                )
            except Exception as exc:
                logger.warning("LLM summarization failed, falling back: %s", exc)

        # Fallback: structured text
        parts = [f"Care plan for patient {patient_id}:"]
        parts.append(f"- **Orders**: {len(orders)} active")
        if orders:
            for o in orders[:3]:
                desc = o.get("description") or o.get("code", {}).get("text", "order")
                parts.append(f"  - {desc}")
            if len(orders) > 3:
                parts.append(f"  - ... and {len(orders) - 3} more")

        parts.append(f"- **Appointments**: {len(appointments)} scheduled")
        if appointments:
            for a in appointments[:3]:
                dt = a.get("datetime", "")
                doctor = a.get("doctor", "")
                parts.append(f"  - {doctor} at {dt}")

        parts.append(f"- **Reminders**: {len(reminders)}")
        parts.append(f"- **Pending labs**: {len(pending_labs)}")
        if pending_labs:
            for lab in pending_labs[:3]:
                test = lab.get("test_name") or lab.get("test", "lab test")
                parts.append(f"  - {test} ({lab.get('status', 'pending')})")

        return SkillOutput(
            status="partial",
            domain=self.domain,
            interpretation="\n".join(parts),
            findings=[
                f"{len(orders)} active orders",
                f"{len(appointments)} appointments",
                f"{len(reminders)} reminders",
                f"{len(pending_labs)} pending lab results",
            ],
            status_reason="no_llm_provider",
        )
