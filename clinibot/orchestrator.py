"""Orchestrator — system-prompt-driven agent loop with skills framework.

Replaces agent.py's 3-path architecture (native tools, text parsing, keyword
fallback) with a single native-tool-calling loop that auto-invokes skill
interpreters after each tool result.
"""

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import audit
import classifier
import clinical_memory
import metrics as _metrics
import phi


def _get_request_id() -> str:
    """Get current request_id from main module's ContextVar (avoids circular import)."""
    try:
        from main import request_id_var
        return request_id_var.get("")
    except (ImportError, LookupError):
        return ""
from providers import ChatResult, get_healthy_provider, get_provider
from session import Session
from skills import SkillInput, SkillRegistry
from tools import build_tool_definitions, call_tool, call_tools_parallel

logger = logging.getLogger("clinibot.orchestrator")

MAX_ITERATIONS = 10
CONSOLIDATION_THRESHOLD = int(os.environ.get("CONSOLIDATION_THRESHOLD", "30"))


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class OrchestratorResult:
    """Structured result from the orchestrator."""
    text: str
    tool_results: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# SSE event types (same contract as agent.py for backward compat)
# ---------------------------------------------------------------------------

@dataclass
class ToolCallStarted:
    tool: str
    params: dict

@dataclass
class ToolResultEvent:
    tool: str
    params: dict
    data: dict

@dataclass
class ConfirmationEvent:
    tool: str
    data: dict
    text: str
    collected: list[dict]

@dataclass
class TextResponseEvent:
    content: str
    collected_tool_results: list[dict]


# ---------------------------------------------------------------------------
# System prompt loading
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_file_if_exists(path: Path) -> str:
    """Load a text file if it exists, else return empty string."""
    if path.exists():
        return path.read_text()
    return ""


def load_system_prompt(domains: list[str] | None = None) -> str:
    """Build system prompt from config/system_prompt.md + SKILLS.md + tool list."""
    system_prompt_md = _load_file_if_exists(_PROJECT_ROOT / "config" / "system_prompt.md")
    skills_md = _load_file_if_exists(_PROJECT_ROOT / "SKILLS.md")

    # Build dynamic tool list
    from tools import get_tool_list, _TOOL_DESCRIPTIONS, is_critical
    tools = get_tool_list(domains=domains)
    tool_lines = []
    for t in tools:
        desc = _TOOL_DESCRIPTIONS.get(t["name"], "")
        critical = " [CRITICAL]" if t["critical"] else ""
        tool_lines.append(f"- {t['name']}: {desc}{critical}")

    return (
        f"{system_prompt_md}\n\n"
        f"# Skills Reference\n{skills_md}\n\n"
        f"# Available Tools\n" + "\n".join(tool_lines)
    )


# ---------------------------------------------------------------------------
# Memory consolidation
# ---------------------------------------------------------------------------

_CONSOLIDATION_PROMPT = """Summarize this clinical conversation history concisely.
Preserve: patient IDs, diagnoses, key vitals, medications, pending actions, and clinical decisions.
If there is an existing summary, integrate new information into it.

Existing summary: {existing_summary}

Messages to consolidate:
{messages}

Provide a concise clinical summary:"""


async def _maybe_consolidate(session: Session, provider=None) -> None:
    """Consolidate old messages if we've exceeded the threshold."""
    unconsolidated = len(session.messages) - session.last_consolidated
    if unconsolidated < CONSOLIDATION_THRESHOLD:
        return

    consolidate_end = len(session.messages) - 10
    if consolidate_end <= session.last_consolidated:
        return

    to_consolidate = session.messages[session.last_consolidated:consolidate_end]
    summary = session.summary

    if provider and await provider.is_available():
        msg_text = "\n".join(f"[{m['role']}] {m['content']}" for m in to_consolidate)
        prompt = _CONSOLIDATION_PROMPT.format(
            existing_summary=session.summary or "(none)",
            messages=msg_text,
        )
        result = await provider.chat([{"role": "user", "content": prompt}])
        if result and result.content:
            summary = result.content

    session.save_consolidation(summary, consolidate_end)
    session.messages = session.messages[consolidate_end:]
    session.last_consolidated = 0
    logger.info("Consolidated session %s (kept %d messages)", session.id, len(session.messages))


# ---------------------------------------------------------------------------
# PHI helpers
# ---------------------------------------------------------------------------

def _redact_messages(messages: list[dict]) -> tuple[list[dict], dict[str, str]]:
    combined_mapping: dict[str, str] = {}
    redacted = []
    for msg in messages:
        redacted_content, mapping = phi.redact(msg.get("content", ""))
        combined_mapping.update(mapping)
        redacted.append({**msg, "content": redacted_content})
    return redacted, combined_mapping


# ---------------------------------------------------------------------------
# Post-tool hooks
# ---------------------------------------------------------------------------

async def _post_tool_hooks(
    tool_name: str, tool_result: dict, params: dict,
    session: Session, provider_name: str | None,
) -> None:
    patient_id = params.get("patient_id", "")
    await clinical_memory.extract_and_store(
        tool_name, tool_result, patient_id, session.id, session.tenant_id,
    )
    result_summary = json.dumps(tool_result)[:200]
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
        request_id=_get_request_id(),
    )


# ---------------------------------------------------------------------------
# Core orchestrator loop (async generator)
# ---------------------------------------------------------------------------

async def _orchestrator_loop(
    user_message: str,
    session: Session,
    provider,
    domains: list[str],
    skill_registry: SkillRegistry,
) -> AsyncIterator:
    """Single native-tool-calling loop with auto skill interpretation."""
    # Build clinical context
    clinical_context = ""
    for pid in session.active_patients:
        facts = await clinical_memory.get_facts(pid, session.tenant_id, limit=10)
        if facts:
            clinical_context += f"\nKnown facts for {pid}:\n"
            for f in facts:
                clinical_context += f"  - [{f['fact_type']}] {json.dumps(f['fact_data'])}\n"

    system_prompt = load_system_prompt(domains=domains) + clinical_context
    messages = [{"role": "system", "content": system_prompt}]
    for msg in session.get_context(max_messages=10):
        messages.append({"role": msg["role"], "content": msg["content"]})

    # PHI redaction
    phi_mapping = None
    if not provider.config.phi_safe:
        messages, phi_mapping = _redact_messages(messages)

    tool_defs = build_tool_definitions(domains=domains)
    # Add skill tool definitions
    tool_defs.extend(skill_registry.tool_definitions())

    collected_tool_results: list[dict] = []

    for iteration in range(MAX_ITERATIONS):
        logger.info("[%s] orchestrator iteration=%d provider=%s",
                    session.id, iteration, provider.config.name)
        _t0 = time.time()
        result = await provider.chat(messages, tools=tool_defs)
        _metrics.LLM_CALLS.labels(provider=provider.config.name, status="ok" if result else "null").inc()
        _metrics.LLM_DURATION.labels(provider=provider.config.name).observe(time.time() - _t0)

        if result is None:
            if collected_tool_results:
                logger.warning("[%s] provider returned None after tool results — returning collected", session.id)
                texts = [f"**{tr['tool']}**: {json.dumps(tr['data'])[:200]}" for tr in collected_tool_results]
                yield TextResponseEvent(content="\n\n".join(texts), collected_tool_results=collected_tool_results)
                return
            logger.warning("[%s] provider returned None — no fallback", session.id)
            yield TextResponseEvent(
                content="I'm having trouble connecting to the AI service. Please try again.",
                collected_tool_results=[],
            )
            return

        # --- Tool calls ---
        if result.tool_calls:
            call_names = [tc.name for tc in result.tool_calls]
            logger.info("[%s] tool_calls: %s", session.id, call_names)

            calls: list[tuple[str, dict]] = []
            skill_calls: list[tuple[str, dict]] = []  # tools handled by skills directly
            for tc in result.tool_calls:
                params = dict(tc.params)
                if phi_mapping:
                    for k, v in params.items():
                        if isinstance(v, str):
                            params[k] = phi.restore(v, phi_mapping)
                if "patient_id" in params:
                    session.active_patients.add(params["patient_id"])
                yield ToolCallStarted(tool=tc.name, params=params)
                # Route skill-only tools (e.g. analyze_radiology_image) to skill.run()
                skill = skill_registry.get_skill(tc.name)
                if skill:
                    skill_calls.append((tc.name, params))
                else:
                    calls.append((tc.name, params))

            # Dispatch backend tools
            tool_results = await call_tools_parallel(calls, session) if calls else []

            # Dispatch skill-only tools
            for s_name, s_params in skill_calls:
                skill = skill_registry.get_skill(s_name)
                try:
                    s_input = SkillInput(
                        tool_name=s_name, tool_result={}, params=s_params,
                        patient_id=s_params.get("patient_id", ""),
                        session_id=session.id, tenant_id=session.tenant_id,
                    )
                    s_output = await asyncio.wait_for(skill.run(s_input), timeout=skill.timeout)
                    tool_results.append({
                        "tool": s_name, "params": s_params,
                        "data": s_output.to_analysis_dict(),
                    })
                except Exception as exc:
                    logger.warning("Skill tool %s failed: %s", s_name, exc)
                    tool_results.append({
                        "tool": s_name, "params": s_params,
                        "data": {"error": str(exc)},
                    })

            for tr in tool_results:
                await _post_tool_hooks(tr["tool"], tr["data"], tr["params"], session, provider.config.name)

                # Auto-invoke skill interpreter
                skill = skill_registry.get_interpreter(tr["tool"])
                if tr["params"].get("skip_analysis"):
                    skill = None  # caller opted out of auto-interpretation
                if skill and isinstance(tr["data"], dict) and "error" not in tr["data"]:
                    try:
                        skill_input = SkillInput(
                            tool_name=tr["tool"],
                            tool_result=tr["data"],
                            params=tr["params"],
                            patient_id=tr["params"].get("patient_id", ""),
                            session_id=session.id,
                            tenant_id=session.tenant_id,
                        )
                        skill_output = await asyncio.wait_for(
                            skill.run(skill_input),
                            timeout=skill.timeout,
                        )
                        if skill_output.status not in ("error", "skipped"):
                            tr["data"]["_analysis"] = skill_output.to_analysis_dict()
                    except asyncio.TimeoutError:
                        logger.warning("Skill %s timed out for %s", skill.name, tr["tool"])
                    except Exception as exc:
                        logger.warning("Skill %s failed for %s: %s", skill.name, tr["tool"], exc)

                yield ToolResultEvent(tool=tr["tool"], params=tr["params"], data=tr["data"])
            collected_tool_results.extend(tool_results)

            # Handle tools_expanded (domain escape hatch)
            expanded = [tr for tr in tool_results if isinstance(tr["data"], dict) and tr["data"].get("status") == "tools_expanded"]
            if expanded:
                for d in expanded[0]["data"].get("added_domains", []):
                    if d not in domains and len(domains) < 5:
                        domains.append(d)
                messages[0] = {"role": "system", "content": load_system_prompt(domains=domains) + clinical_context}
                tool_defs = build_tool_definitions(domains=domains)
                tool_defs.extend(skill_registry.tool_definitions())

            # Critical tool confirmation — return immediately
            confirmations = [tr for tr in tool_results if isinstance(tr["data"], dict) and tr["data"].get("status") == "awaiting_confirmation"]
            if confirmations:
                cr = confirmations[0]
                text = (
                    f"This is a critical action ({cr['tool']}) that requires confirmation.\n"
                    f"Confirmation ID: {cr['data']['confirmation_id']}\n"
                    f"{cr['data']['message']}"
                )
                yield ConfirmationEvent(tool=cr["tool"], data=cr["data"], text=text, collected=collected_tool_results)
                return

            # Append to messages for next iteration
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

        # --- Final text response ---
        content = result.content
        if not content:
            if collected_tool_results:
                texts = [f"**{tr['tool']}**: {json.dumps(tr['data'])[:200]}" for tr in collected_tool_results]
                yield TextResponseEvent(content="\n\n".join(texts), collected_tool_results=collected_tool_results)
                return
            yield TextResponseEvent(content="I couldn't generate a response. Please try again.", collected_tool_results=[])
            return

        if phi_mapping:
            content = phi.restore(content, phi_mapping)

        yield TextResponseEvent(content=content, collected_tool_results=collected_tool_results)
        return

    yield TextResponseEvent(
        content="I've reached the maximum number of steps for this request. Please try a more specific query.",
        collected_tool_results=collected_tool_results,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def run_orchestrator(
    user_message: str,
    session: Session,
    skill_registry: SkillRegistry,
) -> OrchestratorResult:
    """Process a user message through the orchestrator. Returns OrchestratorResult."""
    session.append_message("user", user_message)
    t0 = time.time()

    provider = await get_healthy_provider()
    logger.info("[%s] query=%r provider=%s", session.id, user_message[:80],
                provider.config.name if provider else "NONE")

    if provider:
        await _maybe_consolidate(session, provider)

    if not provider:
        logger.warning("[%s] no healthy provider", session.id)
        result = OrchestratorResult(
            text="All AI services are currently unavailable. Please try again later.",
        )
        session.append_message("assistant", result.text)
        return result

    domains = await classifier.classify(user_message, session)
    logger.info("[%s] classified domains=%s", session.id, domains)

    result = OrchestratorResult(text="Max iterations reached.", tool_results=[])
    async for event in _orchestrator_loop(user_message, session, provider, domains, skill_registry):
        if isinstance(event, TextResponseEvent):
            result = OrchestratorResult(text=event.content, tool_results=event.collected_tool_results)
        elif isinstance(event, ConfirmationEvent):
            result = OrchestratorResult(text=event.text, tool_results=event.collected)

    latency_ms = int((time.time() - t0) * 1000)
    await audit.log_action(
        tenant_id=session.tenant_id,
        session_id=session.id,
        user_id=session.user_id,
        channel=session.channel,
        action="chat_response",
        request_id=_get_request_id(),
        latency_ms=latency_ms,
        provider=provider.config.name,
        model=provider.config.model,
    )

    session.append_message("assistant", result.text)
    return result


async def run_orchestrator_stream(
    user_message: str,
    session: Session,
    skill_registry: SkillRegistry,
) -> AsyncIterator[dict]:
    """Process a user message, yielding SSE events."""
    session.append_message("user", user_message)
    t0 = time.time()

    provider = await get_healthy_provider()

    if provider:
        await _maybe_consolidate(session, provider)

    if not provider:
        session.append_message("assistant", "All AI services are currently unavailable.")
        yield {"type": "text", "content": "All AI services are currently unavailable."}
        yield {"type": "done", "session_id": session.id}
        return

    domains = await classifier.classify(user_message, session)

    async for event in _orchestrator_loop(user_message, session, provider, domains, skill_registry):
        if isinstance(event, ToolCallStarted):
            yield {"type": "tool_call", "tool": event.tool, "status": "started"}
        elif isinstance(event, ToolResultEvent):
            # Redact PHI in SSE tool results for non-PHI-safe providers
            data = event.data
            if provider and not provider.config.phi_safe:
                redacted_str, _ = phi.redact(json.dumps(data))
                try:
                    data = json.loads(redacted_str)
                except (json.JSONDecodeError, ValueError):
                    pass
            yield {"type": "tool_result", "tool": event.tool, "data": data}
        elif isinstance(event, ConfirmationEvent):
            session.append_message("assistant", event.text)
            yield {"type": "text", "content": event.text}
            yield {"type": "done", "session_id": session.id}
            await audit.log_action(
                tenant_id=session.tenant_id, session_id=session.id,
                user_id=session.user_id, channel=session.channel,
                action="chat_response", request_id=_get_request_id(),
                latency_ms=int((time.time() - t0) * 1000),
                provider=provider.config.name, model=provider.config.model,
            )
            return
        elif isinstance(event, TextResponseEvent):
            session.append_message("assistant", event.content)
            yield {"type": "text", "content": event.content}
            yield {"type": "done", "session_id": session.id}
            await audit.log_action(
                tenant_id=session.tenant_id, session_id=session.id,
                user_id=session.user_id, channel=session.channel,
                action="chat_response", request_id=_get_request_id(),
                latency_ms=int((time.time() - t0) * 1000),
                provider=provider.config.name, model=provider.config.model,
            )
            return

    yield {"type": "text", "content": "I've reached the maximum number of steps."}
    yield {"type": "done", "session_id": session.id}
