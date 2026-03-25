"""Hobot Gateway — FastAPI app that orchestrates all MCP tool servers."""

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import audit
import clinical_memory
import formatter
import providers
import session as session_mgr
import tools
import metrics as _metrics
from auth import AuthMiddleware, load_api_keys

_skill_registry = None

# ---------------------------------------------------------------------------
# Structured JSON logging (S8a)
# ---------------------------------------------------------------------------

request_id_var: ContextVar[str] = ContextVar("request_id", default="")

try:
    from pythonjsonlogger import jsonlogger

    class ClinicJsonFormatter(jsonlogger.JsonFormatter):
        def add_fields(self, log_record, record, message_dict):
            super().add_fields(log_record, record, message_dict)
            log_record["request_id"] = request_id_var.get("")
            log_record["timestamp"] = datetime.now(timezone.utc).isoformat()

    _handler = logging.StreamHandler()
    _handler.setFormatter(ClinicJsonFormatter("%(name)s %(levelname)s %(message)s"))
    logging.root.handlers = [_handler]
    logging.root.setLevel(logging.INFO)
except ImportError:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

logger = logging.getLogger("clinibot")

AUDIT_DB = os.environ.get("AUDIT_DB", "/data/audit/clinic.db")
SCHEMA_PATH = os.environ.get("SCHEMA_PATH", "/app/schema/init.sql")
TOOLS_CONFIG = os.environ.get("TOOLS_CONFIG", "/app/config/tools.json")
CHANNELS_CONFIG = os.environ.get("CHANNELS_CONFIG", "/app/config/channels.json")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config/config.json")


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")


async def _loop_with_backoff(fn, base_interval: float, max_backoff: float = 300.0):
    """Run fn() in a loop with exponential backoff on consecutive failures."""
    consecutive_failures = 0
    while True:
        if consecutive_failures == 0:
            delay = base_interval
        else:
            delay = min(base_interval * (2 ** consecutive_failures), max_backoff)
        await asyncio.sleep(delay)
        try:
            await fn()
            consecutive_failures = 0
        except Exception as exc:
            consecutive_failures += 1
            logger.error("Background loop %s failed (attempt %d): %s",
                         fn.__name__, consecutive_failures, exc)


async def _cleanup_tick():
    """Single cleanup tick: expired clinical facts + expired confirmations + stale sessions."""
    deleted = await clinical_memory.cleanup_expired()
    if deleted:
        logger.info("Cleaned up %d expired clinical facts", deleted)
    expired_confirms = tools.cleanup_expired_confirmations()
    if expired_confirms:
        logger.info("Cleaned up %d expired confirmations", expired_confirms)
    session_mgr.evict_stale()


async def _cleanup_loop():
    interval = clinical_memory._cleanup_interval_minutes * 60
    await _loop_with_backoff(_cleanup_tick, interval)


async def _reminder_tick():
    """Single reminder poll tick."""
    patient_services_base = os.environ.get("PATIENT_SERVICES_BASE", "http://synthetic-patient-services:8000")
    client = tools._http_client or httpx.AsyncClient(timeout=10.0)
    try:
        resp = await client.get(f"{patient_services_base}/reminders/due")
        if resp.status_code != 200:
            return
        due = resp.json().get("reminders", [])
    finally:
        if not tools._http_client:
            await client.aclose()

    for rem in due:
        rem_id = rem.get("reminder_id", "")
        session_id = rem.get("session_id", "")
        if session_id.startswith("tg-") and TELEGRAM_BOT_TOKEN:
            chat_id = session_id[3:]
            text = f"\u23f0 Reminder: {rem['message']}"
            tg_client = tools._http_client or httpx.AsyncClient(timeout=10.0)
            try:
                await tg_client.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                )
                logger.info("Fired reminder %s to chat %s", rem_id, chat_id)
            except Exception as exc:
                logger.error("Failed to send reminder %s: %s", rem_id, exc)
            finally:
                if not tools._http_client:
                    await tg_client.aclose()
        else:
            logger.info("Fired reminder %s (non-telegram, session=%s)", rem_id, session_id)


async def _reminder_loop():
    await _loop_with_backoff(_reminder_tick, 30)


def _init_skill_registry():
    """Initialize skill registry with all skills and domain models."""
    global _skill_registry

    from skills import SkillRegistry
    from domain_models.clinical_reasoning import ClinicalReasoningModel
    from domain_models.radiology_model import RadiologyModel
    from domain_models.vitals_anomaly import VitalsAnomalyModel
    from domain_models.drug_interaction import DrugInteractionModel

    # Load skills config
    skills_config = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        skills_config = data.get("skills", {})

    # Initialize domain models
    domain_models = {
        "clinical_reasoning": ClinicalReasoningModel(
            provider_name=skills_config.get("clinical_reasoning", {}).get("provider", "gemini"),
        ),
        "radiology_model": RadiologyModel(
            provider_name=skills_config.get("radiology_model", {}).get("provider", "gemini"),
        ),
        "vitals_anomaly": VitalsAnomalyModel(),
        "drug_interaction": DrugInteractionModel(),
    }

    # Initialize and register skills
    _skill_registry = SkillRegistry()

    from skills.interpret_vitals import InterpretVitalsSkill
    from skills.interpret_labs import InterpretLabsSkill
    from skills.interpret_radiology import InterpretRadiologySkill, AnalyzeRadiologyImageSkill
    from skills.interpret_ecg import InterpretECGSkill
    from skills.medication_validation import MedicationValidationSkill
    from skills.blood_availability import BloodAvailabilitySkill
    from skills.service_orchestration import ServiceOrchestrationSkill
    from skills.clinical_context import ClinicalContextSkill
    from skills.clinical_summary import ClinicalSummarySkill
    from skills.risk_scoring import RiskScoringSkill
    from skills.care_plan_summary import CarePlanSummarySkill

    skill_classes = [
        InterpretVitalsSkill, InterpretLabsSkill, InterpretRadiologySkill,
        AnalyzeRadiologyImageSkill, InterpretECGSkill, MedicationValidationSkill,
        BloodAvailabilitySkill, ServiceOrchestrationSkill, ClinicalContextSkill,
        ClinicalSummarySkill, RiskScoringSkill, CarePlanSummarySkill,
    ]

    for cls in skill_classes:
        skill_cfg = skills_config.get(cls.name, {}) if hasattr(cls, 'name') else {}
        skill = cls(config=skill_cfg, domain_models=domain_models)
        _skill_registry.register(skill)

    logger.info("Skill registry initialized with %d skills", len(_skill_registry.all_skills()))
    return _skill_registry


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    load_api_keys()
    tools.init_http_client()
    await audit.init_db(AUDIT_DB, SCHEMA_PATH)
    clinical_memory.bind_db(audit._db)
    clinical_memory.load_memory_config(CONFIG_PATH)
    tools.load_tools_config(TOOLS_CONFIG)
    tools.load_domain_config(CONFIG_PATH)
    import classifier
    classifier.load_classifier_config(CONFIG_PATH)
    formatter.load_channels_config(CHANNELS_CONFIG)
    providers.load_providers(CONFIG_PATH)
    # Initialize skill registry + domain models
    _init_skill_registry()
    reminder_task = asyncio.create_task(_reminder_loop())
    cleanup_task = asyncio.create_task(_cleanup_loop())
    logger.info("Clinibot gateway started")
    yield
    # Shutdown
    reminder_task.cancel()
    cleanup_task.cancel()
    await tools.close_http_client()
    await audit.close_db()
    logger.info("Clinibot gateway stopped")


app = FastAPI(title="Hobot Gateway", lifespan=lifespan)

# Rate limiting middleware — loaded from config
def _setup_rate_limiting():
    if not os.path.exists(CONFIG_PATH):
        return
    with open(CONFIG_PATH) as f:
        data = json.load(f)
    rl_cfg = data.get("rate_limits")
    if not rl_cfg:
        return
    from ratelimit import RateLimitMiddleware, SlidingWindowLimiter
    user_cfg = rl_cfg.get("per_user", {})
    tenant_cfg = rl_cfg.get("per_tenant", {})
    user_limiter = SlidingWindowLimiter(
        user_cfg.get("requests", 30), user_cfg.get("window_seconds", 60)
    )
    tenant_limiter = SlidingWindowLimiter(
        tenant_cfg.get("requests", 200), tenant_cfg.get("window_seconds", 60)
    )
    app.add_middleware(RateLimitMiddleware, user_limiter=user_limiter, tenant_limiter=tenant_limiter)

_setup_rate_limiting()
app.add_middleware(AuthMiddleware)

# CORS — configurable via env; defaults to same-origin (empty list)
_cors_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

# Prometheus /metrics endpoint (S8b)
try:
    from prometheus_client import make_asgi_app as _make_metrics_app
    _metrics_app = _make_metrics_app()
    app.mount("/metrics", _metrics_app)
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

MAX_MESSAGE_LENGTH = int(os.environ.get("MAX_MESSAGE_LENGTH", "10000"))


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    user_id: str
    channel: str = "webchat"
    tenant_id: str = "default"


class ChatResponse(BaseModel):
    response: str
    session_id: str
    blocks: list[dict] | None = None


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
                    kwargs["auth"] = (os.environ.get("ORTHANC_USER", "orthanc"), os.environ.get("ORTHANC_PASS", "orthanc"))
                resp = await client.get(f"{base_url}{health_path}", **kwargs)
                statuses[name] = "ok" if resp.status_code == 200 else f"status={resp.status_code}"
            except Exception as exc:
                statuses[name] = f"unreachable: {exc}"

    all_ok = all(s == "ok" for s in statuses.values())
    return {
        "status": "ok" if all_ok else "degraded",
        "service": "clinibot-gateway",
        "backends": statuses,
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Main chat endpoint — processes user message through the orchestrator."""
    if len(request.message) > MAX_MESSAGE_LENGTH:
        return ChatResponse(
            response=f"Message too long (max {MAX_MESSAGE_LENGTH} characters).",
            session_id=request.session_id or "",
            blocks=None,
        )
    request_id_var.set(uuid.uuid4().hex[:8])
    t0 = __import__("time").time()
    sess = session_mgr.get_or_create(
        session_id=request.session_id,
        tenant_id=request.tenant_id,
        user_id=request.user_id,
        channel=request.channel,
    )

    from orchestrator import run_orchestrator
    result = await run_orchestrator(request.message, sess, _skill_registry)
    rich = formatter.format_rich_response(result, request.channel)

    _metrics.REQUESTS.labels(endpoint="/chat", status="200").inc()
    _metrics.REQUEST_DURATION.labels(endpoint="/chat").observe(__import__("time").time() - t0)

    return ChatResponse(
        response=rich["text"],
        session_id=sess.id,
        blocks=rich["blocks"],
    )


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """Streaming chat endpoint — emits SSE events between agent iterations."""
    if len(request.message) > MAX_MESSAGE_LENGTH:
        async def _err():
            yield f"data: {json.dumps({'type': 'text', 'content': f'Message too long (max {MAX_MESSAGE_LENGTH} characters).'})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'session_id': request.session_id or ''})}\n\n"
        return StreamingResponse(_err(), media_type="text/event-stream")
    request_id_var.set(uuid.uuid4().hex[:8])
    sess = session_mgr.get_or_create(
        session_id=request.session_id,
        tenant_id=request.tenant_id,
        user_id=request.user_id,
        channel=request.channel,
    )

    async def event_generator():
        from orchestrator import run_orchestrator_stream
        async for event in run_orchestrator_stream(request.message, sess, _skill_registry):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/confirm/{confirmation_id}", response_model=ConfirmResponse)
async def confirm(confirmation_id: str, request: Request):
    """Execute a pending critical tool after human confirmation."""
    from tools import _pending
    entry = _pending.get(confirmation_id)
    if entry is None:
        return ConfirmResponse(result={"error": "Confirmation not found or already executed"})

    sess = session_mgr.get(entry["session_id"])
    if sess is None:
        return ConfirmResponse(result={"error": "Session expired"})

    client_id = getattr(request.state, "client_id", "")
    result = await tools.confirm_tool(confirmation_id, sess, client_id=client_id)
    return ConfirmResponse(result=result)
