"""Agent loop — intent detection, tool dispatch, and response synthesis."""

import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from typing import Any

import audit
import clinical_memory
import phi
from providers import get_provider
from session import Session
from tools import call_tool, get_tool_list

logger = logging.getLogger("nanobot.agent")

MAX_ITERATIONS = 10
CONSOLIDATION_THRESHOLD = int(os.environ.get("CONSOLIDATION_THRESHOLD", "30"))

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are Hobot, a clinical AI assistant for hospital staff.
You have access to the following tools to query hospital systems.
When you need data, call a tool. Never fabricate clinical data.
Always cite which tool provided the data.
For critical actions (marked critical), the system will require human confirmation before execution.

Available tools:
{tools}

Respond concisely and professionally. Use structured formatting when presenting clinical data."""


def _build_system_prompt() -> str:
    tools = get_tool_list()
    tool_desc = "\n".join(
        f"- {t['name']}" + (" [CRITICAL]" if t["critical"] else "")
        for t in tools
    )
    return SYSTEM_PROMPT.format(tools=tool_desc)


# ---------------------------------------------------------------------------
# Keyword-based fallback intent detection
# ---------------------------------------------------------------------------

_INTENT_PATTERNS: list[tuple[re.Pattern, str, callable]] = [
    (re.compile(r"vitals?\s+(?:for\s+)?(\w+)", re.I), "get_vitals", lambda m: {"patient_id": m.group(1)}),
    (re.compile(r"vitals?\s+history\s+(?:for\s+)?(\w+)", re.I), "get_vitals_history", lambda m: {"patient_id": m.group(1)}),
    (re.compile(r"medications?\s+(?:for\s+)?(\w+)", re.I), "get_medications", lambda m: {"patient_id": m.group(1)}),
    (re.compile(r"allergies\s+(?:for\s+)?(\w+)", re.I), "get_allergies", lambda m: {"patient_id": m.group(1)}),
    (re.compile(r"lab\s+results?\s+(?:for\s+)?(\w+)", re.I), "get_lab_results", lambda m: {"patient_id": m.group(1)}),
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
            return tool_name, extract(match)
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
    return result


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

async def run_agent(user_message: str, session: Session) -> str:
    """Process a user message through the agent loop. Returns response text."""
    session.append_message("user", user_message)
    t0 = time.time()

    provider = get_provider()
    provider_available = provider and await provider.is_available()

    # Consolidation check (F2)
    if provider_available:
        await _maybe_consolidate(session, provider)

    if provider_available:
        result = await _run_with_provider(user_message, session, provider)
    else:
        result = await _run_with_keywords(user_message, session)

    latency_ms = int((time.time() - t0) * 1000)
    await audit.log_action(
        tenant_id=session.tenant_id,
        session_id=session.id,
        user_id=session.user_id,
        channel=session.channel,
        action="chat_response",
        latency_ms=latency_ms,
        provider=provider.config.name if provider_available else "keyword_fallback",
        model=provider.config.model if provider_available else None,
    )

    session.append_message("assistant", result)
    return result


async def _run_with_provider(user_message: str, session: Session, provider) -> str:
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

    for _ in range(MAX_ITERATIONS):
        content = await provider.chat(messages)
        if content is None:
            return await _run_with_keywords(user_message, session)

        # Restore PHI in LLM response
        if phi_mapping:
            content = phi.restore(content, phi_mapping)

        tool_call = _parse_tool_call(content)
        if tool_call:
            tool_name, params = tool_call
            if "patient_id" in params:
                session.active_patients.add(params["patient_id"])

            tool_result = await call_tool(tool_name, params, session)
            await _post_tool_hooks(tool_name, tool_result, params, session, provider.config.name)

            messages.append({"role": "assistant", "content": content})
            tool_msg = f"Tool result for {tool_name}:\n{json.dumps(tool_result, indent=2)}"
            if phi_mapping:
                tool_msg, extra_mapping = phi.redact(tool_msg)
                phi_mapping.update(extra_mapping)
            messages.append({"role": "user", "content": tool_msg})
            continue

        return content

    return "I've reached the maximum number of steps for this request. Please try a more specific query."


async def _run_with_keywords(user_message: str, session: Session) -> str:
    """Fallback: keyword-based intent detection and direct tool dispatch."""
    intent = _detect_intent(user_message)
    if intent is None:
        return (
            "I couldn't determine what you're looking for. "
            "Try asking about vitals, medications, allergies, lab results, "
            "ward patients, blood availability, or inventory."
        )

    tool_name, params = intent

    if "patient_id" in params:
        session.active_patients.add(params["patient_id"])

    tool_result = await call_tool(tool_name, params, session)
    await _post_tool_hooks(tool_name, tool_result, params, session, None)

    if "error" in tool_result:
        return f"Error from {tool_name}: {tool_result['error']}"
    if "status" in tool_result and tool_result["status"] == "awaiting_confirmation":
        return (
            f"This is a critical action ({tool_name}) that requires confirmation.\n"
            f"Confirmation ID: {tool_result['confirmation_id']}\n"
            f"{tool_result['message']}"
        )
    return f"**{tool_name}** result:\n```json\n{json.dumps(tool_result, indent=2)}\n```"


# ---------------------------------------------------------------------------
# Streaming agent loop (F3)
# ---------------------------------------------------------------------------

async def run_agent_stream(user_message: str, session: Session) -> AsyncIterator[dict]:
    """Process a user message, yielding SSE events between iterations."""
    session.append_message("user", user_message)
    t0 = time.time()

    provider = get_provider()
    provider_available = provider and await provider.is_available()

    if provider_available:
        await _maybe_consolidate(session, provider)

    if not provider_available:
        # Keyword fallback — single text event
        result = await _run_with_keywords(user_message, session)
        session.append_message("assistant", result)
        yield {"type": "text", "content": result}
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

    for _ in range(MAX_ITERATIONS):
        content = await provider.chat(messages)
        if content is None:
            result = await _run_with_keywords(user_message, session)
            session.append_message("assistant", result)
            yield {"type": "text", "content": result}
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
