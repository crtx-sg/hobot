# Hobot — Agentic AI Clinician Assistant

Conversational clinical agent that unifies eight hospital systems behind a single chat API. Clinicians ask questions in natural language; Hobot queries EHR, vitals, labs, imaging, pharmacy, blood bank, ERP, and patient services, then returns a synthesized answer.

Built on a custom **nanobot gateway** (FastAPI) that orchestrates MCP tool servers, enforces clinical safety gates, extracts structured medical facts, and logs every action to an immutable audit trail.

---

## Prerequisites

| Dependency | Version | Notes |
|------------|---------|-------|
| Docker + Docker Compose | 24.0+ | Compose V2 (built-in `docker compose`) |
| NVIDIA Container Toolkit | latest | GPU passthrough for Ollama |
| NVIDIA GPU | 6+ GB VRAM | RTX 4050 or better |

No host Python install required — everything runs in containers.

---

## Quick Start

```bash
# 1. Clone and enter repo
git clone <repo-url> && cd hobot

# 2. (Optional) Set API keys for cloud providers
export ANTHROPIC_API_KEY=sk-ant-...   # only if using Anthropic provider

# 3. Start Ollama and pull a model (first run only)
docker compose up ollama -d
docker compose exec ollama ollama pull llama3.1:8b

# 4. Build and start everything
docker compose up -d --build

# 5. Check gateway health (all backends should report "ok")
curl http://localhost:3000/health

# 6. Send a chat message
curl -X POST http://localhost:3000/chat \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "Show vitals for P001",
    "user_id": "doc1",
    "channel": "webchat",
    "tenant_id": "T1"
  }'

# 7. Stream a chat (SSE)
curl -N -X POST http://localhost:3000/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "Show vitals for P001",
    "user_id": "doc1",
    "channel": "webchat",
    "tenant_id": "T1"
  }'
```

First `docker compose up --build` takes a few minutes to build all 18 images. Subsequent starts are fast.

---

## Architecture

```
                        ┌──────────────┐
                        │ LLM Provider │
                        │ (Ollama /    │
                        │  OpenAI-compat)│
                        └──────▲───────┘
                               │
┌──────────────────────────────┼──────────────────────────────────┐
│  Nanobot Gateway (:3000)     │                                  │
│  ┌────────────┐  ┌───────────┴──────┐  ┌────────────────────┐  │
│  │ /chat      │→ │  Agent Loop      │→ │  Tool Dispatch     │──┼──→ Synthetic Backends
│  │ /chat/stream│ │  (multi-provider │  │  (HTTP to backends)│  │
│  │ /health    │  │   + kw fallback) │  │  (param validation)│  │
│  │ /confirm   │  └──────────────────┘  └────────────────────┘  │
│  └────────────┘                                                 │
│  ┌────────────┐  ┌──────────────────┐  ┌────────────────────┐  │
│  │ Audit Log  │  │ Clinical Memory  │  │ PHI Redaction      │  │
│  │ (SQLite)   │  │ (fact extraction │  │ (non-PHI-safe      │  │
│  └────────────┘  │  + consolidation)│  │  providers)        │  │
│                  └──────────────────┘  └────────────────────┘  │
│  ┌────────────┐  ┌──────────────────┐                          │
│  │ Sessions   │  │ Response         │                          │
│  │ (JSONL     │  │ Formatter        │                          │
│  │  on disk)  │  │ (per-channel)    │                          │
│  └────────────┘  └──────────────────┘                          │
└─────────────────────────────────────────────────────────────────┘
```

**18 containers total:**

| Layer | Containers |
|-------|-----------|
| Gateway | `nanobot-gateway` |
| Model | `ollama` |
| MCP tool servers (8) | `mcp-ehr`, `mcp-monitoring`, `mcp-radiology`, `mcp-lis`, `mcp-pharmacy`, `mcp-bloodbank`, `mcp-erp`, `mcp-patient-services` |
| Synthetic backends (8) | `synthetic-ehr` (HAPI FHIR), `synthetic-monitoring`, `synthetic-radiology` (Orthanc), `synthetic-lis`, `synthetic-pharmacy`, `synthetic-bloodbank`, `synthetic-erp`, `synthetic-patient-services` |
| Utilities | `audit-db` (SQLite Web UI) |

---

## Exposed Ports

| Port | Service | Purpose |
|------|---------|---------|
| 3000 | nanobot-gateway | Chat API + health check |
| 8080 | synthetic-ehr (HAPI FHIR) | FHIR R4 server |
| 8042 | synthetic-radiology (Orthanc) | DICOM/DICOMweb viewer |
| 8081 | audit-db (sqlite-web) | Audit log browser |

---

## API Reference

### `POST /chat`

Send a message and get a response.

**Request:**

```json
{
  "message": "Show vitals for P001",
  "user_id": "doc1",
  "channel": "webchat",
  "tenant_id": "T1",
  "session_id": null
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `message` | string | yes | Natural language query |
| `user_id` | string | yes | Clinician identifier |
| `channel` | string | no | `webchat` (default), `telegram`, `slack` |
| `tenant_id` | string | no | Tenant for multi-hospital. Default: `default` |
| `session_id` | string | no | Omit to start new session; pass to continue |

**Response:**

```json
{
  "response": "**get_vitals** result:\n```json\n{...}\n```",
  "session_id": "a1b2c3d4-..."
}
```

Pass `session_id` back on subsequent requests to maintain conversation context and clinical memory. Sessions survive gateway restarts (persisted to JSONL on disk).

### `POST /chat/stream`

Streaming version of `/chat`. Same request body, returns Server-Sent Events (SSE).

```bash
curl -N -X POST http://localhost:3000/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"message":"Show vitals for P001","user_id":"doc1","channel":"webchat","tenant_id":"T1"}'
```

**SSE event types:**

| Event type | Fields | Description |
|------------|--------|-------------|
| `tool_call` | `tool`, `status` | Tool execution started |
| `tool_result` | `tool`, `data` | Tool returned results |
| `text` | `content` | Final synthesized response |
| `done` | `session_id` | Stream complete |

Each event is a `data:` line containing JSON. Full LLM response is buffered per iteration (no partial token streaming); SSE events are emitted between agent loop iterations.

### `POST /confirm/{confirmation_id}`

Execute a critical tool that was gated behind confirmation.

When a critical tool is invoked (e.g. `initiate_code_blue`, `order_blood_crossmatch`), the gateway returns a `confirmation_id` instead of executing. POST to this endpoint to authorize execution.

```bash
curl -X POST http://localhost:3000/confirm/a1b2c3d4-...
```

### `GET /health`

Returns per-backend health status.

```json
{
  "status": "ok",
  "service": "nanobot-gateway",
  "backends": {
    "synthetic-monitoring": "ok",
    "synthetic-ehr": "ok",
    "synthetic-lis": "ok",
    "synthetic-pharmacy": "ok",
    "synthetic-radiology": "ok",
    "synthetic-bloodbank": "ok",
    "synthetic-erp": "ok",
    "synthetic-patient-services": "ok"
  }
}
```

`status` is `ok` when all backends respond, `degraded` when any are unreachable.

---

## Configuration

All config files live in `config/` and are mounted into the gateway at `/app/config`.

### `config/config.json` — Providers + Model Routing

Defines LLM providers and which one to use by default.

```json
{
  "providers": {
    "ollama": {
      "baseUrl": "http://ollama:11434",
      "model": "llama3.1:8b",
      "phi_safe": true
    },
    "anthropic": {
      "baseUrl": "https://api.anthropic.com",
      "apiKey": "${ANTHROPIC_API_KEY}",
      "model": "claude-sonnet-4-20250514",
      "phi_safe": false
    }
  },
  "agents": {
    "defaults": {
      "model": "llama3.1:8b",
      "provider": "ollama"
    }
  }
}
```

| Provider field | Description |
|----------------|-------------|
| `baseUrl` | API base URL. Ollama uses `/api/chat`, others use `/v1/chat/completions` |
| `apiKey` | API key. Supports `${ENV_VAR}` expansion |
| `model` | Model name/ID sent to the provider |
| `phi_safe` | If `false`, PHI is redacted before sending messages to this provider |
| `timeout` | Request timeout in seconds (default: 60) |

The `agents.defaults.provider` selects the global default. If the default provider is unavailable at runtime, the agent falls back to keyword-based intent detection.

### `config/tools.json` — Tool Criticality + Parameter Schemas

Controls which tools require human confirmation and validates tool parameters.

```json
{
  "tools": {
    "order_lab": {
      "critical": true,
      "params": {
        "patient_id": {"type": "string", "required": true},
        "test_code": {"type": "string", "required": true},
        "priority": {"type": "string", "enum": ["routine", "stat", "urgent"]}
      }
    },
    "get_vitals": {
      "critical": false,
      "params": {
        "patient_id": {"type": "string", "required": true}
      }
    },
    "list_wards": { "critical": false }
  }
}
```

Parameter validation rules:

| Rule | Description |
|------|-------------|
| `type` | `"string"` or `"number"` — type check |
| `required` | If `true`, param must be present |
| `enum` | Allowed values list |
| `pattern` | Regex pattern (matched with `re.match`) |

Tools without a `params` key skip validation. Validation runs before criticality checks and tool dispatch.

### `config/channels.json` — Channel Capabilities

Controls how responses are formatted per output channel.

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
    }
  }
}
```

When `tables: false`, markdown tables are converted to plain text. When `max_msg_length` is set, responses are truncated.

---

## Environment Variables

The gateway reads these from docker-compose (all have defaults):

| Variable | Default | Description |
|----------|---------|-------------|
| `CONFIG_PATH` | `/app/config/config.json` | Provider + model config |
| `TOOLS_CONFIG` | `/app/config/tools.json` | Tool criticality + param schemas |
| `CHANNELS_CONFIG` | `/app/config/channels.json` | Channel formatting config |
| `AUDIT_DB` | `/data/audit/clinic.db` | SQLite audit database path |
| `SCHEMA_PATH` | `/app/schema/init.sql` | SQL schema for DB init |
| `SESSIONS_DIR` | `/data/sessions` | JSONL session persistence directory |
| `CONSOLIDATION_THRESHOLD` | `30` | Messages before memory consolidation triggers |
| `OLLAMA_HOST` | `http://ollama:11434` | Ollama API endpoint (legacy, used by keyword fallback) |
| `MONITORING_BASE` | `http://synthetic-monitoring:8000` | Vitals backend |
| `EHR_BASE` | `http://synthetic-ehr:8080` | EHR/FHIR backend |
| `LIS_BASE` | `http://synthetic-lis:8000` | Lab backend |
| `PHARMACY_BASE` | `http://synthetic-pharmacy:8000` | Pharmacy backend |
| `RADIOLOGY_BASE` | `http://synthetic-radiology:8042` | Radiology backend |
| `BLOODBANK_BASE` | `http://synthetic-bloodbank:8000` | Blood bank backend |
| `ERP_BASE` | `http://synthetic-erp:8000` | ERP/inventory backend |
| `PATIENT_SERVICES_BASE` | `http://synthetic-patient-services:8000` | Patient services backend |
| `ANTHROPIC_API_KEY` | (none) | Required if using Anthropic provider |

To point at real hospital systems, change the `*_BASE` variables to their production URLs.

---

## Available Tools

The gateway exposes 30+ tools across 8 clinical domains. Each tool maps to a direct HTTP call to the corresponding synthetic backend.

| Domain | Tools | Backend |
|--------|-------|---------|
| **Monitoring** | `get_vitals`, `get_vitals_history`, `list_wards`, `list_doctors`, `get_ward_patients`, `get_doctor_patients`, `get_patient_events`, `get_event_vitals`, `get_event_ecg`, `initiate_code_blue` | synthetic-monitoring |
| **EHR** | `get_patient`, `get_medications`, `get_allergies`, `get_orders`, `write_order` | synthetic-ehr (HAPI FHIR) |
| **Radiology** | `get_studies`, `get_report`, `get_latest_study` | synthetic-radiology (Orthanc) |
| **LIS** | `get_lab_results`, `get_lab_order`, `order_lab`, `get_order_status` | synthetic-lis |
| **Pharmacy** | `check_drug_interactions`, `dispense_medication` | synthetic-pharmacy |
| **Blood Bank** | `get_blood_availability`, `order_blood_crossmatch`, `get_crossmatch_status` | synthetic-bloodbank |
| **ERP** | `get_inventory`, `get_equipment_status`, `place_supply_order` | synthetic-erp |
| **Patient Services** | `request_housekeeping`, `order_diet`, `request_ambulance`, `get_request_status` | synthetic-patient-services |
| **Gateway** | `escalate` | nanobot (internal) |

---

## Key Features

### Multi-Provider LLM Routing

The gateway supports multiple LLM backends through a provider abstraction layer (`providers.py`). Two provider types are built in:

- **OllamaProvider** — local GPU inference via Ollama `/api/chat`
- **OpenAICompatibleProvider** — any `/v1/chat/completions` API (OpenAI, Anthropic-compatible, vLLM, etc.)

Provider selection is global (configured in `config.json`). At runtime, the gateway checks provider health; if the default provider is down, the agent falls back to keyword-based intent detection.

### Session Persistence

Sessions are persisted as JSONL files at `{SESSIONS_DIR}/{tenant_id}/{session_id}.jsonl`. Each file contains:
- Line 0: session metadata (user, tenant, consolidation state, active patients)
- Lines 1+: message records and consolidation events

Sessions survive gateway restarts. On startup, sessions are loaded from disk on first access. The in-memory cache avoids repeated disk reads during a conversation.

### Memory Consolidation

When a session accumulates more than `CONSOLIDATION_THRESHOLD` messages (default: 30), the agent summarizes older messages into a clinical summary using the LLM. The summary is prepended to the context window as a system message. The 10 most recent messages are always kept verbatim.

Consolidation preserves: patient IDs, diagnoses, key vitals, medications, pending actions, and clinical decisions. If the LLM provider is unavailable, the existing summary is kept and the pointer advances.

### Streaming (SSE)

The `/chat/stream` endpoint emits Server-Sent Events between agent loop iterations. Each iteration buffers the full LLM response (no partial token streaming). Events let clients show real-time progress:
1. Tool call started → tool result returned → next iteration
2. Final text response → done

### Tool Parameter Validation

Tool parameters are validated against schemas defined in `config/tools.json` before dispatch. Validation checks `required`, `type`, `enum`, and `pattern` constraints. Invalid parameters return an error immediately without hitting the backend.

### PHI Redaction

When routing to a non-PHI-safe provider (e.g. cloud APIs), the gateway redacts PHI from all messages before sending to the LLM:
- Patient IDs (`P001`, `UHID12345`)
- MRN identifiers (`MRN00123`)
- Dates of birth
- Phone numbers

Tokens are restored in the LLM response. Ollama (local) is marked `phi_safe: true` and skips redaction. Audit log summaries are also redacted before storage.

### Keyword Fallback

When no LLM provider is available, the agent uses regex-based intent detection to map messages to tools directly. Examples:
- "vitals for P001" → `get_vitals(patient_id="P001")`
- "list wards" → `list_wards()`
- "lab results P001" → `get_lab_results(patient_id="P001")`
- "blood availability" → `get_blood_availability()`

---

## Clinical Memory

Every tool call result is automatically parsed into structured **clinical facts** and stored in SQLite. Facts are never summarized or discarded.

Supported fact types: `vitals`, `medication`, `allergy`, `lab_result`, `lab_order`, `demographics`, `order`, `imaging_study`, `radiology_report`, `blood_inventory`, `crossmatch`.

Facts are injected into the LLM context for active patients, so the agent has full clinical history without re-querying backends.

---

## Audit Trail

Every action is logged to `schema/init.sql`-defined tables:

- **`audit_log`** — immutable record of every tool call, chat response, and confirmation. Parameters are SHA-256 hashed (raw PHI never stored in audit).
- **`escalations`** — tracks human escalation requests and resolutions.
- **`clinical_facts`** — structured medical data extracted from tool results.

Browse the audit database at `http://localhost:8081` (sqlite-web UI).

---

## Project Structure

```
hobot/
├── config/
│   ├── config.json          # Provider + model routing config
│   ├── tools.json           # Tool criticality + parameter schemas
│   └── channels.json        # Channel formatting capabilities
├── schema/
│   └── init.sql             # Audit DB schema (SQLite)
├── nanobot/                  # Gateway (FastAPI, port 3000)
│   ├── main.py              # App + endpoints (/chat, /chat/stream, /health, /confirm)
│   ├── agent.py             # Agent loop (multi-provider + consolidation + streaming)
│   ├── providers.py         # LLM provider abstraction (Ollama, OpenAI-compatible)
│   ├── tools.py             # Tool registry + param validation + HTTP dispatch + critical gate
│   ├── audit.py             # SQLite audit logging
│   ├── clinical_memory.py   # Fact extraction + storage
│   ├── session.py           # JSONL-backed session persistence
│   ├── formatter.py         # Channel-aware response formatting
│   ├── phi.py               # PHI redaction/restoration
│   ├── Dockerfile
│   └── requirements.txt
├── mcp-*/                    # MCP tool servers (8 services)
│   ├── server.py            # FastMCP stdio server + health endpoint
│   ├── Dockerfile
│   └── requirements.txt
├── synthetic-*/              # Synthetic backends (8 services)
│   ├── main.py              # FastAPI REST API with in-memory data
│   ├── Dockerfile
│   └── requirements.txt
└── docker-compose.yml        # Full stack orchestration
```

---

## Setup & Operations

### First-Time Setup

```bash
# 1. Clone
git clone <repo-url> && cd hobot

# 2. (Optional) Configure cloud provider keys
export ANTHROPIC_API_KEY=sk-ant-...

# 3. Start Ollama and pull the model
docker compose up ollama -d
docker compose exec ollama ollama pull llama3.1:8b

# 4. Build and start the full stack
docker compose up -d --build
# First build takes a few minutes (18 images). Subsequent starts are fast.

# 5. Verify
curl http://localhost:3000/health
```

### Start

```bash
# Start all services (detached)
docker compose up -d

# Start just the gateway (if backends already running)
docker compose up -d nanobot-gateway
```

### Stop

```bash
# Stop all services (volumes preserved — sessions, audit, models retained)
docker compose down

# Stop and delete ALL data (sessions, audit DB, Ollama models)
docker compose down -v
```

### Restart Gateway Only

```bash
docker compose restart nanobot-gateway
```

Sessions persist across restarts (JSONL on disk). Reuse the same `session_id` to continue a conversation.

### Rebuild After Code Changes

```bash
# Rebuild + restart gateway only
docker compose build nanobot-gateway && docker compose up -d nanobot-gateway

# Rebuild a specific service
docker compose build mcp-bloodbank && docker compose up -d mcp-bloodbank

# Rebuild everything
docker compose up -d --build
```

### Logs

```bash
# Gateway logs (follow)
docker compose logs -f nanobot-gateway

# All logs
docker compose logs -f

# Specific service
docker compose logs -f synthetic-monitoring
```

### Change the Default LLM Model

Edit `config/config.json`:

```json
{
  "providers": {
    "ollama": {
      "baseUrl": "http://ollama:11434",
      "model": "llama3.2-vision",
      "phi_safe": true
    }
  },
  "agents": {
    "defaults": {
      "provider": "ollama"
    }
  }
}
```

Then pull the model and restart:

```bash
docker compose exec ollama ollama pull llama3.2-vision
docker compose restart nanobot-gateway
```

### Switch to a Cloud Provider

Set the API key and change the default provider in `config/config.json`:

```json
{
  "agents": {
    "defaults": {
      "provider": "anthropic"
    }
  }
}
```

```bash
export ANTHROPIC_API_KEY=sk-ant-...
docker compose up -d nanobot-gateway
```

PHI redaction activates automatically for providers with `"phi_safe": false`.

### Verify Session Persistence

```bash
# Send a message, note the session_id
curl -s -X POST http://localhost:3000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"Show vitals for P001","user_id":"doc1","channel":"webchat","tenant_id":"T1"}' \
  | jq .session_id

# Restart the gateway
docker compose restart nanobot-gateway

# Continue the same session
curl -X POST http://localhost:3000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"What did I ask?","user_id":"doc1","channel":"webchat","tenant_id":"T1","session_id":"<id>"}'

# Inspect session files on disk
docker compose exec nanobot-gateway ls /data/sessions/T1/
```

### Inspect Audit Database

Open `http://localhost:8081` in a browser, or query directly:

```bash
docker compose exec audit-db sqlite3 /data/clinic.db "SELECT * FROM audit_log ORDER BY id DESC LIMIT 10;"
```

---

## Swapping Synthetic Backends for Real Systems

Each backend URL is configured via environment variable. To connect to a real hospital EHR:

```yaml
# docker-compose.yml
nanobot-gateway:
  environment:
    - EHR_BASE=https://real-ehr.hospital.local/fhir
```

Remove the corresponding `synthetic-*` service and its `mcp-*` dependency. The gateway calls backends directly, so MCP servers are only needed for external MCP clients.

---

## Volumes

| Volume | Mounted On | Purpose |
|--------|-----------|---------|
| `ollama-models` | ollama `/root/.ollama` | Persists downloaded LLM models |
| `audit-data` | nanobot-gateway `/data/audit`, audit-db `/data` | SQLite audit + clinical facts DB |
| `sessions-data` | nanobot-gateway `/data/sessions` | JSONL session files (per-tenant) |
| `hdf5-data` | synthetic-monitoring `/data/hdf5` | Persists HDF5 vitals data across restarts |

---

## License

TBD
