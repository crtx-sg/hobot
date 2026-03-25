"""Risk scoring skill — NEWS2/MEWS scoring from vitals with configurable thresholds."""

import logging

from domain_models.vitals_anomaly import DEFAULT_THRESHOLDS
from skills import BaseSkill, SkillInput, SkillOutput

logger = logging.getLogger("clinibot.skills.risk_scoring")


class RiskScoringSkill(BaseSkill):
    """Computes NEWS2/MEWS risk score from vitals data.

    Accepts optional thresholds and scoring system via params.
    """

    name = "risk_scoring"
    domain = "critical_care"
    required_context = []
    interprets_tools = []  # Invoked explicitly, not auto-triggered

    async def run(self, input: SkillInput) -> SkillOutput:
        vitals_model = self.domain_models.get("vitals_anomaly")
        if not vitals_model:
            return SkillOutput(
                status="error", domain=self.domain,
                interpretation="Vitals anomaly model unavailable",
            )

        vitals = input.tool_result or input.context.get("vitals", {})
        history = input.context.get("history", [])
        thresholds = input.params.get("thresholds", DEFAULT_THRESHOLDS)
        scoring = input.params.get("scoring", self.config.get("scoring", "news2"))

        result = await vitals_model.predict({
            "vitals": vitals,
            "history": history,
            "thresholds": thresholds,
            "scoring": scoring,
        })

        return SkillOutput(
            status="success",
            domain=self.domain,
            interpretation=result.content,
            findings=result.findings or [],
            score=result.score,
            provider_name=result.model_name,
            model_name=result.model_version,
        )
