"""Service orchestration skill — confirms service requests (ambulance, housekeeping, diet)."""

import logging

from skills import BaseSkill, SkillInput, SkillOutput

logger = logging.getLogger("clinibot.skills.service_orchestration")


class ServiceOrchestrationSkill(BaseSkill):
    """Formats and confirms service request results."""

    name = "service_orchestration"
    domain = "services"
    required_context = []
    interprets_tools = ["request_housekeeping", "order_diet", "request_ambulance", "schedule_appointment"]

    async def run(self, input: SkillInput) -> SkillOutput:
        data = input.tool_result
        req_id = data.get("id", data.get("request_id", ""))
        status = data.get("status", "unknown")
        req_type = data.get("type", input.tool_name.replace("_", " ").title())

        parts = [f"{req_type} — {req_id} ({status})"]

        if input.tool_name == "schedule_appointment":
            for key in ("appointment_id", "doctor", "datetime", "status",
                        "patient_id"):
                val = data.get(key)
                if val:
                    label = key.replace("_", " ").title()
                    parts.append(f"  {label}: {val}")
        else:
            for key in ("patient_id", "diet_type", "meal", "room", "request_type",
                         "from_location", "to_location", "priority"):
                val = data.get(key)
                if val:
                    label = key.replace("_", " ").title()
                    parts.append(f"  {label}: {val}")

        return SkillOutput(
            status="success",
            domain=self.domain,
            interpretation="\n".join(parts),
        )
