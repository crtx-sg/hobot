"""Hobot Gateway — FastAPI app that orchestrates all MCP tool servers."""

import json
import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import audit
import clinical_memory
import formatter
import providers
import session as session_mgr
import tools
from agent import run_agent, run_agent_stream

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("nanobot")

AUDIT_DB = os.environ.get("AUDIT_DB", "/data/audit/clinic.db")
SCHEMA_PATH = os.environ.get("SCHEMA_PATH", "/app/schema/init.sql")
TOOLS_CONFIG = os.environ.get("TOOLS_CONFIG", "/app/config/tools.json")
CHANNELS_CONFIG = os.environ.get("CHANNELS_CONFIG", "/app/config/channels.json")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config/config.json")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await audit.init_db(AUDIT_DB, SCHEMA_PATH)
    clinical_memory.bind_db(audit._db)
    tools.load_tools_config(TOOLS_CONFIG)
    formatter.load_channels_config(CHANNELS_CONFIG)
    providers.load_providers(CONFIG_PATH)
    logger.info("Nanobot gateway started")
    yield
    # Shutdown
    await audit.close_db()
    logger.info("Nanobot gateway stopped")


app = FastAPI(title="Hobot Gateway", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    user_id: str
    channel: str = "webchat"
    tenant_id: str = "default"


class ChatResponse(BaseModel):
    response: str
    session_id: str


class ConfirmResponse(BaseModel):
    result: dict


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Health check — pings each synthetic backend."""
    # (name, base_url, health_path)
    backends = [
        ("synthetic-monitoring", os.environ.get("MONITORING_BASE", "http://synthetic-monitoring:8000"), "/health"),
        ("synthetic-ehr", os.environ.get("EHR_BASE", "http://synthetic-ehr:8080"), "/fhir/metadata"),
        ("synthetic-lis", os.environ.get("LIS_BASE", "http://synthetic-lis:8000"), "/health"),
        ("synthetic-pharmacy", os.environ.get("PHARMACY_BASE", "http://synthetic-pharmacy:8000"), "/health"),
        ("synthetic-radiology", os.environ.get("RADIOLOGY_BASE", "http://synthetic-radiology:8042"), "/system"),
        ("synthetic-bloodbank", os.environ.get("BLOODBANK_BASE", "http://synthetic-bloodbank:8000"), "/health"),
        ("synthetic-erp", os.environ.get("ERP_BASE", "http://synthetic-erp:8000"), "/health"),
        ("synthetic-patient-services", os.environ.get("PATIENT_SERVICES_BASE", "http://synthetic-patient-services:8000"), "/health"),
    ]
    statuses = {}
    async with httpx.AsyncClient(timeout=5.0) as client:
        for name, base_url, health_path in backends:
            try:
                kwargs = {}
                if "radiology" in name:
                    kwargs["auth"] = ("orthanc", "orthanc")
                resp = await client.get(f"{base_url}{health_path}", **kwargs)
                statuses[name] = "ok" if resp.status_code == 200 else f"status={resp.status_code}"
            except Exception as exc:
                statuses[name] = f"unreachable: {exc}"

    all_ok = all(s == "ok" for s in statuses.values())
    return {
        "status": "ok" if all_ok else "degraded",
        "service": "nanobot-gateway",
        "backends": statuses,
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Main chat endpoint — processes user message through the agent loop."""
    sess = session_mgr.get_or_create(
        session_id=request.session_id,
        tenant_id=request.tenant_id,
        user_id=request.user_id,
        channel=request.channel,
    )

    raw_response = await run_agent(request.message, sess)
    formatted = formatter.format_response(raw_response, request.channel)

    return ChatResponse(response=formatted, session_id=sess.id)


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """Streaming chat endpoint — emits SSE events between agent iterations."""
    sess = session_mgr.get_or_create(
        session_id=request.session_id,
        tenant_id=request.tenant_id,
        user_id=request.user_id,
        channel=request.channel,
    )

    async def event_generator():
        async for event in run_agent_stream(request.message, sess):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/confirm/{confirmation_id}", response_model=ConfirmResponse)
async def confirm(confirmation_id: str):
    """Execute a pending critical tool after human confirmation."""
    # We need a session for the confirmation — look it up from pending
    from tools import _pending
    entry = _pending.get(confirmation_id)
    if entry is None:
        return ConfirmResponse(result={"error": "Confirmation not found or already executed"})

    sess = session_mgr.get(entry["session_id"])
    if sess is None:
        return ConfirmResponse(result={"error": "Session expired"})

    result = await tools.confirm_tool(confirmation_id, sess)
    return ConfirmResponse(result=result)
