"""Drug interaction model — backend + rule-based interaction checking."""

import logging

from domain_models import DomainModel, ModelResult

logger = logging.getLogger("clinibot.domain_models.drug_interaction")

# Common high-severity interaction pairs (rule-based fallback)
_KNOWN_INTERACTIONS = {
    frozenset({"warfarin", "aspirin"}): "Increased bleeding risk",
    frozenset({"metformin", "contrast"}): "Risk of lactic acidosis",
    frozenset({"ssri", "maoi"}): "Serotonin syndrome risk",
    frozenset({"ace_inhibitor", "potassium"}): "Hyperkalemia risk",
    frozenset({"lithium", "nsaid"}): "Lithium toxicity risk",
}


class DrugInteractionModel(DomainModel):
    """Drug interaction checking via pharmacy backend + local rules.

    The pharmacy backend (check_drug_interactions) is the primary source.
    Local rules provide a fallback for common high-severity pairs.
    """

    name = "drug_interaction"
    version = "1.0"

    async def predict(self, input: dict) -> ModelResult:
        """Check drug interactions.

        Expected input keys:
            medications: list[str] — medication names
            backend_result: dict | None — result from check_drug_interactions tool
        """
        medications = input.get("medications", [])
        backend_result = input.get("backend_result")

        findings = []

        # Use backend result if available
        if backend_result and "interactions" in backend_result:
            for interaction in backend_result["interactions"]:
                severity = interaction.get("severity", "unknown")
                desc = interaction.get("description", "")
                drugs = interaction.get("drugs", [])
                findings.append(f"{' + '.join(drugs)}: {desc} (severity: {severity})")

        # Supplement with local rules
        med_lower = {m.lower() for m in medications}
        for pair, desc in _KNOWN_INTERACTIONS.items():
            if pair.issubset(med_lower):
                finding = f"{' + '.join(pair)}: {desc}"
                if finding not in findings:
                    findings.append(finding)

        if findings:
            interpretation = "Drug interactions found:\n" + "\n".join(f"- {f}" for f in findings)
        else:
            interpretation = "No known drug interactions detected."

        return ModelResult(
            content=interpretation,
            findings=findings,
            model_name="drug_interaction",
            model_version=self.version,
        )

    async def is_available(self) -> bool:
        return True  # Local rules always available
