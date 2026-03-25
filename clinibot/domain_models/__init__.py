"""Domain models — specialized inference models for clinical reasoning.

Domain models are injected into skills and provide:
- LLM-based clinical reasoning (cloud or local)
- Rule-based scoring (NEWS2, drug interactions)
- Vision model inference (radiology images)
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("clinibot.domain_models")


@dataclass
class ModelResult:
    """Unified return type from domain model inference."""
    content: str
    findings: list[str] | None = None
    score: dict | None = None
    model_name: str = ""
    model_version: str = ""
    raw: dict | None = None


class DomainModel(ABC):
    """Abstract base for domain models."""

    name: str = ""
    version: str = "1.0"

    @abstractmethod
    async def predict(self, input: dict) -> ModelResult:
        """Run inference. Input schema varies by model."""

    @abstractmethod
    async def is_available(self) -> bool:
        """Check if the model is ready to serve predictions."""
