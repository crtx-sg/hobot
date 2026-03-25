"""Interpret vitals skill — auto-fires after get_vitals / get_vitals_history.

Supports configurable thresholds at three levels (merged in priority order):
1. Hospital defaults — config.json: skills.interpret_vitals.thresholds
2. Patient-specific — clinical memory: vitals_thresholds fact type
3. Per-request — input.params["thresholds"]

Scoring system (NEWS2 or MEWS) is configurable via config or params.
"""

import asyncio
import json
import logging

import clinical_memory
from analyzer_prompts import ANALYZER_PROMPTS
from domain_models.vitals_anomaly import DEFAULT_THRESHOLDS, format_thresholds_for_prompt
from skills import BaseSkill, SkillInput, SkillOutput
from skills.clinical_context import build_patient_context

logger = logging.getLogger("clinibot.skills.interpret_vitals")


def _merge_thresholds(*layers: dict) -> dict:
    """Merge threshold dicts (later layers override earlier ones per-param)."""
    merged = {}
    for layer in layers:
        for param, limits in layer.items():
            if param in merged:
                merged[param] = {**merged[param], **limits}
            else:
                merged[param] = dict(limits)
    return merged


class InterpretVitalsSkill(BaseSkill):
    """Interprets vitals using clinical reasoning model + vitals anomaly model.

    Threshold resolution order:
        defaults → hospital config → patient-specific (clinical memory) → request params
    """

    name = "interpret_vitals"
    domain = "critical_care"
    required_context = ["demographics"]
    interprets_tools = ["get_vitals", "get_vitals_history", "get_vitals_trend"]

    async def _get_patient_thresholds(self, patient_id: str, tenant_id: str) -> dict:
        """Fetch patient-specific thresholds from clinical memory, fallback to backend.

        Flow: clinical_memory(vitals_thresholds) → monitoring backend → empty dict
        Fetched thresholds are stored in clinical_memory for future use.
        """
        # Check memory first
        try:
            facts = await clinical_memory.get_facts(
                patient_id, tenant_id, fact_type="vitals_thresholds")
            if facts:
                data = facts[0].get("fact_data", {})
                if isinstance(data, str):
                    data = json.loads(data)
                return data if isinstance(data, dict) else {}
        except Exception:
            pass

        # Fallback: fetch from monitoring backend
        try:
            from skills.clinical_context import fetch_from_backend

            class _Stub:
                id = "threshold-fetch"

            result = await fetch_from_backend(patient_id, "vitals_thresholds", _Stub())
            if result and "thresholds" in result:
                thresholds = result["thresholds"]
                # Store in clinical memory for future use
                await clinical_memory.store_fact(
                    session_id="system", tenant_id=tenant_id,
                    patient_id=patient_id, fact_type="vitals_thresholds",
                    fact_data=thresholds, source_tool="get_patient_thresholds",
                )
                logger.info("Fetched and cached thresholds for %s (source: %s)",
                            patient_id, result.get("source", "unknown"))
                return thresholds
        except Exception as exc:
            logger.debug("Could not fetch thresholds for %s: %s", patient_id, exc)

        return {}

    async def run(self, input: SkillInput) -> SkillOutput:
        patient_id = input.patient_id
        if not patient_id:
            return SkillOutput(
                status="error", domain=self.domain,
                interpretation="Cannot interpret vitals without patient_id",
            )

        # Check required context
        context_summary, context_found = await build_patient_context(
            patient_id, input.tenant_id,
            ["demographics", "medication", "allergy"],
        )

        if "demographics" not in context_found:
            logger.info("interpret_vitals skipped: missing demographics for %s", patient_id)
            return SkillOutput(
                status="skipped", domain=self.domain,
                interpretation="",
                status_reason="missing_required_context",
            )

        # ── Resolve thresholds ───────────────────────────────────────────
        # Layer 1: built-in defaults
        # Layer 2: hospital config (skills.interpret_vitals.thresholds)
        hospital_thresholds = self.config.get("thresholds", {})
        # Layer 3: patient-specific (clinical memory → backend fallback)
        patient_thresholds = await self._get_patient_thresholds(
            patient_id, input.tenant_id)
        # Layer 4: per-request override
        request_thresholds = input.params.get("thresholds", {})

        thresholds = _merge_thresholds(
            DEFAULT_THRESHOLDS, hospital_thresholds,
            patient_thresholds, request_thresholds,
        )

        # Resolve scoring system
        scoring = input.params.get("scoring", self.config.get("scoring", "news2"))

        # ── Run vitals anomaly model ─────────────────────────────────────
        vitals_model = self.domain_models.get("vitals_anomaly")
        score = None
        if vitals_model and await vitals_model.is_available():
            model_result = await vitals_model.predict({
                "vitals": input.tool_result,
                "history": input.context.get("history", []),
                "thresholds": thresholds,
                "scoring": scoring,
            })
            score = model_result.score

        # ── Run clinical reasoning model ─────────────────────────────────
        clinical_model = self.domain_models.get("clinical_reasoning")
        if not clinical_model or not await clinical_model.is_available():
            interpretation = ""
            if score:
                label = scoring.upper()
                interpretation = f"{label} score: {score.get(scoring, 'N/A')} (risk: {score.get('risk', 'unknown')})"
                violations = score.get("violations", [])
                if violations:
                    parts = []
                    for v in violations:
                        name = v["param"].replace("_", " ")
                        parts.append(f"{v['level']}: {name}={v['value']}")
                    interpretation += f". Threshold alerts: {'; '.join(parts)}"
            return SkillOutput(
                status="partial" if interpretation else "error",
                domain=self.domain,
                interpretation=interpretation,
                score=score,
                context_used=context_found,
                context_summary=context_summary,
                status_reason="provider_unavailable",
            )

        # Format vitals as inline text
        vitals = input.tool_result
        data_parts = [f"{k.replace('_', ' ')} {v}" for k, v in vitals.items()
                       if not k.startswith("_")]
        data_str = ", ".join(data_parts) if data_parts else json.dumps(vitals, indent=2)

        # Format context as plain text
        try:
            ctx = json.loads(context_summary) if context_summary.startswith("{") else {}
            ctx_parts = []
            if ctx.get("age"): ctx_parts.append(f"{ctx['age']}yo")
            if ctx.get("gender"): ctx_parts.append(ctx["gender"])
            if ctx.get("active_medications"):
                ctx_parts.append(f"on {', '.join(ctx['active_medications'])}")
            if ctx.get("allergies"):
                ctx_parts.append(f"allergies: {', '.join(ctx['allergies'])}")
            context_text = ", ".join(ctx_parts) if ctx_parts else context_summary
        except (json.JSONDecodeError, TypeError):
            context_text = context_summary

        # Build threshold context for prompt
        threshold_text = format_thresholds_for_prompt(thresholds)

        template = ANALYZER_PROMPTS.get("vitals_analysis", "")
        prompt = template.format(
            data=data_str,
            context=context_text,
            thresholds=threshold_text,
            scoring=scoring.upper(),
        )

        result = await clinical_model.predict({"prompt": prompt})

        return SkillOutput(
            status="success",
            domain=self.domain,
            interpretation=result.content,
            score=score,
            context_used=context_found,
            context_summary=context_summary,
            provider_name=result.model_name,
            model_name=result.model_version,
        )
