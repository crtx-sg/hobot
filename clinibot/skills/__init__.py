"""Skills framework — reusable domain capabilities exposed as tool interpreters.

Skills sit between raw tool results and the orchestrator, providing
domain-specific interpretation, validation, and scoring.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("clinibot.skills")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SkillInput:
    """Input to a skill's run() method."""
    tool_name: str
    tool_result: dict
    params: dict
    patient_id: str
    session_id: str
    tenant_id: str
    context: dict = field(default_factory=dict)  # clinical context facts


@dataclass
class SkillOutput:
    """Structured output from a skill."""
    status: str  # "success", "partial", "error"
    domain: str
    interpretation: str
    findings: list[str] = field(default_factory=list)
    score: dict | None = None  # e.g. NEWS2 score
    context_used: list[str] = field(default_factory=list)
    context_summary: str = ""
    provider_name: str = ""
    model_name: str = ""
    status_reason: str | None = None
    metadata: dict = field(default_factory=dict)

    def to_analysis_dict(self) -> dict:
        """Convert to the _analysis dict format for backward compatibility."""
        result = {
            "status": self.status,
            "domain": self.domain,
            "interpretation": self.interpretation,
            "findings": self.findings,
            "context_used": self.context_used,
            "context_summary": self.context_summary,
            "analyzer_provider": self.provider_name,
            "analyzer_model": self.model_name,
        }
        if self.status_reason:
            result["status_reason"] = self.status_reason
        if self.score:
            result["score"] = self.score
        return result


# ---------------------------------------------------------------------------
# Base skill
# ---------------------------------------------------------------------------

class BaseSkill(ABC):
    """Abstract base for all skills."""

    name: str = ""
    domain: str = ""
    required_context: list[str] = []
    interprets_tools: list[str] = []  # tool names this skill auto-interprets

    def __init__(self, config: dict | None = None, domain_models: dict | None = None):
        self.config = config or {}
        self.domain_models = domain_models or {}
        self.timeout = self.config.get("timeout", 15)
        self.enabled = self.config.get("enabled", True)
        self.provider_name = self.config.get("provider", "")

    @abstractmethod
    async def run(self, input: SkillInput) -> SkillOutput:
        """Execute the skill. Must be implemented by subclasses."""

    async def has_required_context(self, input: SkillInput) -> bool:
        """Check if required clinical context is available."""
        if not self.required_context:
            return True
        for ct in self.required_context:
            if ct not in input.context or not input.context[ct]:
                return False
        return True


# ---------------------------------------------------------------------------
# Skill registry
# ---------------------------------------------------------------------------

class SkillRegistry:
    """Central registry mapping tool names to skill interpreters."""

    def __init__(self):
        self._skills: dict[str, BaseSkill] = {}  # skill_name -> skill
        self._tool_map: dict[str, BaseSkill] = {}  # tool_name -> skill

    def register(self, skill: BaseSkill) -> None:
        """Register a skill and its tool mappings."""
        self._skills[skill.name] = skill
        for tool_name in skill.interprets_tools:
            self._tool_map[tool_name] = skill
        logger.info("Registered skill '%s' (tools: %s)", skill.name, skill.interprets_tools)

    def get_interpreter(self, tool_name: str) -> BaseSkill | None:
        """Get the skill that interprets a given tool's results."""
        skill = self._tool_map.get(tool_name)
        if skill and skill.enabled:
            return skill
        return None

    def get_skill(self, name: str) -> BaseSkill | None:
        """Get a skill by name."""
        return self._skills.get(name)

    def all_skills(self) -> list[BaseSkill]:
        """Return all registered skills."""
        return list(self._skills.values())

    def tool_definitions(self) -> list[dict]:
        """Return tool definitions for skills that expose callable tools.

        Skills that only interpret existing tool results don't need
        their own tool definitions — they're auto-invoked by the orchestrator.
        Only skills with explicit tool schemas (like analyze_radiology_image) appear here.
        """
        defs = []
        for skill in self._skills.values():
            if not skill.enabled:
                continue
            tool_def = getattr(skill, "tool_definition", None)
            if callable(tool_def):
                d = tool_def()
                if d:
                    defs.append(d)
        return defs
