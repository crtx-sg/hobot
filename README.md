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

# 2. Start Ollama and pull a model (first run only)
docker compose up ollama -d
docker compose exec ollama ollama pull llama3.1:8b

# 3. Start everything
docker compose up -d --build

# 4. Check gateway health (all backends should report "ok")
curl http://localhost:3000/health

# 5. Send a chat message
curl -X POST http://localhost:3000/chat \
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
                        ┌─────────────┐
                        │   Ollama    │
                        │  (GPU/LLM)  │
                        └──────▲──────┘
                               │
┌──────────────────────────────┼──────────────────────────────────┐
│  Nanobot Gateway (:3000)     │                                  │
│  ┌────────────┐  ┌───────────┴──────┐  ┌────────────────────┐  │
│  │ /chat      │→ │  Agent Loop      │→ │  Tool Dispatch     │──┼──→ Synthetic Backends
│  │ /health    │  │  (Ollama + kw    │  │  (HTTP to backends)│  │
│  │ /confirm   │  │   fallback)      │  └────────────────────┘  │
│  └────────────┘  └──────────────────┘                           │
│  ┌────────────┐  ┌──────────────────┐  ┌────────────────────┐  │
│  │ Audit Log  │  │ Clinical Memory  │  │ Response Formatter │  │
│  │ (SQLite)   │  │ (fact extraction)│  │ (per-channel)      │  │
│  └────────────┘  └──────────────────┘  └────────────────────┘  │
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

Pass `session_id` back on subsequent requests to maintain conversation context and clinical memory.

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

### `config/tools.json` — Tool Criticality

Controls which tools require human confirmation before execution.

```json
{
  "tools": {
    "initiate_code_blue": { "critical": true },
    "write_order": { "critical": true },
    "order_blood_crossmatch": { "critical": true },
    "dispense_medication": { "critical": true },
    "request_ambulance": { "critical": true },
    "order_lab": { "critical": true },
    "get_vitals": { "critical": false },
    "get_patient": { "critical": false }
  }
}
```

Critical tools return a `confirmation_id` on first call. The clinician must POST `/confirm/{id}` to execute.

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

When `tables: false`, markdown tables are converted to plain text. When `max_msg_length` is set, responses are truncated.

### `config/config.json` — Provider Config

```json
{
  "providers": {
    "ollama": {
      "baseUrl": "http://ollama:11434",
      "phi_safe": true
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

---

## Environment Variables

The gateway reads these from docker-compose (all have defaults):

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_HOST` | `http://ollama:11434` | Ollama API endpoint |
| `OLLAMA_MODEL` | `llama3.1:8b` | Model for agent reasoning |
| `AUDIT_DB` | `/data/audit/clinic.db` | SQLite audit database path |
| `SCHEMA_PATH` | `/app/schema/init.sql` | SQL schema for DB init |
| `TOOLS_CONFIG` | `/app/config/tools.json` | Tool criticality config |
| `CHANNELS_CONFIG` | `/app/config/channels.json` | Channel formatting config |
| `MONITORING_BASE` | `http://synthetic-monitoring:8000` | Vitals backend |
| `EHR_BASE` | `http://synthetic-ehr:8080` | EHR/FHIR backend |
| `LIS_BASE` | `http://synthetic-lis:8000` | Lab backend |
| `PHARMACY_BASE` | `http://synthetic-pharmacy:8000` | Pharmacy backend |
| `RADIOLOGY_BASE` | `http://synthetic-radiology:8042` | Radiology backend |
| `BLOODBANK_BASE` | `http://synthetic-bloodbank:8000` | Blood bank backend |
| `ERP_BASE` | `http://synthetic-erp:8000` | ERP/inventory backend |
| `PATIENT_SERVICES_BASE` | `http://synthetic-patient-services:8000` | Patient services backend |

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

## Agent Modes

The gateway agent operates in two modes:

**Ollama mode (primary):** When Ollama is reachable, the agent sends conversation context + tool definitions to the LLM. The LLM selects tools and synthesizes natural language responses from results. Supports multi-step reasoning (up to 10 iterations per request).

**Keyword fallback:** When Ollama is unavailable, the agent uses regex-based intent detection to map messages to tools. Tool results are returned as formatted JSON. Examples:
- "vitals for P001" → `get_vitals(patient_id="P001")`
- "list wards" → `list_wards()`
- "lab results P001" → `get_lab_results(patient_id="P001")`
- "blood availability" → `get_blood_availability()`

Ollama health is checked at startup and re-checked on failure.

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

## PHI Protection

When routing to a non-PHI-safe provider, the gateway activates regex-based PHI redaction:
- Patient IDs (P001, UHID12345)
- Dates of birth
- Phone numbers

are replaced with tokens before sending to the LLM, then restored in the response. Ollama (local) is marked `phi_safe: true` and skips redaction.

---

## Project Structure

```
hobot/
├── config/
│   ├── config.json          # Provider + model config
│   ├── tools.json           # Tool criticality flags
│   └── channels.json        # Channel formatting capabilities
├── schema/
│   └── init.sql             # Audit DB schema (SQLite)
├── nanobot/                  # Gateway (FastAPI, port 3000)
│   ├── main.py              # App + endpoints (/chat, /health, /confirm)
│   ├── agent.py             # Agent loop (Ollama + keyword fallback)
│   ├── tools.py             # Tool registry + HTTP dispatch + critical gate
│   ├── audit.py             # SQLite audit logging
│   ├── clinical_memory.py   # Fact extraction + storage
│   ├── session.py           # In-memory session manager
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

## Operations

### Start / Stop

```bash
# Start everything
docker compose up -d --build

# Stop everything (data persisted in volumes)
docker compose down

# Stop and delete all data
docker compose down -v
```

### Logs

```bash
# Gateway logs
docker compose logs -f nanobot-gateway

# All logs
docker compose logs -f

# Specific service
docker compose logs -f synthetic-monitoring
```

### Rebuild a single service

```bash
docker compose build mcp-bloodbank
docker compose up -d mcp-bloodbank
```

### Change the Ollama model

```bash
docker compose exec ollama ollama pull llama3.2-vision
# Then set OLLAMA_MODEL=llama3.2-vision in docker-compose.yml and restart:
docker compose up -d nanobot-gateway
```

### Inspect audit database

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
| `hdf5-data` | synthetic-monitoring `/data/hdf5` | Persists HDF5 vitals data across restarts |

---

## License

TBD
