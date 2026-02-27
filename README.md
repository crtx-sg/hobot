# Hobot — Agentic AI Clinician Assistant

Message-driven clinical agent runtime built on the **nanobot framework**. Nanobot provides channels, message bus, sessions, agent loop, MCP tools, providers, cron, and sub-agents out of the box. Hobot adds clinical safety, PHI protection, structured medical memory, and domain-specific MCP tool servers on top.

---

## Table of Contents

- [Background](#background)
- [Requirements](#requirements)
- [Architecture Overview](#architecture-overview)
- [What Nanobot Provides](#what-nanobot-provides)
- [What Hobot Builds On Top](#what-hobot-builds-on-top)
  - [Clinical Safety Middleware](#clinical-safety-middleware)
  - [PHI-Aware Provider Routing](#phi-aware-provider-routing)
  - [Response Formatter](#response-formatter)
  - [MCP Tool Servers](#mcp-tool-servers)
  - [Human Escalation Tool](#human-escalation-tool)
  - [Audit Database](#audit-database)
  - [Clinical Memory](#clinical-memory)
- [Docker Compose Architecture](#docker-compose-architecture)
- [Configuration](#configuration)
- [Workflows](#workflows)
- [Observability](#observability)
- [Multi-Tenancy Model](#multi-tenancy-model)

---

## Background

Clinical environments need AI assistants that can:

1. **Query patient data** — vitals, labs, medications, imaging — across EHR, monitoring, and radiology systems.
2. **Enforce safety** — critical actions (Code Blue, EHR writes) require explicit clinician confirmation before execution.
3. **Protect PHI** — patient data never leaves the local network unless the provider has a BAA in place.
4. **Maintain clinical memory** — structured medical facts must never be silently lost to context window summarization.
5. **Work across channels** — clinicians use Telegram, Slack, WebChat, and others. The agent must adapt output format per channel.
6. **Degrade gracefully** — when backends are down, serve cached data with clear staleness warnings.
7. **Audit everything** — every tool call, confirmation, escalation, and LLM request is logged immutably.

Hobot achieves this by extending nanobot rather than wrapping it. Nanobot handles messaging infrastructure; Hobot adds clinical domain logic.

---

## Requirements

### Hardware

| Component | Minimum |
|-----------|---------|
| GPU | NVIDIA RTX 4050 (~6 GB VRAM) |
| RAM | 16 GB |
| Storage | 50 GB (models + synthetic data) |

### Software

| Dependency | Version |
|------------|---------|
| Docker + Docker Compose | 24.0+ |
| NVIDIA Container Toolkit | For GPU passthrough to Ollama |
| Python | 3.11+ (for MCP tool servers) |

### Models

| Model | Use Case | VRAM |
|-------|----------|------|
| `llama3.2-vision` (11B) | Primary — general reasoning + vision | ~6 GB |
| MedGemma (when available) | Clinical domain fine-tune from Google | TBD |
| `llama3.1:70b` | **Not viable** on RTX 4050 — skipped | ~40 GB |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Docker Compose                           │
│                                                                 │
│  ┌──────────────┐   ┌─────────┐   ┌──────────────────────────┐ │
│  │  Channels     │   │  Ollama │   │  MCP Tool Servers        │ │
│  │  (Telegram,   │   │  (GPU)  │   │  ┌────────┐ ┌────────┐  │ │
│  │   Slack,      │◄─►│         │   │  │mcp-ehr │ │mcp-mon │  │ │
│  │   WebChat...) │   └────▲────┘   │  └───▲────┘ └───▲────┘  │ │
│  └──────┬───────┘        │        │  ┌───┴────┐      │       │ │
│         │                │        │  │mcp-rad │      │       │ │
│         ▼                │        │  └───▲────┘      │       │ │
│  ┌──────────────────────────┐     │      │           │       │ │
│  │     Nanobot Gateway      │     └──────┼───────────┼───────┘ │
│  │  ┌────────────────────┐  │            │           │         │
│  │  │ Clinical Safety    │  │◄───────────┴───────────┘         │
│  │  │ Middleware         │  │                                   │
│  │  │  • Pre-exec guard  │  │     ┌──────────────────────────┐ │
│  │  │  • Post-exec audit │  │     │  Synthetic Backends      │ │
│  │  │  • PHI redaction   │  │     │  ┌──────┐ ┌─────┐       │ │
│  │  └────────────────────┘  │     │  │ HAPI │ │Orth-│       │ │
│  │  ┌────────────────────┐  │     │  │ FHIR │ │anc  │       │ │
│  │  │ Response Formatter │  │     │  └──────┘ └─────┘       │ │
│  │  └────────────────────┘  │     │  ┌──────────────┐       │ │
│  │  ┌────────────────────┐  │     │  │ Synth Vitals │       │ │
│  │  │ Clinical Memory    │  │     │  └──────────────┘       │ │
│  │  └────────────────────┘  │     └──────────────────────────┘ │
│  └──────────┬───────────────┘                                   │
│             │                                                   │
│             ▼                                                   │
│  ┌──────────────────┐                                           │
│  │  SQLite Audit DB  │  (+ SQLite Web UI on :8081)              │
│  │  • audit_log      │                                          │
│  │  • escalations    │                                          │
│  │  • clinical_facts │                                          │
│  └──────────────────┘                                           │
└─────────────────────────────────────────────────────────────────┘
```

**8 containers total.** Swap synthetic backends for real ones in production by changing env vars.

---

## What Nanobot Provides

These are **not custom-built** — they come from the nanobot framework:

| Concern | Nanobot Module |
|---------|---------------|
| Channel adapters | Telegram, Discord, Slack, Matrix, Email, WhatsApp, QQ, DingTalk, Feishu |
| Message bus | `bus/queue.py` — async inbound/outbound queues |
| Session management | `session/manager.py` — JSONL append-only, in-memory cache |
| Agent loop | `agent/loop.py` — up to 40 iterations, tool calls, streaming |
| Memory | `agent/memory.py` — consolidation, configurable window |
| MCP tools | `agent/tools/mcp.py` — native MCP integration |
| Provider abstraction | `providers/` — OpenAI-compatible, LiteLLM, custom |
| Cron/scheduling | `cron/service.py` |
| Sub-agents | `agent/subagent.py` |
| Context assembly | `agent/context.py` — ContextBuilder |
| Heartbeat | `heartbeat/service.py` |
| Channel permissions | Allowlist-based sender validation |

**Key decision:** Use `nanobot gateway` directly. Extend, don't wrap.

---

## What Hobot Builds On Top

### Clinical Safety Middleware

Hooks into nanobot's `ToolRegistry` at three points:

**Pre-execution guard:**
- Tools classified as `critical` (EHR writes, Code Blue, escalation) require explicit clinician confirmation via the channel before execution.
- Non-critical tools execute immediately.

**Post-execution audit:**
- Every tool call writes an immutable record to SQLite: timestamp, user, tool, params hash, result summary, confirmation ID, template version.

**Pre-LLM PHI redaction:**
- If the selected provider is not `phi_safe`, PHI patterns (UHID, patient names, DOB) are redacted before sending to the model.
- Original values re-injected into the response after inference.

### PHI-Aware Provider Routing

| Provider | Location | PHI Safe | Use Case |
|----------|----------|----------|----------|
| Ollama | Local | Yes | Default for all clinical tasks |
| Cloud (Anthropic, OpenAI) | Remote | No | Opt-in, non-PHI tasks only, or with BAA |

- Default model: `llama3.2-vision` via Ollama (11B, fits RTX 4050).
- Cloud providers tagged `phi_safe: false`. PHI redaction middleware activates automatically when routing to them.

### Response Formatter

Sits in the outbound path between agent and channel `send()`:

- Each channel declares capabilities: `{ rich_text, buttons, tables, images, max_msg_length }`.
- Formatter downgrades output to match:
  - Tables → plain text lists (for text-only channels)
  - Images → links (for channels without inline image support)
  - Long messages → paginated
- Critical alerts (Code Blue) get channel-appropriate formatting:
  - Text-only: **BOLD/CAPS**
  - WebChat: red banner with high-priority styling

### MCP Tool Servers

Three microservices, each running in its own Docker container:

| Service | Protocol | Backend | Description |
|---------|----------|---------|-------------|
| `mcp-ehr` | FHIR R4 | HAPI FHIR (synthetic) → real EHR | Patient demographics, medications, allergies, labs, orders. Includes patient consent check. |
| `mcp-monitoring` | Custom | Synthetic → real vitals feeds | Real-time and historical vital signs (HR, BP, SpO2, temp). |
| `mcp-radiology` | DICOM/DICOMweb | Orthanc (synthetic) → real PACS | Imaging studies, reports, DICOM viewer URLs. |

Each MCP server:
- Has a health endpoint (`GET /health`).
- Supports `degraded_mode`: returns cached/stale data with a staleness indicator when the backend is unreachable.
- Tool result truncation: shows error if nanobot's 500-char limit is hit (increase limit in config).

### Human Escalation Tool

Custom MCP tool `escalate`:

1. Notifies designated on-call clinician via configured channel.
2. Pauses the agent loop, awaits human response.
3. Logs escalation in audit trail (both `audit_log` and `escalations` tables).

**Triggered by:**
- Agent uncertainty (low-confidence clinical decision)
- Critical findings (abnormal imaging, dangerous vitals)
- Patient safety concerns (drug interactions, allergy alerts)

### Audit Database

**Phase 1:** SQLite — single file, zero config, ACID-compliant, WAL mode for concurrent reads.
**Phase 2:** Migrate to PostgreSQL when scaling to multi-node.

#### Schema

```sql
-- Immutable log of every action
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,          -- ISO 8601
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    action TEXT NOT NULL,             -- tool_call, confirmation, escalation, llm_request
    tool_name TEXT,
    params_hash TEXT,                 -- SHA256 of params (not raw PHI)
    result_summary TEXT,
    confirmation_id TEXT,
    template_version TEXT,
    provider TEXT,
    model TEXT,
    latency_ms INTEGER
);

-- Escalation tracking
CREATE TABLE escalations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    audit_log_id INTEGER REFERENCES audit_log(id),
    escalated_to TEXT NOT NULL,
    reason TEXT,
    resolved_at TEXT,
    resolved_by TEXT,
    resolution TEXT
);
```

#### Backup Strategy

- SQLite WAL mode enabled for concurrent read access.
- Periodic backup to object storage (S3/MinIO).
- Backups are append-only — never delete old backups within retention window.

### Clinical Memory

**Problem:** Nanobot's memory consolidation summarizes old context (lossy). Clinical data must not be silently lost.

**Solution — Dual-layer memory:**

| Layer | Store | Lossy? | Purpose |
|-------|-------|--------|---------|
| Conversational | Nanobot default (JSONL + consolidation) | Yes | Chat continuity. Summarization is acceptable. |
| Clinical | SQLite `clinical_facts` table | No | Structured medical facts. Never summarized, never discarded. |

#### Schema

```sql
CREATE TABLE clinical_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    fact_type TEXT NOT NULL,          -- vitals, diagnosis, medication, lab_result, allergy, note
    fact_data TEXT NOT NULL,          -- JSON
    source_tool TEXT,                 -- which MCP tool provided this
    recorded_at TEXT NOT NULL,
    expires_at TEXT                   -- NULL = permanent
);
```

#### How It Works

1. **Post-tool-execution hook** extracts structured clinical facts from tool results.
2. Facts stored in `clinical_facts` table immediately.
3. On session resume, agent context includes: summarized conversation + **full clinical facts** for active patients.
4. Clinician can ask "show all facts for patient X" — reads from DB, not from lossy memory.

#### Edge Cases

- **Long multi-patient sessions:** facts extracted per-patient before consolidation runs.
- **Ambiguous facts:** marked `confidence: low`, flagged for clinician review.
- **Consolidation:** only conversational context is summarized. Clinical facts are never deleted by consolidation.

---

## Docker Compose Architecture

```yaml
services:
  # --- Core ---
  nanobot-gateway:
    build: ./nanobot
    depends_on: [ollama, mcp-ehr, mcp-monitoring, mcp-radiology, audit-db]
    volumes:
      - ./config:/root/.nanobot
      - audit-data:/data/audit
    ports:
      - "3000:3000"    # WebChat
    environment:
      - OLLAMA_HOST=http://ollama:11434
      - AUDIT_DB=/data/audit/clinic.db

  # --- Model ---
  ollama:
    image: ollama/ollama
    volumes:
      - ollama-models:/root/.ollama
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]

  # --- MCP Tool Servers ---
  mcp-ehr:
    build: ./mcp-ehr
    depends_on: [synthetic-ehr]
    environment:
      - FHIR_BASE=http://synthetic-ehr:8080/fhir

  mcp-monitoring:
    build: ./mcp-monitoring
    depends_on: [synthetic-monitoring]

  mcp-radiology:
    build: ./mcp-radiology
    depends_on: [synthetic-radiology]

  # --- Synthetic Backends (Phase 1) ---
  synthetic-ehr:
    image: hapiproject/hapi-fhir-jpaserver
    ports: ["8080:8080"]

  synthetic-monitoring:
    build: ./synthetic-monitoring

  synthetic-radiology:
    image: orthancteam/orthanc
    ports: ["8042:8042"]

  # --- Audit DB Web UI ---
  audit-db:
    image: kevinmichaelchen/sqlite-web
    volumes:
      - audit-data:/data
    ports: ["8081:8080"]   # SQLite web UI for debugging

volumes:
  ollama-models:
  audit-data:
```

**8 containers.** In production, swap synthetic backends for real systems by changing environment variables.

### Ports

| Port | Service |
|------|---------|
| 3000 | Nanobot WebChat |
| 8080 | HAPI FHIR (synthetic EHR) |
| 8042 | Orthanc (synthetic PACS) |
| 8081 | SQLite Web UI (audit DB) |

### Getting Started

```bash
# 1. Clone and enter repo
git clone <repo-url> && cd hobot

# 2. Pull Ollama model (first run only)
docker compose up ollama -d
docker compose exec ollama ollama pull llama3.2-vision

# 3. Start all services
docker compose up -d

# 4. Verify health
curl http://localhost:3000/health

# 5. Open WebChat
open http://localhost:3000
```

---

## Configuration

Nanobot configuration lives in `./config/` (mounted to `/root/.nanobot` in the gateway container).

### Provider Config (`config.json`)

```json
{
  "providers": {
    "ollama": {
      "baseUrl": "http://ollama:11434",
      "phi_safe": true
    },
    "anthropic": {
      "apiKey": "${ANTHROPIC_API_KEY}",
      "phi_safe": false
    }
  },
  "agents": {
    "defaults": {
      "model": "llama3.2-vision",
      "provider": "ollama"
    }
  }
}
```

### Tool Classification

Tools are classified in the MCP tool server manifests:

```json
{
  "tools": {
    "initiate_code_blue": { "critical": true },
    "write_order": { "critical": true },
    "get_vitals": { "critical": false },
    "get_patient": { "critical": false }
  }
}
```

Critical tools trigger the confirmation flow before execution.

### Channel Capabilities

```json
{
  "channels": {
    "telegram": {
      "rich_text": true,
      "buttons": false,
      "tables": false,
      "images": true,
      "max_msg_length": 4096
    },
    "webchat": {
      "rich_text": true,
      "buttons": true,
      "tables": true,
      "images": true,
      "max_msg_length": null
    },
    "slack": {
      "rich_text": true,
      "buttons": true,
      "tables": true,
      "images": true,
      "max_msg_length": 40000
    }
  }
}
```

### Rate Limiting

Per-clinician rate limits to prevent abuse:

```json
{
  "rate_limits": {
    "per_user": {
      "requests_per_minute": 20,
      "requests_per_hour": 200
    }
  }
}
```

---

## Workflows

### Workflow 1: Patient Status Query (Routine)

```
Clinician (Telegram): "Show vitals for patient UHID12345"
  → Agent detects intent: patient_vitals
  → Clinical memory lookup: clinical_facts WHERE patient_id = 'UHID12345'
  → Tool calls (parallel):
      mcp-monitoring.get_vitals(patient_id="UHID12345")
      mcp-ehr.get_patient(patient_id="UHID12345")
  → Post-exec: extract facts → store in clinical_facts + audit log
  → Agent synthesizes response (Ollama, local, PHI-safe)
  → Response Formatter: Telegram markdown
```

**Clinician sees:**

```
**UHID12345 — John Doe, 54M**
HR 78 | BP 120/80 | SpO2 98% | Temp 37.1°C
Meds: Metformin 500mg, Lisinopril 10mg
Allergies: Penicillin
```

**Latency:** ~2-4s (Ollama inference + 2 parallel MCP calls)

---

### Workflow 2: Code Blue (Safety-Critical)

```
Clinician (Slack): "Patient UHID99887 in cardiac arrest, initiate code blue"
  → Agent detects: code_blue (critical tool)
  → Pre-exec: CRITICAL → confirmation required
  → Agent: "⚠️ CONFIRM Code Blue for UHID99887 (Room 4B, Jane Smith, 67F)? Reply YES."
  → Clinician: "YES"
  → Confirmation logged → Code Blue initiated in EHR
  → Parallel: get_vitals + get_patient
  → Agent synthesizes emergency summary
  → Escalation tool: notify on-call cardiologist
```

**Latency target:** <5s to confirmation prompt. <3s after confirmation.

---

### Workflow 3: Radiology Review (Multimodal)

```
Clinician (WebChat): "Show latest chest X-ray for UHID55443"
  → mcp-radiology.get_latest_study(patient_id="UHID55443", modality="XR", body_part="chest")
  → mcp-radiology.get_report(study_id="STU-12345")
  → Agent synthesizes with llama3.2-vision (image analysis)
  → Response Formatter adapts per channel:
      WebChat: inline image + formatted report
      Telegram: image attachment + text summary
      Slack: image in thread + structured blocks
```

---

### Workflow 4: Degraded Mode (EHR Down)

```
Clinician: "Medications for UHID12345"
  → mcp-ehr.get_medications() → TIMEOUT → retry → TIMEOUT
  → degraded_mode activated
  → Fallback: clinical_facts WHERE patient_id='UHID12345' AND fact_type='medication'
  → Returns cached data from 2h ago
```

**Clinician sees:**

```
⚠️ EHR system currently unavailable.
Showing cached medications (as of 2h ago):
- Metformin 500mg BID
- Lisinopril 10mg QD
⚠️ This data may be outdated. Verify with pharmacy.
```

**Audit log:** `tool_failure` + `degraded_mode_used`

---

### Workflow 5: Multi-Patient Rounds Summary

```
Clinician (WebChat): "Morning summary for my patients on Ward 3B"
  → mcp-ehr.get_ward_patients(ward="3B", attending="DR-SMITH")
    → Returns [UHID001, UHID002, UHID003, UHID004]
  → Parallel fan-out (nanobot sub-agents):
      Per patient: get_vitals + get_latest_labs + get_overnight_notes
      4 patients × 3 tools = 12 MCP calls
  → Clinical facts extracted and stored per patient
  → Agent synthesizes ward summary
```

**Clinician sees:**

```
**Ward 3B Morning Rounds — 4 patients**

1. UHID001 — Bed 1, John Doe, 54M
   Overnight: stable. HR 72, BP 118/76
   Labs: K+ 4.2 (normal), Cr 1.1 (stable)
   Plan: continue current meds, discharge eval today

2. UHID002 — Bed 3, Mary Jones, 71F
   ⚠️ Overnight: fever spike 38.9°C at 02:00
   Labs: WBC 14.2 (↑), CRP 45 (↑)
   Recommend: blood cultures, consider ABx change
   ...
```

---

## Observability

### Structured Logging

All components emit structured JSON logs. Key fields: `timestamp`, `service`, `level`, `message`, `tenant_id`, `session_id`.

### Key Metrics

Emitted to logs, scrapeable by Prometheus:

| Metric | Description |
|--------|-------------|
| `agent.response_latency_ms` | p50, p95 response time |
| `tool.call_count` | By tool name |
| `tool.failure_rate` | By tool name |
| `provider.fallback_count` | Cloud fallback events |
| `escalation.count` | Human escalation events |

### Health Endpoint

```
GET /health → {
  "status": "healthy",
  "ollama": "ok",
  "mcp_ehr": "ok",
  "mcp_monitoring": "ok",
  "mcp_radiology": "ok",
  "audit_db": "ok"
}
```

Returns `degraded` if any MCP tool server is down (agent still functional via cached data).

### Docker Healthchecks

All containers define healthchecks. `docker compose ps` shows health status at a glance.

---

## Multi-Tenancy Model

Hobot supports multi-hospital deployment:

- **`tenant_id`** on all database tables (`audit_log`, `escalations`, `clinical_facts`).
- **Network isolation** per tenant via Docker networks — tenants cannot reach each other's MCP backends.
- **Provider config** per tenant — one hospital may use local Ollama only, another may opt into cloud with BAA.
- **Rate limits** scoped per tenant + per clinician.
- **Audit logs** queryable per tenant. No cross-tenant data leakage.

### Adding a New Tenant

1. Create tenant entry in config.
2. Provision Docker network for tenant's MCP backends.
3. Configure channel allowlists (which clinician IDs can interact).
4. Deploy tenant-specific MCP tool servers (or share with network isolation).

---

## License

TBD
