"""Radiology vision model — image analysis via vision-capable LLM."""

import base64
import logging
import os

import httpx

import phi
from domain_models import DomainModel, ModelResult
from providers import get_provider

logger = logging.getLogger("clinibot.domain_models.radiology_model")

ORTHANC_BASE = os.environ.get("ORTHANC_BASE", os.environ.get("RADIOLOGY_BASE", "http://synthetic-radiology:8042"))


class RadiologyModel(DomainModel):
    """Vision model for medical image interpretation.

    Fetches DICOM images from Orthanc and sends to vision-capable LLM.
    """

    name = "radiology_model"
    version = "1.0"

    def __init__(self, provider_name: str = "gemini"):
        self.provider_name = provider_name

    async def predict(self, input: dict) -> ModelResult:
        """Interpret a radiology image.

        Expected input keys:
            study_id: str
            prompt: str — clinical prompt with context
        """
        study_id = input.get("study_id", "")
        prompt = input.get("prompt", "")

        image_data = await self._fetch_wado_image(study_id)
        if not image_data:
            return ModelResult(content="Failed to fetch image from Orthanc", model_name=self.provider_name)

        image_b64, media_type = image_data

        provider = get_provider(self.provider_name)
        if not provider:
            return ModelResult(content="Provider unavailable", model_name=self.provider_name)

        # PHI redaction
        phi_mapping = None
        if not provider.config.phi_safe:
            prompt, phi_mapping = phi.redact(prompt)

        messages = self._build_vision_message(prompt, image_b64, media_type)
        result = await provider.chat(messages)
        if not result or not result.content:
            return ModelResult(content="Provider returned no content", model_name=self.provider_name)

        content = result.content
        if phi_mapping:
            content = phi.restore(content, phi_mapping)

        return ModelResult(
            content=content,
            model_name=self.provider_name,
            model_version=provider.config.model,
        )

    def _build_vision_message(self, prompt: str, image_b64: str, media_type: str) -> list[dict]:
        """Build messages list with image for vision-capable providers."""
        data_uri = f"data:{media_type};base64,{image_b64}"

        if "anthropic" in self.provider_name.lower():
            return [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image", "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": image_b64,
                }},
            ]}]

        # OpenAI / Gemini compatible format
        return [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ]}]

    async def _fetch_wado_image(self, study_id: str) -> tuple[str, str] | None:
        """Fetch a preview image from Orthanc. Returns (base64_data, media_type) or None."""
        auth = (os.environ.get("ORTHANC_USER", "orthanc"), os.environ.get("ORTHANC_PASS", "orthanc"))
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(f"{ORTHANC_BASE}/studies/{study_id}/series", auth=auth)
                if resp.status_code != 200:
                    return None
                series = resp.json()
                if not series:
                    return None
                series_id = series[0]["ID"] if isinstance(series[0], dict) else series[0]

                resp = await client.get(f"{ORTHANC_BASE}/series/{series_id}/instances", auth=auth)
                if resp.status_code != 200:
                    return None
                instances = resp.json()
                if not instances:
                    return None
                instance_id = instances[0]["ID"] if isinstance(instances[0], dict) else instances[0]

                resp = await client.get(f"{ORTHANC_BASE}/instances/{instance_id}/preview", auth=auth)
                if resp.status_code != 200:
                    return None

                b64 = base64.b64encode(resp.content).decode("ascii")
                return b64, "image/png"
        except Exception as exc:
            logger.warning("Failed to fetch WADO image for study %s: %s", study_id, exc)
            return None

    async def is_available(self) -> bool:
        provider = get_provider(self.provider_name)
        if not provider:
            return False
        return await provider.is_available()
