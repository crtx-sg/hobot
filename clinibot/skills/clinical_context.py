"""Clinical context skill — builds patient context from memory + backend fallback.

Extracted from analyzers._build_patient_context.
"""

import json
import logging

import clinical_memory
from skills import BaseSkill, SkillInput, SkillOutput

logger = logging.getLogger("clinibot.skills.clinical_context")

# Backend tools to fetch missing context
_BACKEND_FETCH_MAPPING: dict[str, str] = {
    "demographics": "get_patient",
    "medication": "get_medications",
    "allergy": "get_allergies",
    "imaging_study": "get_latest_study",
    "vitals_thresholds": "get_patient_thresholds",
}


async def fetch_from_backend(patient_id: str, fact_type: str, session) -> dict | None:
    """Direct backend fetch when clinical_memory has no facts.

    Uses tools._dispatch() directly to bypass validation, critical gating,
    and analyzer intercepts — avoiding recursion.
    """
    import tools  # lazy import to avoid circular
    tool_name = _BACKEND_FETCH_MAPPING.get(fact_type)
    if not tool_name or tool_name not in tools.TOOL_BACKENDS:
        return None
    try:
        result = await tools._dispatch(tool_name, {"patient_id": patient_id}, session)
        return result if "error" not in result else None
    except Exception as exc:
        logger.warning("Backend fetch %s for %s failed: %s", tool_name, patient_id, exc)
        return None


async def build_patient_context(
    patient_id: str,
    tenant_id: str,
    context_types: list[str],
    session=None,
) -> tuple[str, list[str]]:
    """Fetch facts from clinical_memory filtered by context_types.

    Falls back to backend fetch if memory is empty and session is provided.
    Returns (summary, fact_types_found).
    """
    if not context_types:
        return "", []

    found_types: set[str] = set()
    context_parts: list[str] = []

    for ct in context_types:
        facts = await clinical_memory.get_facts(patient_id, tenant_id, fact_type=ct, limit=5)
        if facts:
            found_types.add(ct)
            for f in facts:
                context_parts.append(json.dumps(f["fact_data"]))
        elif session:
            backend_data = await fetch_from_backend(patient_id, ct, session)
            if backend_data:
                found_types.add(ct)
                context_parts.append(json.dumps(backend_data))

    summary = "; ".join(context_parts) if context_parts else ""
    if len(summary) > 1000:
        summary = summary[:1000] + "..."
    return summary, list(found_types)


async def get_context_for_skill(
    patient_id: str,
    tenant_id: str,
    context_types: list[str],
    session=None,
) -> dict[str, list[dict]]:
    """Return context as a dict of fact_type -> list of fact_data dicts.

    Used by skills to check required_context availability.
    """
    result: dict[str, list[dict]] = {}
    for ct in context_types:
        facts = await clinical_memory.get_facts(patient_id, tenant_id, fact_type=ct, limit=5)
        if facts:
            result[ct] = [f["fact_data"] for f in facts]
        elif session:
            backend_data = await fetch_from_backend(patient_id, ct, session)
            if backend_data:
                result[ct] = [backend_data]
    return result


class ClinicalContextSkill(BaseSkill):
    """Skill that builds patient clinical context."""

    name = "clinical_context"
    domain = "core"
    interprets_tools = []  # Not auto-invoked, called by other skills

    async def run(self, input: SkillInput) -> SkillOutput:
        context_types = input.context.get("requested_types", [])
        summary, found = await build_patient_context(
            input.patient_id, input.tenant_id, context_types,
        )
        return SkillOutput(
            status="success" if found else "partial",
            domain=self.domain,
            interpretation=summary,
            context_used=found,
            context_summary=summary,
        )
