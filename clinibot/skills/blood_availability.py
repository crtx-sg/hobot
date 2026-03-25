"""Blood availability skill — interprets blood bank results."""

import logging

from skills import BaseSkill, SkillInput, SkillOutput

logger = logging.getLogger("clinibot.skills.blood_availability")


class BloodAvailabilitySkill(BaseSkill):
    """Interprets blood bank availability and crossmatch results."""

    name = "blood_availability"
    domain = "bloodbank"
    required_context = []
    interprets_tools = ["get_blood_availability", "order_blood_crossmatch"]

    async def run(self, input: SkillInput) -> SkillOutput:
        data = input.tool_result
        findings = []

        if input.tool_name == "get_blood_availability":
            for blood_type, info in data.items():
                if isinstance(info, dict):
                    units = info.get("units", info.get("available", 0))
                    if isinstance(units, (int, float)) and units < 5:
                        findings.append(f"Low stock: {blood_type} ({units} units)")
            interpretation = f"Blood bank status reviewed. {len(findings)} low-stock alerts." if findings else "Blood bank inventory adequate."
        elif input.tool_name == "order_blood_crossmatch":
            status = data.get("status", "unknown")
            req_id = data.get("request_id", "")
            interpretation = f"Crossmatch order {req_id}: {status}"
        else:
            interpretation = "Blood bank result processed."

        return SkillOutput(
            status="success",
            domain=self.domain,
            interpretation=interpretation,
            findings=findings,
        )
