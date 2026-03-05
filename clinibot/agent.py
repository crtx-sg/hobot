"""Agent loop — intent detection, tool dispatch, and response synthesis."""

import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import audit
import clinical_memory
import phi
from providers import ChatResult, OllamaProvider, get_healthy_provider, get_provider
from session import Session
from tools import (
    _TOOL_DESCRIPTIONS,
    build_tool_definitions,
    call_tool,
    call_tools_parallel,
    get_tool_list,
)


@dataclass
class AgentResult:
    """Structured result from the agent loop."""
    text: str
    tool_results: list[dict] = field(default_factory=list)

logger = logging.getLogger("clinibot.agent")

MAX_ITERATIONS = 10
CONSOLIDATION_THRESHOLD = int(os.environ.get("CONSOLIDATION_THRESHOLD", "30"))

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are Hobot, a clinical AI assistant for hospital staff.
Be crisp and sharp. Prefer tables/bullets over paragraphs.
When you need data, call a tool. Never fabricate clinical data.
Always cite which tool provided the data.
When user refers to a bed (e.g. "bed 3"), use resolve_bed first to get patient_id, then call the relevant tool.
For critical actions (marked critical), the system will require human confirmation before execution.

Available tools:
{tools}

To call a tool, respond with ONLY a JSON block like this (no other text):
```json
{{"tool": "tool_name", "params": {{"param1": "value1"}}}}
```

After receiving the tool result, synthesize a concise clinical response.
Do NOT fabricate tool results. You MUST call the tool and wait for real data.
Respond concisely and professionally. Use structured formatting when presenting clinical data."""


def _supports_native_tools(provider) -> bool:
    """Return True if the provider supports native function calling."""
    return not isinstance(provider, OllamaProvider)


def _build_system_prompt() -> str:
    tools = get_tool_list()
    tool_desc = "\n".join(
        f"- {t['name']}: {_TOOL_DESCRIPTIONS.get(t['name'], '')}" + (" [CRITICAL]" if t["critical"] else "")
        for t in tools
    )
    return SYSTEM_PROMPT.format(tools=tool_desc)


# ---------------------------------------------------------------------------
# Keyword-based fallback intent detection
# ---------------------------------------------------------------------------

_INTENT_PATTERNS: list[tuple[re.Pattern, str, callable]] = [
    # --- Rounds, bed, scheduling, reminders ---
    (re.compile(r"rounds?\s+(?:report\s+)?(?:for\s+)?(?:ward\s+)?(\S+)", re.I),
     "get_ward_rounds", lambda m: {"ward_id": m.group(1)}),
    (re.compile(r"(?:meds?|medications?)\s+(?:for\s+)?(?:patient\s+(?:in\s+)?)?bed\s*(\d+)", re.I),
     "get_medications", lambda m: {"bed_id": f"BED{m.group(1)}"}),
    (re.compile(r"(?:vitals?|show\s+vitals?)\s+(?:for\s+)?(?:patient\s+(?:in\s+)?)?bed\s*(\d+)", re.I),
     "get_vitals", lambda m: {"bed_id": f"BED{m.group(1)}"}),
    (re.compile(r"schedule\s+.*?appointment\s+.*?(?:dr\.?\s*)(\w+)\s+.*?bed\s*(\d+)", re.I),
     "schedule_appointment", lambda m: {"doctor": f"DR-{m.group(1).upper()}", "bed_id": f"BED{m.group(2)}", "datetime": "TBD"}),
    (re.compile(r"remind(?:er)?\s+.*?(\d+)\s+hours?\s+.*?(?:bed\s*(\d+))?", re.I),
     "set_reminder", lambda m: {
         "delay_minutes": int(m.group(1)) * 60,
         "message": m.group(0).strip(),
         **({"bed_id": f"BED{m.group(2)}"} if m.group(2) else {}),
     }),
    # --- Vitals patterns ---
    (re.compile(r"vitals?\s+raw\s+(?:data\s+)?(?:(?:over\s+)?(?:the\s+)?last\s+(\d+)\s+hours?\s+)?(?:for\s+)?(\w+)", re.I),
     "get_vitals_trend",
     lambda m: {"patient_id": m.group(2), "display": "raw", **({"hours": int(m.group(1))} if m.group(1) else {})}),
    (re.compile(r"vitals?\s+trends?\s+(?:(?:over\s+)?(?:the\s+)?last\s+(\d+)\s+hours?\s+)?(?:for\s+)?(\w+)", re.I),
     "get_vitals_trend",
     lambda m: {"patient_id": m.group(2), "display": "chart", **({"hours": int(m.group(1))} if m.group(1) else {})}),
    (re.compile(r"vitals?\s+(?:for\s+)?(\w+)", re.I), "get_vitals", lambda m: {"patient_id": m.group(1)}),
    (re.compile(r"vitals?\s+history\s+(?:for\s+)?(\w+)", re.I), "get_vitals_history", lambda m: {"patient_id": m.group(1)}),
    (re.compile(r"medications?\s+(?:for\s+)?(\w+)", re.I), "get_medications", lambda m: {"patient_id": m.group(1)}),
    (re.compile(r"allergies\s+(?:for\s+)?(\w+)", re.I), "get_allergies", lambda m: {"patient_id": m.group(1)}),
    (re.compile(r"lab\s+results?\s+(?:for\s+)?(\w+)", re.I), "get_lab_results", lambda m: {"patient_id": m.group(1)}),
    # ECG patterns — must come before the generic "patient" pattern
    (re.compile(r"ecg\s+(?:event|for\s+event)\s+(\S+)\s+(?:of|for)\s+(?:patient\s+)?(\w+)", re.I),
     "get_event_ecg", lambda m: {"event_id": m.group(1), "patient_id": m.group(2)}),
    (re.compile(r"(?:get|show|fetch)\s+(?:(?:last|latest)\s+)?ecg\s+(?:of|for)\s+(?:patient\s+)?(\w+)", re.I),
     "get_latest_ecg", lambda m: {"patient_id": m.group(1)}),
    (re.compile(r"(?:(?:last|latest)\s+)?ecg\s+(?:for\s+)?(?:patient\s+)?(\w[-\w]*)", re.I), "get_latest_ecg", lambda m: {"patient_id": m.group(1)}),
    (re.compile(r"patient\s+(?:info|details?|record)?\s*(?:for\s+)?(\w+)", re.I), "get_patient", lambda m: {"patient_id": m.group(1)}),
    (re.compile(r"(?:list|show)\s+wards?", re.I), "list_wards", lambda m: {}),
    (re.compile(r"(?:list|show)\s+doctors?", re.I), "list_doctors", lambda m: {}),
    (re.compile(r"ward\s+patients?\s+(?:for\s+)?(\w+)", re.I), "get_ward_patients", lambda m: {"ward_id": m.group(1)}),
    (re.compile(r"doctor\s+patients?\s+(?:for\s+)?(\w+)", re.I), "get_doctor_patients", lambda m: {"doctor_id": m.group(1)}),
    (re.compile(r"blood\s+availability", re.I), "get_blood_availability", lambda m: {}),
    (re.compile(r"inventory", re.I), "get_inventory", lambda m: {}),
    (re.compile(r"studies\s+(?:for\s+)?(\w+)", re.I), "get_studies", lambda m: {"patient_id": m.group(1)}),
    (re.compile(r"code\s+blue\s+(?:for\s+)?(\w+)", re.I), "initiate_code_blue", lambda m: {"patient_id": m.group(1)}),
    (re.compile(r"escalate\s+(\w+)\s+(?:to\s+)?(.+)", re.I), "escalate", lambda m: {"patient_id": m.group(1), "escalate_to": m.group(2).strip(), "reason": "User-requested escalation"}),
]


def _detect_intent(message: str) -> tuple[str, dict] | None:
    for pattern, tool_name, extract in _INTENT_PATTERNS:
        match = pattern.search(message)
        if match:
            params = extract(match)
            # Normalize IDs to uppercase (backend is case-sensitive)
            for key in ("patient_id", "ward_id", "doctor_id"):
                if key in params:
                    params[key] = params[key].upper()
            return tool_name, params
    return None


_TOOL_SYNTHESIS_PROMPT = (
    "You are a clinical assistant for nurses and clinicians. "
    "Summarize the following tool result concisely. "
    "Use bullet points. No JSON. No jargon unless clinically standard. "
    "Flag abnormal values. Keep it under 10 lines.\n\n"
    "Tool: {tool_name}\n"
    "Result:\n{result_json}"
)


def _format_tool_result_text(tool_name: str, data: dict) -> str:
    """Simple human-readable summary when LLM is not available."""
    if "error" in data:
        return f"Error from {tool_name}: {data['error']}"

    lines: list[str] = []
    pid = data.get("patient_id", "")

    if tool_name == "get_lab_results":
        lines.append(f"Lab Results — {pid}")
        for lab in data.get("labs", []):
            lines.append(f"\n{lab.get('test_type', 'Lab')} ({lab.get('status', '')}):")
            for test, info in lab.get("results", {}).items():
                val = info.get("value", "")
                unit = info.get("unit", "")
                ref = info.get("ref_range", "")
                lines.append(f"  {test}: {val} {unit} (ref {ref})")

    elif tool_name == "get_medications":
        lines.append(f"Medications — {pid}")
        for m in data.get("medications", []):
            med = m.get("medication", "")
            dose = m.get("dose", "")
            route = m.get("route", "")
            freq = m.get("frequency", "")
            lines.append(f"  • {med} {dose} {route} {freq}")

    elif tool_name == "get_patient":
        lines.append(f"Patient: {data.get('name', pid)}")
        lines.append(f"  ID: {data.get('patient_id', '')}")
        lines.append(f"  Gender: {data.get('gender', '')}")
        lines.append(f"  DOB: {data.get('birth_date', '')}")

    elif tool_name == "get_allergies":
        lines.append(f"Allergies — {pid}")
        for a in data.get("allergies", []):
            substance = a.get("substance", "")
            criticality = a.get("criticality", "")
            lines.append(f"  • {substance} ({criticality})")

    elif tool_name == "get_vitals":
        lines.append(f"Vitals — {pid}")
        for key in ("heart_rate", "bp_systolic", "bp_diastolic", "spo2", "temperature"):
            val = data.get(key)
            if val is not None:
                label = key.replace("_", " ").title()
                lines.append(f"  {label}: {val}")

    elif tool_name in ("get_latest_ecg", "get_event_ecg"):
        event_id = data.get("event_id", "")
        condition = data.get("condition", "")
        leads = data.get("leads", {})
        sr = data.get("sampling_rate_hz", "")
        dur = data.get("duration_s", "")
        lines.append(f"ECG — {pid}" + (f" (event {event_id})" if event_id else ""))
        if condition:
            lines.append(f"  Condition: {condition}")
        lines.append(f"  {len(leads)} leads, {sr} Hz, {dur}s")
        lines.append(f"  Leads: {', '.join(leads.keys())}")

    elif tool_name in ("get_ward_patients", "get_doctor_patients"):
        header = data.get("ward_id") or data.get("doctor_id", "")
        lines.append(f"Patients — {header}")
        for p in data.get("patients", []):
            pid_inner = p.get("patient_id", "")
            news = p.get("news_score", 0)
            lines.append(f"  • {pid_inner} NEWS={news}")

    elif tool_name == "get_blood_availability":
        lines.append("Blood Bank Inventory")
        for bt, info in data.items():
            if isinstance(info, dict):
                units = info.get("units", info.get("available", ""))
                lines.append(f"  • {bt}: {units} units")
            elif bt not in ("error",):
                lines.append(f"  • {bt}: {info}")

    elif tool_name in ("order_diet", "request_housekeeping", "request_ambulance"):
        req_id = data.get("id", "")
        status = data.get("status", "")
        req_type = data.get("type", tool_name)
        lines.append(f"{req_type} — {req_id} ({status})")
        for key in ("patient_id", "diet_type", "meal", "room", "request_type",
                     "from_location", "to_location", "priority"):
            val = data.get(key)
            if val:
                label = key.replace("_", " ").title()
                lines.append(f"  {label}: {val}")

    if lines:
        return "\n".join(lines)

    # Generic fallback: show top-level keys as key-value
    lines.append(f"{tool_name} result:")
    for k, v in data.items():
        if isinstance(v, (list, dict)):
            lines.append(f"  {k}: ({len(v)} items)" if isinstance(v, list) else f"  {k}: ...")
        else:
            lines.append(f"  {k}: {v}")
    return "\n".join(lines)


_RAW_DISPLAY_RE = re.compile(r"vitals?\s+raw\b", re.I)


def _detect_display_hint(message: str) -> str | None:
    """Detect client-side display hint from user message text.

    Returns "raw" if the message asks for raw vitals data, else None (caller defaults to "chart").
    """
    if _RAW_DISPLAY_RE.search(message):
        return "raw"
    return None


# ---------------------------------------------------------------------------
# Memory consolidation (F2)
# ---------------------------------------------------------------------------

CONSOLIDATION_PROMPT = """Summarize this clinical conversation history concisely.
Preserve: patient IDs, diagnoses, key vitals, medications, pending actions, and clinical decisions.
If there is an existing summary, integrate new information into it.

Existing summary: {existing_summary}

Messages to consolidate:
{messages}

Provide a concise clinical summary:"""


async def _llm_consolidate(messages: list[dict], existing_summary: str, provider) -> str | None:
    """Use LLM to produce a clinical-aware conversation summary."""
    msg_text = "\n".join(f"[{m['role']}] {m['content']}" for m in messages)
    prompt = CONSOLIDATION_PROMPT.format(
        existing_summary=existing_summary or "(none)",
        messages=msg_text,
    )
    result = await provider.chat([{"role": "user", "content": prompt}])
    return result.content if result else None


async def _maybe_consolidate(session: Session, provider=None) -> None:
    """Consolidate old messages if we've exceeded the threshold."""
    unconsolidated = len(session.messages) - session.last_consolidated
    if unconsolidated < CONSOLIDATION_THRESHOLD:
        return

    # Keep the last 10 messages fresh
    consolidate_end = len(session.messages) - 10
    if consolidate_end <= session.last_consolidated:
        return

    to_consolidate = session.messages[session.last_consolidated:consolidate_end]

    summary = session.summary
    if provider and await provider.is_available():
        llm_summary = await _llm_consolidate(to_consolidate, session.summary, provider)
        if llm_summary:
            summary = llm_summary

    session.save_consolidation(summary, consolidate_end)
    # Trim in-memory messages to post-pointer only
    session.messages = session.messages[consolidate_end:]
    session.last_consolidated = 0  # Reset pointer relative to trimmed list
    logger.info("Consolidated session %s (kept %d messages)", session.id, len(session.messages))


# ---------------------------------------------------------------------------
# PHI helpers (F6)
# ---------------------------------------------------------------------------

def _redact_messages(messages: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """Redact PHI from all message contents. Returns (redacted_messages, combined_mapping)."""
    combined_mapping: dict[str, str] = {}
    redacted = []
    for msg in messages:
        redacted_content, mapping = phi.redact(msg.get("content", ""))
        combined_mapping.update(mapping)
        redacted.append({**msg, "content": redacted_content})
    return redacted, combined_mapping


# ---------------------------------------------------------------------------
# Tool call parsing
# ---------------------------------------------------------------------------

def _parse_tool_call(content: str) -> tuple[str, dict] | None:
    """Extract a tool call from LLM output (JSON block)."""
    for pattern in [
        re.compile(r'```json\s*(\{.*?\})\s*```', re.S),
        re.compile(r'(\{"tool":\s*"[^"]+?".*?\})', re.S),
    ]:
        match = pattern.search(content)
        if match:
            try:
                data = json.loads(match.group(1))
                if "tool" in data:
                    return data["tool"], data.get("params", {})
            except json.JSONDecodeError:
                continue
    return None


# ---------------------------------------------------------------------------
# Post-tool hooks (shared by provider and keyword paths)
# ---------------------------------------------------------------------------

async def _post_tool_hooks(tool_name: str, tool_result: dict, params: dict, session: Session, provider_name: str | None) -> None:
    patient_id = params.get("patient_id", "")
    await clinical_memory.extract_and_store(
        tool_name, tool_result, patient_id, session.id, session.tenant_id
    )
    result_summary = json.dumps(tool_result)[:200]
    # Redact audit summaries
    result_summary, _ = phi.redact(result_summary)
    await audit.log_action(
        tenant_id=session.tenant_id,
        session_id=session.id,
        user_id=session.user_id,
        channel=session.channel,
        action="tool_call",
        tool_name=tool_name,
        params=params,
        result_summary=result_summary,
    )


# ---------------------------------------------------------------------------
# Agent loop (F4 — provider-routed)
# ---------------------------------------------------------------------------

async def run_agent(user_message: str, session: Session) -> AgentResult:
    """Process a user message through the agent loop. Returns AgentResult."""
    session.append_message("user", user_message)
    t0 = time.time()

    provider = await get_healthy_provider()
    logger.info("[%s] query=%r provider=%s", session.id, user_message[:80],
                provider.config.name if provider else "NONE")

    # Consolidation check (F2)
    if provider:
        await _maybe_consolidate(session, provider)

    if provider:
        result = await _run_with_provider(user_message, session, provider)
    else:
        logger.warning("[%s] no healthy provider — falling back to keyword regex", session.id)
        result = await _run_with_keywords(user_message, session, provider=None)

    latency_ms = int((time.time() - t0) * 1000)
    await audit.log_action(
        tenant_id=session.tenant_id,
        session_id=session.id,
        user_id=session.user_id,
        channel=session.channel,
        action="chat_response",
        latency_ms=latency_ms,
        provider=provider.config.name if provider else "keyword_fallback",
        model=provider.config.model if provider else None,
    )

    session.append_message("assistant", result.text)
    return result


async def _run_with_provider(user_message: str, session: Session, provider) -> AgentResult:
    """Agent loop using an LLM provider for intent + synthesis."""
    # Build clinical context from active patients
    clinical_context = ""
    for pid in session.active_patients:
        facts = await clinical_memory.get_facts(pid, session.tenant_id, limit=10)
        if facts:
            clinical_context += f"\nKnown facts for {pid}:\n"
            for f in facts:
                clinical_context += f"  - [{f['fact_type']}] {json.dumps(f['fact_data'])}\n"

    messages = [
        {"role": "system", "content": _build_system_prompt() + clinical_context},
    ]
    for msg in session.get_context(max_messages=10):
        messages.append({"role": msg["role"], "content": msg["content"]})

    # PHI redaction for non-phi-safe providers (F6)
    phi_mapping = None
    if not provider.config.phi_safe:
        messages, phi_mapping = _redact_messages(messages)

    # Native tool definitions for providers that support it
    use_native = _supports_native_tools(provider)
    tool_defs = build_tool_definitions() if use_native else None

    collected_tool_results: list[dict] = []

    for iteration in range(MAX_ITERATIONS):
        logger.info("[%s] llm iteration=%d provider=%s native_tools=%s",
                    session.id, iteration, provider.config.name, bool(tool_defs))
        result = await provider.chat(messages, tools=tool_defs)
        if result is None:
            # If we already have tool results, synthesize from them instead of
            # falling back to keywords (which would discard completed work).
            if collected_tool_results:
                logger.warning("[%s] provider returned None after %d tool results — synthesizing from collected data",
                               session.id, len(collected_tool_results))
                # Mark current provider unhealthy so fallback picks a different one
                provider._healthy = False
                provider._healthy_at = time.time()
                fallback = await get_healthy_provider()
                if fallback and fallback.config.name == provider.config.name:
                    fallback = None  # same broken provider, skip
                if fallback:
                    logger.info("[%s] using fallback provider '%s' for synthesis", session.id, fallback.config.name)
                texts = []
                for tr in collected_tool_results:
                    texts.append(await _synthesize_tool_text(tr["tool"], tr["data"], provider=fallback))
                return AgentResult(text="\n\n".join(texts), tool_results=collected_tool_results)
            logger.warning("[%s] provider returned None — keyword fallback", session.id)
            return await _run_with_keywords(user_message, session, provider=provider)

        # --- NATIVE TOOL PATH (parallel) ---
        if result.tool_calls:
            call_names = [tc.name for tc in result.tool_calls]
            logger.info("[%s] llm requested tools: %s", session.id, call_names)
            calls: list[tuple[str, dict]] = []
            for tc in result.tool_calls:
                params = dict(tc.params)
                # Restore PHI in tool params before dispatch
                if phi_mapping:
                    for k, v in params.items():
                        if isinstance(v, str):
                            params[k] = phi.restore(v, phi_mapping)
                # Track active patients
                if "patient_id" in params:
                    session.active_patients.add(params["patient_id"])
                # Inject display hint
                if tc.name == "get_vitals_trend" and "display" not in params:
                    hint = _detect_display_hint(user_message)
                    if hint:
                        params["display"] = hint
                calls.append((tc.name, params))

            tool_results = await call_tools_parallel(calls, session)

            # Post-hooks for each result
            for tr in tool_results:
                has_error = "error" in tr["data"]
                logger.info("[%s] tool_result: %s params=%s error=%s",
                            session.id, tr["tool"], tr["params"], has_error)
                await _post_tool_hooks(
                    tr["tool"], tr["data"], tr["params"], session, provider.config.name
                )
            collected_tool_results.extend(tool_results)

            # If any tool returned awaiting_confirmation, return immediately
            # so the confirmation block reaches the client (don't feed to LLM).
            confirmation_results = [
                tr for tr in tool_results
                if tr["data"].get("status") == "awaiting_confirmation"
            ]
            if confirmation_results:
                cr = confirmation_results[0]
                text = (
                    f"This is a critical action ({cr['tool']}) that requires confirmation.\n"
                    f"Confirmation ID: {cr['data']['confirmation_id']}\n"
                    f"{cr['data']['message']}"
                )
                logger.info("[%s] critical tool %s gated — returning confirmation", session.id, cr["tool"])
                return AgentResult(text=text, tool_results=collected_tool_results)

            # Append assistant message with tool_calls, then tool results
            messages.append(result.raw_message)
            for tc, tr in zip(result.tool_calls, tool_results):
                tool_content = json.dumps(tr["data"], indent=2)
                if phi_mapping:
                    tool_content, extra = phi.redact(tool_content)
                    phi_mapping.update(extra)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_content,
                })
            continue

        # --- TEXT PATH (single tool, Ollama fallback) ---
        content = result.content
        if content is None:
            if collected_tool_results:
                logger.warning("[%s] llm returned no content after %d tool results — synthesizing",
                               session.id, len(collected_tool_results))
                fallback = await get_healthy_provider()
                if fallback and fallback.config.name == provider.config.name:
                    fallback = None
                texts = []
                for tr in collected_tool_results:
                    texts.append(await _synthesize_tool_text(tr["tool"], tr["data"], provider=fallback))
                return AgentResult(text="\n\n".join(texts), tool_results=collected_tool_results)
            logger.warning("[%s] llm returned no content — keyword fallback", session.id)
            return await _run_with_keywords(user_message, session, provider=provider)

        # Restore PHI in LLM response
        if phi_mapping:
            content = phi.restore(content, phi_mapping)

        tool_call = _parse_tool_call(content)
        if tool_call:
            tool_name, params = tool_call
            logger.info("[%s] parsed tool from text: %s params=%s", session.id, tool_name, params)
            if "patient_id" in params:
                session.active_patients.add(params["patient_id"])

            # Inject client-side display hint the LLM can't know about
            if tool_name == "get_vitals_trend" and "display" not in params:
                hint = _detect_display_hint(user_message)
                if hint:
                    params["display"] = hint

            tool_result = await call_tool(tool_name, params, session)
            await _post_tool_hooks(tool_name, tool_result, params, session, provider.config.name)

            collected_tool_results.append({"tool": tool_name, "params": params, "data": tool_result})

            # Critical tool confirmation — return immediately, don't feed to LLM
            if tool_result.get("status") == "awaiting_confirmation":
                text = (
                    f"This is a critical action ({tool_name}) that requires confirmation.\n"
                    f"Confirmation ID: {tool_result['confirmation_id']}\n"
                    f"{tool_result['message']}"
                )
                logger.info("[%s] critical tool %s gated — returning confirmation", session.id, tool_name)
                return AgentResult(text=text, tool_results=collected_tool_results)

            messages.append({"role": "assistant", "content": content})
            tool_msg = f"Tool result for {tool_name}:\n{json.dumps(tool_result, indent=2)}"
            if phi_mapping:
                tool_msg, extra_mapping = phi.redact(tool_msg)
                phi_mapping.update(extra_mapping)
            messages.append({"role": "user", "content": tool_msg})
            continue

        logger.info("[%s] final response: %d chars, %d tool_results",
                    session.id, len(content), len(collected_tool_results))
        return AgentResult(text=content, tool_results=collected_tool_results)

    logger.warning("[%s] hit MAX_ITERATIONS=%d", session.id, MAX_ITERATIONS)
    return AgentResult(
        text="I've reached the maximum number of steps for this request. Please try a more specific query.",
        tool_results=collected_tool_results,
    )


async def _run_with_keywords(user_message: str, session: Session, provider=None) -> AgentResult:
    """Fallback: keyword-based intent detection and direct tool dispatch."""
    intent = _detect_intent(user_message)
    if intent is None:
        logger.info("[%s] keyword: no intent matched for %r", session.id, user_message[:80])
        return AgentResult(
            text="I couldn't determine what you're looking for. "
            "Try asking about vitals, medications, allergies, lab results, "
            "ward patients, blood availability, or inventory."
        )

    tool_name, params = intent
    logger.info("[%s] keyword: matched %s params=%s (llm_synth=%s)",
                session.id, tool_name, params, bool(provider))

    # Bed auto-resolution: if bed_id present but no patient_id, resolve first
    if "bed_id" in params and "patient_id" not in params:
        bed_result = await call_tool("resolve_bed", {"bed_id": params.pop("bed_id")}, session)
        if "error" in bed_result:
            return AgentResult(text=f"Could not resolve bed: {bed_result['error']}")
        params["patient_id"] = bed_result["patient_id"]

    if "patient_id" in params:
        session.active_patients.add(params["patient_id"])

    tool_result = await call_tool(tool_name, params, session)
    await _post_tool_hooks(tool_name, tool_result, params, session, None)

    collected = [{"tool": tool_name, "params": params, "data": tool_result}]

    if "error" in tool_result:
        return AgentResult(text=f"Error from {tool_name}: {tool_result['error']}", tool_results=collected)
    if "status" in tool_result and tool_result["status"] == "awaiting_confirmation":
        text = (
            f"This is a critical action ({tool_name}) that requires confirmation.\n"
            f"Confirmation ID: {tool_result['confirmation_id']}\n"
            f"{tool_result['message']}"
        )
        return AgentResult(text=text, tool_results=collected)

    # Synthesize a human-readable response
    text = await _synthesize_tool_text(tool_name, tool_result, provider)
    return AgentResult(text=text, tool_results=collected)


async def _synthesize_tool_text(tool_name: str, tool_result: dict, provider=None) -> str:
    """Produce a human-readable summary: LLM if available, else simple formatter."""
    if provider:
        try:
            result_json = json.dumps(tool_result, indent=2)
            # Truncate large payloads to avoid blowing context
            if len(result_json) > 4000:
                result_json = result_json[:4000] + "\n... (truncated)"
            prompt = _TOOL_SYNTHESIS_PROMPT.format(
                tool_name=tool_name, result_json=result_json,
            )
            result = await provider.chat([{"role": "user", "content": prompt}])
            if result and result.content:
                return result.content
        except Exception as exc:
            logger.warning("LLM synthesis failed, using text formatter: %s", exc)
    return _format_tool_result_text(tool_name, tool_result)


# ---------------------------------------------------------------------------
# Streaming agent loop (F3)
# ---------------------------------------------------------------------------

async def run_agent_stream(user_message: str, session: Session) -> AsyncIterator[dict]:
    """Process a user message, yielding SSE events between iterations."""
    session.append_message("user", user_message)
    t0 = time.time()

    provider = await get_healthy_provider()

    if provider:
        await _maybe_consolidate(session, provider)

    if not provider:
        # Keyword fallback — single text event
        result = await _run_with_keywords(user_message, session, provider=None)
        session.append_message("assistant", result.text)
        yield {"type": "text", "content": result.text}
        yield {"type": "done", "session_id": session.id}
        return

    # Build messages (same as _run_with_provider)
    clinical_context = ""
    for pid in session.active_patients:
        facts = await clinical_memory.get_facts(pid, session.tenant_id, limit=10)
        if facts:
            clinical_context += f"\nKnown facts for {pid}:\n"
            for f in facts:
                clinical_context += f"  - [{f['fact_type']}] {json.dumps(f['fact_data'])}\n"

    messages = [
        {"role": "system", "content": _build_system_prompt() + clinical_context},
    ]
    for msg in session.get_context(max_messages=10):
        messages.append({"role": msg["role"], "content": msg["content"]})

    phi_mapping = None
    if not provider.config.phi_safe:
        messages, phi_mapping = _redact_messages(messages)

    use_native = _supports_native_tools(provider)
    tool_defs = build_tool_definitions() if use_native else None

    for _ in range(MAX_ITERATIONS):
        result = await provider.chat(messages, tools=tool_defs)
        if result is None:
            kw_result = await _run_with_keywords(user_message, session, provider=provider)
            session.append_message("assistant", kw_result.text)
            yield {"type": "text", "content": kw_result.text}
            yield {"type": "done", "session_id": session.id}
            return

        # --- NATIVE TOOL PATH (parallel) ---
        if result.tool_calls:
            calls: list[tuple[str, dict]] = []
            for tc in result.tool_calls:
                params = dict(tc.params)
                if phi_mapping:
                    for k, v in params.items():
                        if isinstance(v, str):
                            params[k] = phi.restore(v, phi_mapping)
                if "patient_id" in params:
                    session.active_patients.add(params["patient_id"])
                if tc.name == "get_vitals_trend" and "display" not in params:
                    hint = _detect_display_hint(user_message)
                    if hint:
                        params["display"] = hint
                calls.append((tc.name, params))
                yield {"type": "tool_call", "tool": tc.name, "status": "started"}

            tool_results = await call_tools_parallel(calls, session)

            for tr in tool_results:
                await _post_tool_hooks(
                    tr["tool"], tr["data"], tr["params"], session, provider.config.name
                )
                yield {"type": "tool_result", "tool": tr["tool"], "data": tr["data"]}

            messages.append(result.raw_message)
            for tc, tr in zip(result.tool_calls, tool_results):
                tool_content = json.dumps(tr["data"], indent=2)
                if phi_mapping:
                    tool_content, extra = phi.redact(tool_content)
                    phi_mapping.update(extra)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_content,
                })
            continue

        # --- TEXT PATH (single tool, Ollama fallback) ---
        content = result.content
        if content is None:
            kw_result = await _run_with_keywords(user_message, session, provider=provider)
            session.append_message("assistant", kw_result.text)
            yield {"type": "text", "content": kw_result.text}
            yield {"type": "done", "session_id": session.id}
            return

        if phi_mapping:
            content = phi.restore(content, phi_mapping)

        tool_call = _parse_tool_call(content)
        if tool_call:
            tool_name, params = tool_call
            if "patient_id" in params:
                session.active_patients.add(params["patient_id"])

            yield {"type": "tool_call", "tool": tool_name, "status": "started"}

            tool_result = await call_tool(tool_name, params, session)
            await _post_tool_hooks(tool_name, tool_result, params, session, provider.config.name)

            yield {"type": "tool_result", "tool": tool_name, "data": tool_result}

            messages.append({"role": "assistant", "content": content})
            tool_msg = f"Tool result for {tool_name}:\n{json.dumps(tool_result, indent=2)}"
            if phi_mapping:
                tool_msg, extra_mapping = phi.redact(tool_msg)
                phi_mapping.update(extra_mapping)
            messages.append({"role": "user", "content": tool_msg})
            continue

        # Final text response
        session.append_message("assistant", content)
        yield {"type": "text", "content": content}
        yield {"type": "done", "session_id": session.id}

        latency_ms = int((time.time() - t0) * 1000)
        await audit.log_action(
            tenant_id=session.tenant_id,
            session_id=session.id,
            user_id=session.user_id,
            channel=session.channel,
            action="chat_response",
            latency_ms=latency_ms,
            provider=provider.config.name,
            model=provider.config.model,
        )
        return

    yield {"type": "text", "content": "I've reached the maximum number of steps for this request."}
    yield {"type": "done", "session_id": session.id}
