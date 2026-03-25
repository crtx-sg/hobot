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

logger = logging.getLogger("clinibot.providers")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    """A single tool invocation returned by the provider."""
    id: str           # provider-assigned call ID
    name: str
    params: dict


@dataclass
class ChatResult:
    """Unified return type from LLMProvider.chat."""
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_message: dict | None = None  # raw assistant message for re-feeding


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key: str
    model: str
    phi_safe: bool
    timeout: float = 60.0
    supports_vision: bool = False


HEALTH_CACHE_TTL = 30  # seconds


def _to_openai_tool(tool_def: dict) -> dict:
    """Convert canonical tool def to OpenAI function-calling format."""
    return {
        "type": "function",
        "function": {
            "name": tool_def["name"],
            "description": tool_def.get("description", ""),
            "parameters": tool_def.get("parameters", {"type": "object", "properties": {}}),
        },
    }


_RETRYABLE_STATUS = {429, 502, 503, 529}
_MAX_RETRIES = 2
_RETRY_BACKOFF = (1.0, 3.0)  # seconds per attempt


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    json: dict | None = None,
    provider_name: str = "",
) -> httpx.Response:
    """HTTP request with retry on 429 / 5xx. Raises on final failure."""
    import asyncio as _asyncio

    last_resp = None
    for attempt in range(1 + _MAX_RETRIES):
        if method == "GET":
            resp = await client.get(url, headers=headers)
        else:
            resp = await client.post(url, headers=headers, json=json)
        if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
            wait = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
            logger.warning("%s %d — retry %d/%d in %.1fs",
                           provider_name, resp.status_code, attempt + 1, _MAX_RETRIES, wait)
            await _asyncio.sleep(wait)
            last_resp = resp
            continue
        resp.raise_for_status()
        return resp
    # Should not reach here, but just in case
    last_resp.raise_for_status()
    return last_resp


class LLMProvider(ABC):
    def __init__(self, config: ProviderConfig):
        self.config = config
        self._healthy: bool | None = None
        self._healthy_at: float = 0

    @abstractmethod
    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> ChatResult | None:
        """Send messages and return ChatResult."""

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
        logger.info("health_check provider=%s healthy=%s", self.config.name, self._healthy)
        return self._healthy

    @abstractmethod
    async def _check_health(self) -> bool:
        """Actual health probe — implemented by subclasses."""


class OllamaProvider(LLMProvider):
    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> ChatResult | None:
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout) as client:
                # Single user message with no tools → use /api/generate
                # (better results with base/completion models like OpenBioLLM)
                if len(messages) == 1 and messages[0]["role"] == "user" and not tools:
                    resp = await _request_with_retry(
                        client, "POST",
                        f"{self.config.base_url}/api/generate",
                        json={
                            "model": self.config.model,
                            "prompt": messages[0]["content"],
                            "stream": False,
                            "options": {"num_predict": 2048},
                        },
                        provider_name="ollama",
                    )
                    content = resp.json().get("response", "")
                else:
                    resp = await _request_with_retry(
                        client, "POST",
                        f"{self.config.base_url}/api/chat",
                        json={
                            "model": self.config.model,
                            "messages": messages,
                            "stream": False,
                            "options": {"num_predict": 2048},
                        },
                        provider_name="ollama",
                    )
                    content = resp.json().get("message", {}).get("content", "")
                return ChatResult(content=content)
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
    """Provider for any OpenAI-compatible API (OpenAI, Anthropic, vLLM, etc.)."""

    def _chat_url(self) -> str:
        return f"{self.config.base_url}/v1/chat/completions"

    def _models_url(self) -> str:
        return f"{self.config.base_url}/v1/models"

    def _auth_headers(self) -> dict:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> ChatResult | None:
        try:
            body: dict = {
                "model": self.config.model,
                "messages": messages,
            }
            if tools:
                body["tools"] = [_to_openai_tool(t) for t in tools]
            async with httpx.AsyncClient(timeout=self.config.timeout) as client:
                resp = await _request_with_retry(
                    client, "POST",
                    self._chat_url(),
                    headers=self._auth_headers(),
                    json=body,
                    provider_name=self.config.name,
                )
                data = resp.json()
                msg = data["choices"][0]["message"]
                content = msg.get("content")

                # Parse native tool_calls if present
                raw_tool_calls = msg.get("tool_calls") or []
                parsed: list[ToolCall] = []
                for tc in raw_tool_calls:
                    fn = tc.get("function", {})
                    args = fn.get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    parsed.append(ToolCall(
                        id=tc.get("id", ""),
                        name=fn.get("name", ""),
                        params=args,
                    ))

                return ChatResult(
                    content=content,
                    tool_calls=parsed,
                    raw_message=msg if parsed else None,
                )
        except Exception as exc:
            logger.warning("OpenAI-compatible chat failed: %s", exc)
            self._healthy = False
            self._healthy_at = time.time()
            return None

    async def chat_stream(self, messages: list[dict]) -> AsyncIterator[str]:
        yield ""

    async def _check_health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    self._models_url(),
                    headers=self._auth_headers(),
                )
                return resp.status_code == 200
        except Exception:
            return False


class GeminiProvider(OpenAICompatibleProvider):
    """Google Gemini via its OpenAI-compatible endpoint."""

    def _chat_url(self) -> str:
        return f"{self.config.base_url}/chat/completions"

    def _models_url(self) -> str:
        return f"{self.config.base_url}/models"


class AnthropicProvider(LLMProvider):
    """Anthropic via the native Messages API (/v1/messages)."""

    ANTHROPIC_VERSION = "2023-06-01"

    def _messages_url(self) -> str:
        return f"{self.config.base_url}/v1/messages"

    def _auth_headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.config.api_key,
            "anthropic-version": self.ANTHROPIC_VERSION,
        }

    # -- format conversions ---------------------------------------------------

    @staticmethod
    def _to_anthropic_tools(tool_defs: list[dict]) -> list[dict]:
        """Convert canonical tool defs to Anthropic tool format."""
        out = []
        for t in tool_defs:
            out.append({
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get("parameters", {"type": "object", "properties": {}}),
            })
        return out

    @staticmethod
    def _convert_messages(messages: list[dict]) -> tuple[str, list[dict]]:
        """Convert agent-loop messages to Anthropic format.

        Returns (system_prompt, anthropic_messages).
        The agent loop uses OpenAI-style messages:
          - {role: "system", content: "..."}
          - {role: "assistant", content: [...]}   ← raw_message from previous turn
          - {role: "tool", tool_call_id: "...", content: "..."}
        Anthropic needs:
          - system as a separate parameter
          - tool results wrapped in role:"user" with type:"tool_result" blocks
          - assistant messages with content blocks (already in raw_message)
        """
        system = ""
        converted: list[dict] = []

        i = 0
        while i < len(messages):
            msg = messages[i]
            role = msg.get("role", "")

            if role == "system":
                system = msg.get("content", "")
                i += 1
                continue

            if role == "user":
                converted.append({"role": "user", "content": msg.get("content", "")})
                i += 1
                continue

            if role == "assistant":
                content = msg.get("content")
                # raw_message from a previous Anthropic turn — content is a list
                if isinstance(content, list):
                    converted.append({"role": "assistant", "content": content})
                # OpenAI-style assistant with tool_calls (from Gemini/OpenAI fallback)
                elif msg.get("tool_calls"):
                    blocks: list[dict] = []
                    if content:
                        blocks.append({"type": "text", "text": content})
                    for tc in msg["tool_calls"]:
                        fn = tc.get("function", {})
                        args = fn.get("arguments", "{}")
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                args = {}
                        blocks.append({
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": fn.get("name", ""),
                            "input": args,
                        })
                    converted.append({"role": "assistant", "content": blocks})
                else:
                    converted.append({"role": "assistant", "content": content or ""})
                i += 1
                continue

            if role == "tool":
                # Collect consecutive tool results into one user message
                tool_results: list[dict] = []
                while i < len(messages) and messages[i].get("role") == "tool":
                    t = messages[i]
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": t.get("tool_call_id", ""),
                        "content": t.get("content", ""),
                    })
                    i += 1
                converted.append({"role": "user", "content": tool_results})
                continue

            # Unknown role — pass through as user
            converted.append({"role": "user", "content": str(msg.get("content", ""))})
            i += 1

        return system, converted

    # -- core API calls -------------------------------------------------------

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> ChatResult | None:
        try:
            system, anthropic_msgs = self._convert_messages(messages)

            body: dict = {
                "model": self.config.model,
                "max_tokens": 4096,
                "messages": anthropic_msgs,
            }
            if system:
                body["system"] = system
            if tools:
                body["tools"] = self._to_anthropic_tools(tools)

            logger.info("anthropic request: model=%s msgs=%d tools=%d",
                        self.config.model, len(anthropic_msgs), len(tools or []))

            async with httpx.AsyncClient(timeout=self.config.timeout) as client:
                resp = await _request_with_retry(
                    client, "POST",
                    self._messages_url(),
                    headers=self._auth_headers(),
                    json=body,
                    provider_name="anthropic",
                )
                data = resp.json()

            stop_reason = data.get("stop_reason", "")
            usage = data.get("usage", {})
            logger.info("anthropic response: stop=%s input_tokens=%s output_tokens=%s",
                        stop_reason, usage.get("input_tokens"), usage.get("output_tokens"))

            # Parse response content blocks
            content_blocks = data.get("content", [])
            text_parts: list[str] = []
            parsed_tool_calls: list[ToolCall] = []

            for block in content_blocks:
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    parsed_tool_calls.append(ToolCall(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        params=block.get("input", {}),
                    ))

            text = "\n".join(text_parts) if text_parts else None

            if parsed_tool_calls:
                logger.info("anthropic tool_calls: %s",
                            [(tc.name, tc.params) for tc in parsed_tool_calls])

            # raw_message: store content blocks so they can be re-fed as-is
            raw_message = {"role": "assistant", "content": content_blocks} if parsed_tool_calls else None

            return ChatResult(
                content=text,
                tool_calls=parsed_tool_calls,
                raw_message=raw_message,
            )
        except Exception as exc:
            logger.warning("Anthropic chat failed: %s", exc)
            self._healthy = False
            self._healthy_at = time.time()
            return None

    async def chat_stream(self, messages: list[dict]) -> AsyncIterator[str]:
        yield ""

    async def _check_health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    self._messages_url(),
                    headers=self._auth_headers(),
                    json={
                        "model": self.config.model,
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "hi"}],
                    },
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


def load_providers(config_path: str, config_override: dict | None = None) -> None:
    """Load providers from config.json, or from config_override if provided."""
    global _default_provider
    if config_override:
        data = config_override
    elif os.path.exists(config_path):
        with open(config_path) as f:
            data = json.load(f)
    else:
        logger.warning("Provider config not found: %s", config_path)
        return

    agent_defaults = data.get("agents", {}).get("defaults", {})
    default_model = agent_defaults.get("model", "")
    _default_provider = agent_defaults.get("provider")

    for name, pconf in data.get("providers", {}).items():
        base_url = pconf.get("baseUrl", "")
        api_key = _resolve_env(pconf.get("apiKey", ""))
        model = pconf.get("model", default_model)
        phi_safe = pconf.get("phi_safe", False)
        timeout = pconf.get("timeout", 60.0)
        supports_vision = pconf.get("supports_vision", False)

        config = ProviderConfig(
            name=name,
            base_url=base_url,
            api_key=api_key,
            model=model,
            phi_safe=phi_safe,
            timeout=timeout,
            supports_vision=supports_vision,
        )
        # Route to correct provider class
        # Explicit "type" field takes precedence; otherwise infer from name/URL
        ptype = pconf.get("type", "").lower()
        if ptype == "ollama" or (not ptype and ("ollama" in name.lower() or "/api/" in base_url)):
            _providers[name] = OllamaProvider(config)
        elif ptype == "gemini" or (not ptype and ("gemini" in name.lower() or "generativelanguage.googleapis.com" in base_url)):
            _providers[name] = GeminiProvider(config)
        elif ptype == "anthropic" or (not ptype and ("anthropic" in name.lower() or "api.anthropic.com" in base_url)):
            _providers[name] = AnthropicProvider(config)
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


async def get_healthy_provider() -> LLMProvider | None:
    """Return the first healthy provider, trying default first then others."""
    # Try default first
    target = _default_provider
    if target and target in _providers:
        p = _providers[target]
        if await p.is_available():
            return p
        logger.warning("Default provider '%s' unhealthy, trying others", target)
    # Try remaining providers in order
    for name, p in _providers.items():
        if name == target:
            continue
        if await p.is_available():
            logger.info("Using fallback provider '%s'", name)
            return p
    logger.warning("No healthy providers available")
    return None
