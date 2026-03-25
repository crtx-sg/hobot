"""Clinical reasoning model — LLM-based general clinical interpretation."""

import logging

import phi
from domain_models import DomainModel, ModelResult
from providers import get_provider

logger = logging.getLogger("clinibot.domain_models.clinical_reasoning")


class ClinicalReasoningModel(DomainModel):
    """General-purpose clinical interpretation via LLM.

    Used by interpret_labs, interpret_vitals, clinical_summary skills.
    """

    name = "clinical_reasoning"
    version = "1.0"

    def __init__(self, provider_name: str = "gemini"):
        self.provider_name = provider_name

    async def predict(self, input: dict) -> ModelResult:
        """Run clinical reasoning on structured data + context.

        Expected input keys:
            prompt: str — fully formatted prompt
            phi_safe_override: bool — skip PHI redaction if True
        """
        provider = get_provider(self.provider_name)
        if not provider:
            return ModelResult(content="Provider unavailable", model_name=self.provider_name)

        prompt = input["prompt"]

        # PHI redaction for non-phi-safe providers
        phi_mapping = None
        if not provider.config.phi_safe and not input.get("phi_safe_override"):
            prompt, phi_mapping = phi.redact(prompt)

        result = await provider.chat([{"role": "user", "content": prompt}])
        if not result or not result.content:
            return ModelResult(content="No response from provider", model_name=self.provider_name)

        content = result.content
        if phi_mapping:
            content = phi.restore(content, phi_mapping)

        return ModelResult(
            content=content,
            model_name=self.provider_name,
            model_version=provider.config.model,
        )

    async def is_available(self) -> bool:
        provider = get_provider(self.provider_name)
        if not provider:
            return False
        return await provider.is_available()
