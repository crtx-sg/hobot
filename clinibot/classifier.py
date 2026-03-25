"""Intent pre-classifier — maps user messages to tool domain groups.

3-tier strategy:
  Tier 1: Regex rules (<1ms, handles ~70% of clinical messages)
  Tier 2: LLM (gemini-flash, ~200-500ms for ambiguous messages)
  Tier 3: Fallback (config-defined broad domain set on any error)
"""

import json
import logging
import os
import re

import httpx

logger = logging.getLogger("clinibot.classifier")

# ---------------------------------------------------------------------------
# Config (loaded from config.json)
# ---------------------------------------------------------------------------

_config: dict = {}
_domain_names: list[str] = []


def _build_regex_rules(domain_keywords: dict) -> list[tuple[re.Pattern, list[str]]]:
    """Build regex rules from config domain_keywords mapping."""
    rules = []
    for domain, keywords in domain_keywords.items():
        pattern = r"\b(" + "|".join(keywords) + r")"
        rules.append((re.compile(pattern, re.I), [domain]))
    return rules


def load_classifier_config(config_path: str) -> None:
    global _config, _domain_names, _REGEX_RULES
    if not os.path.exists(config_path):
        return
    with open(config_path) as f:
        data = json.load(f)
    _config = data.get("classifier", {})
    _domain_names = list(data.get("tool_domains", {}).keys())
    # Auto-generate regex rules from config keywords if present
    domain_keywords = _config.get("domain_keywords")
    if domain_keywords:
        _REGEX_RULES = _build_regex_rules(domain_keywords)
        logger.info("Built %d regex rules from config domain_keywords", len(_REGEX_RULES))
    logger.info("Classifier config loaded: provider=%s, domains=%s",
                _config.get("provider"), _domain_names)


# ---------------------------------------------------------------------------
# Tier 1: Regex rules
# ---------------------------------------------------------------------------

_REGEX_RULES: list[tuple[re.Pattern, list[str]]] = [
    (re.compile(r"\b(vitals?|bp|blood\s*pressure|heart\s*rate|spo2|temp(?:erature)?|news2?|pulse)\b", re.I), ["vitals"]),
    (re.compile(r"\b(labs?|cbc|bmp|blood\s*test|culture|hba1c|wbc|hemoglobin|creatinine)\b", re.I), ["labs"]),
    (re.compile(r"\b(ecg|ekg|rhythm|12.lead)\b", re.I), ["ecg"]),
    (re.compile(r"\b(x.?ray|ct\b|mri|scan|radiol|chest|imaging|stud(?:y|ies))", re.I), ["radiology"]),
    (re.compile(r"\b(medicat|drug|prescri|dispens|allerg|interaction)", re.I), ["medications"]),
    (re.compile(r"\b(order(?!ly)|prescribe)\b", re.I), ["orders"]),
    (re.compile(r"\b(code\s*blue|emergency|ambulance|crash|arrest|escalat)", re.I), ["emergency"]),
    (re.compile(r"\b(housekeep\w*|maintenance|porter|clean(?:ing)?)\b", re.I), ["services"]),
    (re.compile(r"\b(appoint|remind|schedul)", re.I), ["scheduling"]),
    (re.compile(r"\b(inventory|suppl(?:y|ies)|stock|equipment)\b", re.I), ["supplies"]),
    (re.compile(r"\b(ward|doctor|rounds?|patient\s*list)\b", re.I), ["ward"]),
    (re.compile(r"\b(diet|meal|breakfast|lunch|dinner)\b", re.I), ["orders"]),
    (re.compile(r"\b(blood\s*bank|crossmatch|transfus)", re.I), ["orders"]),
]


def _regex_classify(message: str) -> list[str] | None:
    """Return domain tags matched by regex, or None if no matches."""
    matched: set[str] = set()
    for pattern, domains in _REGEX_RULES:
        if pattern.search(message):
            matched.update(domains)
    if matched and len(matched) <= 3:
        return list(matched)
    if matched:
        # Too many matches — let LLM disambiguate
        return None
    return None


# ---------------------------------------------------------------------------
# Tier 2: LLM classification (gemini-flash)
# ---------------------------------------------------------------------------

_CLASSIFY_PROMPT = """Classify this hospital staff message into 1-3 domain categories.

Available domains: {domains}

Message: "{message}"

Reply with ONLY a JSON array of 1-3 domain strings, e.g. ["vitals", "labs"]. No explanation."""


async def _llm_classify(message: str) -> list[str] | None:
    """Use gemini-flash to classify ambiguous messages."""
    import asyncio

    provider_name = _config.get("provider", "gemini")
    timeout = _config.get("timeout", 2)

    from providers import get_provider
    provider = get_provider(provider_name)
    if not provider or not await provider.is_available():
        return None

    prompt = _CLASSIFY_PROMPT.format(
        domains=", ".join(_domain_names),
        message=message[:200],
    )

    try:
        result = await asyncio.wait_for(
            provider.chat([{"role": "user", "content": prompt}]),
            timeout=timeout,
        )
        if not result or not result.content:
            return None

        # Parse JSON array from response
        content = result.content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        domains = json.loads(content)
        if isinstance(domains, list) and all(isinstance(d, str) for d in domains):
            valid = [d for d in domains if d in _domain_names]
            return valid[:3] if valid else None
        return None
    except asyncio.TimeoutError:
        logger.warning("LLM classify timed out after %ds", timeout)
        return None
    except Exception as exc:
        logger.warning("LLM classify failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def classify(user_message: str, session=None) -> list[str]:
    """Return 1-3 domain tags for the user message. Always includes 'core'."""
    # Tier 1: regex
    domains = _regex_classify(user_message)
    if domains:
        logger.debug("Classifier tier=regex domains=%s", domains)
        return domains

    # Tier 2: LLM
    if _config:
        domains = await _llm_classify(user_message)
        if domains:
            logger.debug("Classifier tier=llm domains=%s", domains)
            return domains

    # Tier 3: fallback
    fallback = _config.get("fallback_domains", ["core", "vitals", "labs", "medications"])
    logger.debug("Classifier tier=fallback domains=%s", fallback)
    return fallback
