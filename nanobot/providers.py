"""Multi-provider LLM routing — Ollama and OpenAI-compatible backends."""

import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger("nanobot.providers")


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key: str
    model: str
    phi_safe: bool
    timeout: float = 60.0


HEALTH_CACHE_TTL = 30  # seconds


class LLMProvider(ABC):
    def __init__(self, config: ProviderConfig):
        self.config = config
        self._healthy: bool | None = None
        self._healthy_at: float = 0

    @abstractmethod
    async def chat(self, messages: list[dict]) -> str | None:
        """Send messages and return complete response text."""

    @abstractmethod
    async def chat_stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """Stream response tokens (unused in current SSE design, reserved)."""

    async def is_available(self) -> bool:
        """Health check with TTL cache."""
        now = time.time()
        if self._healthy is not None and (now - self._healthy_at) < HEALTH_CACHE_TTL:
            return self._healthy
        self._healthy = await self._check_health()
        self._healthy_at = now
        return self._healthy

    @abstractmethod
    async def _check_health(self) -> bool:
        """Actual health probe — implemented by subclasses."""


class OllamaProvider(LLMProvider):
    async def chat(self, messages: list[dict]) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout) as client:
                resp = await client.post(
                    f"{self.config.base_url}/api/chat",
                    json={
                        "model": self.config.model,
                        "messages": messages,
                        "stream": False,
                    },
                )
                resp.raise_for_status()
                return resp.json().get("message", {}).get("content", "")
        except Exception as exc:
            logger.warning("Ollama chat failed: %s", exc)
            self._healthy = False
            self._healthy_at = time.time()
            return None

    async def chat_stream(self, messages: list[dict]) -> AsyncIterator[str]:
        # Reserved — not used in current SSE design
        yield ""

    async def _check_health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.config.base_url}/api/tags")
                return resp.status_code == 200
        except Exception:
            return False


class OpenAICompatibleProvider(LLMProvider):
    async def chat(self, messages: list[dict]) -> str | None:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout) as client:
                resp = await client.post(
                    f"{self.config.base_url}/v1/chat/completions",
                    headers=headers,
                    json={
                        "model": self.config.model,
                        "messages": messages,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
        except Exception as exc:
            logger.warning("OpenAI-compatible chat failed: %s", exc)
            self._healthy = False
            self._healthy_at = time.time()
            return None

    async def chat_stream(self, messages: list[dict]) -> AsyncIterator[str]:
        yield ""

    async def _check_health(self) -> bool:
        headers = {}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{self.config.base_url}/v1/models",
                    headers=headers,
                )
                return resp.status_code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_providers: dict[str, LLMProvider] = {}
_default_provider: str | None = None


def _resolve_env(value: str) -> str:
    """Replace ${ENV_VAR} with os.environ value."""
    def replacer(m):
        return os.environ.get(m.group(1), "")
    return re.sub(r"\$\{(\w+)\}", replacer, value)


def load_providers(config_path: str) -> None:
    """Load providers from config.json."""
    global _default_provider
    if not os.path.exists(config_path):
        logger.warning("Provider config not found: %s", config_path)
        return
    with open(config_path) as f:
        data = json.load(f)

    agent_defaults = data.get("agents", {}).get("defaults", {})
    default_model = agent_defaults.get("model", "")
    _default_provider = agent_defaults.get("provider")

    for name, pconf in data.get("providers", {}).items():
        base_url = pconf.get("baseUrl", "")
        api_key = _resolve_env(pconf.get("apiKey", ""))
        model = pconf.get("model", default_model)
        phi_safe = pconf.get("phi_safe", False)
        timeout = pconf.get("timeout", 60.0)

        config = ProviderConfig(
            name=name,
            base_url=base_url,
            api_key=api_key,
            model=model,
            phi_safe=phi_safe,
            timeout=timeout,
        )
        # Ollama has /api/chat endpoint, others use OpenAI-compatible
        if "ollama" in name.lower() or "/api/" in base_url:
            _providers[name] = OllamaProvider(config)
        else:
            _providers[name] = OpenAICompatibleProvider(config)

    logger.info("Loaded %d providers (default=%s)", len(_providers), _default_provider)


def get_provider(name: str | None = None) -> LLMProvider | None:
    """Get a provider by name, or the default."""
    target = name or _default_provider
    if target and target in _providers:
        return _providers[target]
    # Fallback: return first available
    if _providers:
        return next(iter(_providers.values()))
    return None
