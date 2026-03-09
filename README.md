# Hobot — Agentic AI Clinician Assistant

Conversational clinical agent that unifies eight hospital systems behind a single chat API. Clinicians ask questions in natural language; Hobot queries EHR, vitals, labs, imaging, pharmacy, blood bank, ERP, and patient services, then returns a synthesized answer.

Built on a custom **clinibot gateway** (FastAPI) that orchestrates MCP tool servers, enforces clinical safety gates, extracts structured medical facts, and logs every action to an immutable audit trail.

Supports **ward rounds reports** (severity-sorted with vitals, meds, scans), **bed-based queries** ("meds for patient in bed 5"), **appointment scheduling**, **timed reminders** with Telegram push notifications, and **specialized LLM analyzers** that provide domain-specific clinical interpretation of labs, ECGs, vitals, and medical images.

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

# 2. Set API keys (Anthropic is the default provider)
export ANTHROPIC_API_KEY=your-anthropic-api-key
# Optional: Gemini as fallback
export GEMINI_API_KEY=your-gemini-api-key

# 3. Build and start everything
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

# 6. Stream a chat (SSE)
curl -N -X POST http://localhost:3000/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "Show vitals for P001",
    "user_id": "doc1",
    "channel": "webchat",
    "tenant_id": "T1"
  }'
```

First `docker compose up --build` takes a few minutes to build all images. Subsequent starts are fast.

---

## Architecture

```
                        ┌──────────────┐
                        │ LLM Provider │
                        │ (Anthropic / │
                        │ Gemini /     │
                        │ Ollama / Any)│
                        └──────▲───────┘
                               │ native tool_calls
                               │ (Anthropic/Gemini/OpenAI)
                               │ or text JSON (Ollama)
┌──────────────────────────────┼──────────────────────────────────┐
│  Clinibot Gateway (:3000)     │                                  │
│  ┌────────────┐  ┌───────────┴──────┐  ┌────────────────────┐  │
│  │ /chat      │→ │  Agent Loop      │→ │  Tool Dispatch     │──┼──→ Synthetic Backends
│  │ /chat/stream│ │  (multi-provider │  │  (parallel via     │  │    (asyncio.gather)
│  │ /health    │  │   native tools + │  │   asyncio.gather)  │  │
│  │ /confirm   │  │   kw fallback)   │  │  (param validation)│  │
│  └────────────┘  └──────────────────┘  └────────────────────┘  │
│  ┌────────────┐  ┌──────────────────┐  ┌────────────────────┐  │
│  │ Audit Log  │  │ Clinical Memory  │  │ PHI Redaction      │  │
│  │ (SQLite)   │  │ (fact extraction │  │ (non-PHI-safe      │  │
│  └────────────┘  │  + consolidation)│  │  providers)        │  │
│                  └──────────────────┘  └────────────────────┘  │
│  ┌────────────┐  ┌──────────────────┐  ┌────────────────────┐  │
│  │ Sessions   │  │ Response         │  │ Analyzers          │  │
│  │ (JSONL     │  │ Formatter        │  │ (intercept + tool  │  │
│  │  on disk)  │  │ (per-channel)    │  │  domain LLMs)      │  │
│  └────────────┘  └──────────────────┘  └────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

**20 containers total:**

| Layer | Containers |
|-------|-----------|
| Gateway | `clinibot-gateway` |
| Model | `ollama` |
| MCP tool servers (8) | `mcp-ehr`, `mcp-monitoring`, `mcp-radiology`, `mcp-lis`, `mcp-pharmacy`, `mcp-bloodbank`, `mcp-erp`, `mcp-patient-services` |
| Synthetic backends (8) | `synthetic-ehr` (HAPI FHIR), `synthetic-monitoring`, `synthetic-radiology` (Orthanc), `synthetic-lis`, `synthetic-pharmacy`, `synthetic-bloodbank`, `synthetic-erp`, `synthetic-patient-services` |
| Channels | `telegram-bot` |
| Utilities | `audit-db` (SQLite Web UI) |

---

## Exposed Ports

| Port | Service | Purpose |
|------|---------|---------|
| 3000 | clinibot-gateway | Chat API + health check |
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
| `channel` | string | no | `webchat` (default), `telegram`, `slack`, `whatsapp` |
| `tenant_id` | string | no | Tenant for multi-hospital. Default: `default` |
| `session_id` | string | no | Omit to start new session; pass to continue |

**Response:**

```json
{
  "response": "Patient P001 vitals: HR 82 bpm, BP 120/80...",
  "session_id": "a1b2c3d4-...",
  "blocks": [
    {"type": "data_table", "title": "Vitals — P001", "tool": "get_vitals",
     "columns": ["Metric","Value","Unit"], "rows": [["HR","82","bpm"], ...]},
    {"type": "alert", "severity": "warning", "text": "Heart Rate abnormal: 94"},
    {"type": "actions", "buttons": [{"label":"View History","action":"get_vitals_history","params":{"patient_id":"P001"}}]}
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `response` | string | Always present. Plain text response (backward-compatible). |
| `session_id` | string | Session identifier for conversation continuity. |
| `blocks` | list or null | Structured UI blocks for rich rendering. `null` when no tool calls were made. Old clients ignore this field and use `response`. |

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
  "service": "clinibot-gateway",
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

Defines LLM providers and which one to use by default. The current default is **Anthropic Claude** (`claude-sonnet-4-20250514`).

```json
{
  "providers": {
    "gemini": {
      "baseUrl": "https://generativelanguage.googleapis.com/v1beta/openai",
      "apiKey": "${GEMINI_API_KEY}",
      "model": "gemini-2.5-flash",
      "phi_safe": false,
      "timeout": 30.0,
      "supports_vision": true
    },
    "ollama": {
      "baseUrl": "http://ollama:11434",
      "model": "qwen2.5:7b",
      "phi_safe": true
    },
    "anthropic": {
      "baseUrl": "https://api.anthropic.com",
      "apiKey": "${ANTHROPIC_API_KEY}",
      "model": "claude-sonnet-4-20250514",
      "phi_safe": false,
      "supports_vision": true
    }
  },
  "agents": {
    "defaults": {
      "model": "claude-sonnet-4-20250514",
      "provider": "anthropic"
    }
  },
  "analyzers": {
    "intercept": {
      "get_report": {
        "provider": "gemini",
        "prompt_template": "radiology_report_summary",
        "domain": "radiology"
      }
    },
    "tools": {
      "analyze_lab_results": {
        "provider": "gemini",
        "prompt_template": "lab_analysis",
        "domain": "pathology",
        "source_fact_types": ["lab_result"],
        "context_types": ["demographics", "medication", "allergy"]
      },
      "analyze_ecg": {
        "provider": "ollama",
        "prompt_template": "ecg_analysis",
        "domain": "cardiology",
        "preprocessor": "ecg_stub",
        "source_fact_types": ["ecg"],
        "context_types": ["demographics", "medication"]
      },
      "analyze_vitals": {
        "provider": "gemini",
        "prompt_template": "vitals_analysis",
        "domain": "critical_care",
        "source_fact_types": ["vitals", "vitals_trend"],
        "context_types": ["demographics", "medication", "allergy"]
      },
      "analyze_radiology_image": {
        "provider": "gemini",
        "prompt_template": "radiology_image_analysis",
        "domain": "radiology",
        "input_type": "image_wado",
        "context_types": ["demographics", "medication", "imaging_study"]
      }
    },
    "defaults": {
      "fallback": "passthrough",
      "timeout": 15
    }
  }
}
```

**Four provider types are supported:**

| Provider type | Detection | Endpoint | Native Tools |
|---------------|-----------|----------|--------------|
| **AnthropicProvider** | Name contains `anthropic` or URL contains `api.anthropic.com` | `{baseUrl}/v1/messages` (native Messages API) | Yes — parallel |
| **GeminiProvider** | Name contains `gemini` or URL contains `generativelanguage.googleapis.com` | `{baseUrl}/chat/completions` | Yes — parallel |
| **OllamaProvider** | Name contains `ollama` or URL contains `/api/` | `{baseUrl}/api/chat` | No — text JSON fallback |
| **OpenAICompatibleProvider** | Everything else (OpenAI, vLLM, etc.) | `{baseUrl}/v1/chat/completions` | Yes — parallel |

Providers that support native function calling send tool definitions in the request and receive structured `tool_calls` in the response. The agent dispatches all tool calls in parallel via `asyncio.gather`. Ollama falls back to text-based JSON parsing (single tool per iteration).

The **AnthropicProvider** uses the native Anthropic Messages API (`/v1/messages`) with `x-api-key` auth, `input_schema` tool format, and `tool_use`/`tool_result` content blocks — not the OpenAI compatibility layer.

| Provider field | Description |
|----------------|-------------|
| `baseUrl` | API base URL (path is appended automatically per provider type) |
| `apiKey` | API key. Supports `${ENV_VAR}` expansion |
| `model` | Model name/ID sent to the provider |
| `phi_safe` | If `false`, PHI is redacted before sending messages to this provider |
| `timeout` | Request timeout in seconds (default: 60) |
| `supports_vision` | If `true`, provider can process image inputs (used by `analyze_radiology_image`) |

The `agents.defaults.provider` selects the global default. If the default provider is unavailable at runtime, the agent tries all other configured providers in order before falling back to keyword-based intent detection (see [Provider Fallback Chain](#provider-fallback-chain)).

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

Controls how responses are formatted and which block types are supported per output channel.

```json
{
  "channels": {
    "telegram": {
      "rich_text": true,
      "buttons": true,
      "tables": false,
      "images": true,
      "max_msg_length": 4096,
      "parse_mode": "HTML",
      "supported_blocks": ["data_table", "key_value", "alert", "text", "actions", "confirmation", "image", "chart", "waveform"]
    },
    "webchat": {
      "rich_text": true,
      "buttons": true,
      "tables": true,
      "images": true,
      "max_msg_length": null,
      "supported_blocks": ["data_table", "key_value", "alert", "text", "actions", "confirmation", "image", "chart", "waveform"]
    },
    "whatsapp": {
      "rich_text": false,
      "buttons": false,
      "tables": false,
      "images": true,
      "max_msg_length": 4096,
      "supported_blocks": ["data_table", "key_value", "alert", "text", "image"]
    }
  }
}
```

| Field | Description |
|-------|-------------|
| `tables` | When `false`, markdown tables in `response` text are converted to plain key:value lines |
| `max_msg_length` | Truncates `response` text. `null` = no limit |
| `parse_mode` | Telegram-specific: `"HTML"` for rich rendering |
| `supported_blocks` | Block types this channel can render. Unsupported types are stripped from the `blocks` array before returning |

### `config/config.json` — Analyzers Section

The `analyzers` section configures domain-specific clinical interpretation. See [Specialized LLM Analyzers](#specialized-llm-analyzers) for the full feature description.

**Intercept config fields:**

| Field | Description |
|-------|-------------|
| `provider` | Which LLM provider to use for analysis |
| `prompt_template` | Key into `analyzer_prompts.py` template dict |
| `domain` | Clinical domain label (`"radiology"`, etc.) |

**Tool analyzer config fields:**

| Field | Description |
|-------|-------------|
| `provider` | Which LLM provider to use |
| `prompt_template` | Key into `analyzer_prompts.py` template dict |
| `domain` | Clinical domain label |
| `source_fact_types` | Fact types in clinical memory containing the raw data to analyze |
| `context_types` | Fact types to fetch as patient context (demographics, meds, etc.) |
| `preprocessor` | Optional preprocessor name (e.g. `"ecg_stub"`) |
| `input_type` | `"image_wado"` for image-based analyzers (fetches from Orthanc) |

**Defaults:**

| Field | Default | Description |
|-------|---------|-------------|
| `fallback` | `"passthrough"` | Behavior when no analyzer configured |
| `timeout` | `15` | Seconds before analyzer times out |

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
| `GEMINI_API_KEY` | (none) | Required if using Gemini provider (default) |
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
| `TELEGRAM_BOT_TOKEN` | (none) | Required for reminder push notifications to Telegram |

To point at real hospital systems, change the `*_BASE` variables to their production URLs.

---

## Available Tools

The gateway exposes 30+ tools across 8 clinical domains. Each tool maps to a direct HTTP call to the corresponding synthetic backend.

| Domain | Tools | Backend |
|--------|-------|---------|
| **Monitoring** | `get_vitals`, `get_vitals_history`, `get_vitals_trend`, `list_wards`, `list_doctors`, `get_ward_patients`, `get_doctor_patients`, `get_patient_events`, `get_event_vitals`, `get_event_ecg`, `get_latest_ecg`, `initiate_code_blue` | synthetic-monitoring |
| **EHR** | `get_patient`, `get_medications`, `get_allergies`, `get_orders`, `write_order` | synthetic-ehr (HAPI FHIR) |
| **Radiology** | `get_studies`, `get_report`, `get_latest_study` | synthetic-radiology (Orthanc) |
| **LIS** | `get_lab_results`, `get_lab_order`, `order_lab`, `get_order_status` | synthetic-lis |
| **Pharmacy** | `check_drug_interactions`, `dispense_medication` | synthetic-pharmacy |
| **Blood Bank** | `get_blood_availability`, `order_blood_crossmatch`, `get_crossmatch_status` | synthetic-bloodbank |
| **ERP** | `get_inventory`, `get_equipment_status`, `place_supply_order` | synthetic-erp |
| **Patient Services** | `request_housekeeping`, `order_diet`, `request_ambulance`, `get_request_status` | synthetic-patient-services |
| **Gateway** | `escalate`, `get_ward_rounds`, `resolve_bed`, `schedule_appointment`, `set_reminder` | clinibot (internal) |
| **Analyzers** | `analyze_lab_results`, `analyze_ecg`, `analyze_vitals`, `analyze_radiology_image` | clinibot (domain LLMs) |

---

## Key Features

### Multi-Provider LLM Routing

The gateway supports multiple LLM backends through a provider abstraction layer (`providers.py`). All providers return a unified `ChatResult` (content + optional `ToolCall` list). Four provider types are built in:

- **AnthropicProvider** — Anthropic Claude via the native Messages API (`/v1/messages`). Supports native function calling with `tool_use`/`tool_result` content blocks.
- **GeminiProvider** — Google Gemini via its OpenAI-compatible endpoint (`{baseUrl}/chat/completions`). Supports native function calling.
- **OllamaProvider** — local GPU inference via Ollama `/api/chat`. Text-based tool parsing (single tool per iteration).
- **OpenAICompatibleProvider** — any `/v1/chat/completions` API (OpenAI, vLLM, etc.). Supports native function calling.

Providers with native function calling receive structured tool definitions and can return multiple `tool_calls` in a single response. These are dispatched in parallel via `asyncio.gather`, significantly reducing latency for multi-tool queries.

Provider selection is configured in `config.json`. At runtime, the gateway checks provider health and uses a fallback chain (see below).

### Provider Fallback Chain

When the default provider is unavailable or rate-limited, the gateway automatically tries other configured providers before falling to keyword regex:

```
Default provider (e.g. Anthropic) → other providers in config order (e.g. Gemini → Ollama) → keyword regex
```

`get_healthy_provider()` handles this at startup and mid-request. If a provider fails after tool calls are already collected (e.g. 429 on the synthesis step), the agent marks it unhealthy and tries a fallback provider for synthesis — tool results are never discarded.

### Retry with Backoff

All providers share a `_request_with_retry()` helper that retries on transient HTTP errors (429, 502, 503, 529) with exponential backoff (1s, 3s). After retries are exhausted, the provider returns `None` and the fallback chain takes over.

### LLM Response Synthesis

Tool results are synthesized into human-readable responses using the LLM provider. When no provider is available, built-in text formatters produce structured summaries for common tools (vitals, labs, meds, ECG, blood availability, patient services, etc.). The LLM synthesis prompt instructs the model to use bullet points, flag abnormal values, and keep responses under 10 lines.

### Specialized LLM Analyzers

Raw clinical data (labs, ECGs, vitals, images) is fed to domain-specific LLM analyzers for context-aware interpretation. Two analyzer types exist:

**Intercept analyzers** auto-fire on tool results that are self-contained and need only summarization. Currently, `get_report` (radiology reports) is the only intercept — the radiologist-authored text is summarized and attached as an `_analysis` key on the tool result. The main LLM sees both the raw report and the summary. Intercept failures are silent — the raw data passes through unchanged.

**Tool analyzers** are registered as gateway tools (`analyze_lab_results`, `analyze_ecg`, `analyze_vitals`, `analyze_radiology_image`) that the main LLM dispatches after gathering patient context. This ensures clinical interpretation quality: the same lab values mean different things for a 72-year-old with CKD on ACE inhibitors vs. a healthy 25-year-old.

**Data flow:** Tool analyzers pull source data from clinical memory (stored by data-fetch tools in a prior turn) and patient context (demographics, medications, allergies). They build a domain prompt, call a specialized provider (Gemini for vision/text, Ollama for local/PHI-safe), and return a standardized `AnalyzerResult`.

**AnalyzerResult format:**

| Field | Description |
|-------|-------------|
| `status` | `"success"`, `"partial"`, or `"error"` |
| `status_reason` | Why partial/error: `"incomplete_context"`, `"limited_capability"`, `"timeout"`, `"no_source_data"` |
| `domain` | `"pathology"`, `"cardiology"`, `"critical_care"`, `"radiology"` |
| `interpretation` | Free-text clinical interpretation (primary output) |
| `findings` | Structured findings list (for UI rendering, fact storage) |
| `context_used` | Which fact types were available for context |
| `context_summary` | One-line patient summary used by analyzer |
| `analyzer_provider` | Which provider ran the analysis |
| `analyzer_model` | Model identifier |

**Status semantics:** `status` reflects two independent dimensions — context completeness and analyzer capability. Missing demographics/meds yields `"incomplete_context"`. The ECG stub preprocessor (no ML model yet) yields `"limited_capability"`. Both can combine. The main LLM is instructed to note limitations when status is `"partial"`.

**Analyzer tools:**

| Tool | Domain | Provider | Required Context | Source Data |
|------|--------|----------|-----------------|-------------|
| `analyze_lab_results` | Pathology | Gemini | demographics, meds, allergies | Lab results from clinical memory |
| `analyze_ecg` | Cardiology | Ollama | demographics, meds | ECG metadata from clinical memory (stub preprocessor strips waveforms) |
| `analyze_vitals` | Critical care | Gemini | demographics, meds, allergies | Vitals + trend data from clinical memory |
| `analyze_radiology_image` | Radiology | Gemini (vision) | demographics, meds, imaging study | Image fetched from Orthanc via WADO |

**Example flow (labs):**
```
User: "Interpret labs for bed 5"
  Turn 1: LLM → resolve_bed(5) → P005
          LLM → get_lab_results(P005), get_patient(P005), get_medications(P005)
          → all results stored as facts in clinical memory
  Turn 2: LLM → analyze_lab_results(patient_id="P005")
          → fetches lab data + patient context from clinical memory
          → builds domain prompt → Gemini → AnalyzerResult
  Turn 3: LLM synthesizes: "Lab analysis shows BUN elevated but expected for CKD..."
```

**Error handling:**
- Intercept timeout (15s) → log warning, return raw data unchanged
- Tool analyzer timeout (15s) → return `{status: "error", status_reason: "timeout"}`
- Provider unhealthy → intercept: skip. Tool: error result
- Source data not in memory → `{status: "error", status_reason: "no_source_data"}`
- Missing context → `{status: "partial", status_reason: "incomplete_context"}` (analysis still proceeds with available context)
- PHI: if analyzer provider is not `phi_safe`, context is redacted before sending and interpretation is restored after

**Preprocessors:** The ECG analyzer uses a pluggable preprocessor (`ecg_stub`) that strips waveform arrays and returns metadata only with capability `"limited"`. When a future ML preprocessor is configured (`ecg_ml`), the same prompt receives richer data and returns capability `"full"` — no prompt changes needed.

**Configuration:** Analyzers are configured in the `analyzers` section of `config/config.json`. See [Configuration](#configuration) for the full schema.

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

When no LLM provider is available (all providers unhealthy), the agent uses regex-based intent detection to map messages to tools directly. Examples:
- "vitals for P001" → `get_vitals(patient_id="P001")`
- "vitals trend for P001" → `get_vitals_trend(patient_id="P001")`
- "vitals trend over last 48 hours for P001" → `get_vitals_trend(patient_id="P001", hours=48)`
- "list wards" → `list_wards()`
- "lab results P001" → `get_lab_results(patient_id="P001")`
- "get latest ECG for P001" → `get_latest_ecg(patient_id="P001")`
- "ECG event EVT-001 for P001" → `get_event_ecg(event_id="EVT-001", patient_id="P001")`
- "blood availability" → `get_blood_availability()`
- "rounds report for ICU-A" → `get_ward_rounds(ward_id="ICU-A")`
- "meds for bed 5" → `resolve_bed` → `get_medications(patient_id="P005")`
- "vitals for patient in bed 3" → `resolve_bed` → `get_vitals(patient_id="P003")`
- "schedule appointment with Dr Patel for patient in bed 5" → `schedule_appointment(...)`
- "remind me in 2 hours to turn patient in bed 3" → `set_reminder(delay_minutes=120, ...)`

Keyword results are synthesized through the LLM when a provider is available (used when the primary provider fails mid-request but a fallback exists), or formatted using built-in text formatters as a last resort.

### Vitals Trend Analysis with EWS Scoring

The `get_vitals_trend` tool provides per-reading Early Warning Score (EWS) computation and statistical trend detection using linear regression (`scipy.stats.linregress`).

**Query:** `"vitals trend over last 24 hours for P003"`

**What it returns:**
- 24 hourly readings with EWS score computed per reading (simplified NEWS2)
- Trend classification: `deteriorating`, `improving`, or `stable`
- Statistical metrics: slope, r², p-value, recent slope (last 3 readings)
- Confidence level: `high` (r² > 0.5, p < 0.05) or `low`
- Clinical interpretation string

**Trend classification logic:**
- **Deteriorating**: positive slope > 0.1 with p < 0.05
- **Improving**: negative slope < -0.1 with p < 0.05
- **Stable**: everything else

**Rich blocks generated:**

| Block | Content |
|-------|---------|
| `data_table` | Time, HR, BP Sys, SpO2, Temp, EWS per reading |
| `chart` | EWS score line chart over time |
| `text` | Trend status, confidence, clinical interpretation |
| `alert` | Only if deteriorating — `critical` (high confidence) or `warning` (low confidence) |

**Clinical scenarios (synthetic data):**
- P003: deteriorating (HR 70→110, SpO2 98→92, temp 37→39)
- P005: improving (HR 105→75, SpO2 93→98)
- P001, P002, P004: random (stable)

### Ward Rounds Report

The `get_ward_rounds` tool produces a severity-sorted patient report for an entire ward. It performs multi-service fan-out: fetches vitals from monitoring, medications from EHR, and latest imaging from radiology — all merged into a single response per patient.

**Query:** `"rounds report for ICU-A"`

**What it returns per patient (sorted by NEWS score descending):**
- Patient ID, bed assignment, attending doctor
- NEWS score with latest vitals (HR, BP, SpO2, Temp)
- 4-hour vitals history window
- Active medications (first 3)
- Latest radiology scan description
- Alert block if NEWS >= 5

**Rich blocks generated:**

| Block | Content |
|-------|---------|
| `key_value` | One per patient: Bed, NEWS, HR, BP, SpO2, Temp, Doctor, Meds, Last Scan |
| `alert` | Per patient with NEWS >= 5 — "needs urgent review" |

**Bed assignments (static seed data):**

| Bed | Patient |
|-----|---------|
| BED1 | P001 |
| BED2 | P002 |
| BED3 | P003 |
| BED4 | P004 |
| BED5 | P005 |

### Bed-Based Queries

Clinicians can query by bed number instead of patient ID. The agent auto-resolves `bed_id` → `patient_id` before dispatching the actual tool.

**Examples:**
- "meds for patient in bed 5" → resolves BED5 → P005 → calls `get_medications`
- "show vitals for bed 3" → resolves BED3 → P003 → calls `get_vitals`

Resolution uses the `resolve_bed` gateway tool, which queries the monitoring service's `/bed/{bed_id}/patient` endpoint. In keyword mode, bed resolution is automatic and transparent. In LLM mode, the system prompt instructs the model to call `resolve_bed` first.

### Appointment Scheduling

The `schedule_appointment` tool creates in-memory appointments. Designed for quick scheduling from the chat interface.

**Query:** `"schedule appointment with Dr Patel for patient in bed 5"`

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `patient_id` | string | yes | Resolved from bed if needed |
| `doctor` | string | yes | Doctor name/ID |
| `datetime` | string | yes | Appointment time |
| `notes` | string | no | Additional notes |

**Returns:** `{appointment_id, patient_id, doctor, datetime, status:"scheduled"}`

Appointments are stored in-memory on the gateway. They survive within a gateway process lifetime but are cleared on restart.

### Timed Reminders

The `set_reminder` tool schedules a reminder that fires after a specified delay. For Telegram users, the reminder is pushed directly to their chat via the Telegram Bot API.

**Query:** `"remind me in 2 hours to turn patient in bed 3"`

**How it works:**
1. Gateway stores the reminder in-memory with a trigger timestamp
2. A background loop checks every 30 seconds for due reminders
3. When triggered, extracts `chat_id` from the session ID (`tg-{chat_id}`)
4. Sends a push message via `https://api.telegram.org/bot{token}/sendMessage`
5. Marks reminder as `"fired"`

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `message` | string | yes | Reminder text |
| `delay_minutes` | number | yes | Minutes until reminder fires |

Requires `TELEGRAM_BOT_TOKEN` to be set on the clinibot-gateway service for push delivery.

### Agent Orchestration

The gateway uses two execution paths — **LLM-driven** and **keyword fallback** — with shared tool dispatch and post-processing.

#### Multi-Step Tool Chaining (LLM Path)

When an LLM provider is available, the agent runs an iterative loop (up to 10 iterations):

1. Send conversation + system prompt + tool definitions to LLM
2. LLM returns either tool call(s) or a final text response
3. If tool calls: dispatch **all in parallel** via `asyncio.gather`, append results to context, loop back to step 1
4. If text: return to user

**Native tool calling** (OpenAI/Gemini): Tool definitions are sent as structured `tools` in the API request. The LLM returns typed `tool_calls` with IDs. Multiple tools in a single response are dispatched concurrently. Results are fed back as `role="tool"` messages keyed by `tool_call_id`.

**Text fallback** (Ollama): Tools are described in the system prompt. The LLM emits a JSON block `{"tool":"...", "params":{...}}`. One tool per iteration. Results are fed back as `role="user"` messages.

This allows multi-step reasoning. For example, with native tools, "compare vitals and meds for P003" triggers:
- LLM returns 2 tool_calls: `get_vitals(P003)` + `get_medications(P003)` → dispatched in parallel
- LLM synthesizes comparison from both results in one pass

With text fallback, the same query takes 2 sequential iterations.

#### Multi-Service Fan-Out (Gateway Tools)

Some gateway tools orchestrate multiple backend calls internally:

- **`get_ward_rounds`**: Calls monitoring `/ward/{id}/rounds`, then fans out per patient to EHR (medications) and Orthanc (latest scan) **in parallel** (`asyncio.gather` for both requests per patient AND across all patients). Returns merged data.

This keeps the LLM loop simple (single tool call) while the gateway handles the cross-service aggregation.

#### Bed Auto-Resolution (Keyword Path)

In keyword fallback mode, bed references are resolved transparently:

```
User: "meds for bed 5"
  ↓ regex match → tool=get_medications, bed_id=BED5
  ↓ auto-resolve: call resolve_bed(BED5) → patient_id=P005
  ↓ dispatch: get_medications(patient_id=P005)
  ↓ return result
```

The `bed_id` is consumed during resolution and replaced with `patient_id` before the target tool is called.

#### Context and Memory

Each execution path shares:
- **Clinical memory**: facts extracted from tool results, injected into LLM context
- **Session persistence**: JSONL on disk, survives restarts
- **Consolidation**: LLM summarizes old messages when threshold exceeded
- **PHI redaction**: automatic for non-PHI-safe providers
- **Audit logging**: every tool call and response logged with timing

---

## Clinical Memory

Every tool call result is automatically parsed into structured **clinical facts** and stored in SQLite. Facts are never summarized or discarded.

Supported fact types: `vitals`, `vitals_trend`, `medication`, `allergy`, `lab_result`, `lab_order`, `demographics`, `order`, `imaging_study`, `radiology_report`, `blood_inventory`, `crossmatch`, `ecg`, `lab_interpretation`, `ecg_interpretation`, `vitals_interpretation`, `radiology_interpretation`.

Facts are injected into the LLM context for active patients, so the agent has full clinical history without re-querying backends.

---

## Structured Rich Responses

The gateway returns structured `blocks` alongside the plain text `response`, enabling each client to render data natively. Blocks are generated from actual tool call results — when the LLM responds without making tool calls, `blocks` is `null`.

### Block Types

| Block Type | Description | Example Source |
|------------|-------------|----------------|
| `data_table` | Tabular data with columns + rows | Vitals, labs, medications, allergies, blood inventory, ward patients, imaging studies |
| `key_value` | Key-value pairs | Patient demographics, radiology reports |
| `alert` | Severity-tagged notification (`warning`, `critical`, `info`) | Abnormal vitals (NEWS), drug interactions |
| `actions` | Clickable buttons that trigger tool calls | "View History", "Vitals", "Lab Results" |
| `confirmation` | Critical action confirmation with button | Code blue, blood crossmatch, medication dispense |
| `text` | Plain text content | Drug interaction "no results" |
| `image` | Image reference (PNG/JPEG) with URL and alt text | X-ray images, radiology scans |
| `chart` | Line/bar chart data with series | Vitals trend over time |
| `waveform` | Time-series waveform data with lead channels | ECG waveforms |

### Channel Rendering

Blocks are rendered differently per channel:

| Channel | `data_table` | `actions` | `alert` | `image` | `chart`/`waveform` |
|---------|-------------|-----------|---------|---------|-------------------|
| **webchat** | Passed through as-is (frontend renders) | Passed through | Passed through | Passed through | Passed through |
| **telegram** | HTML `<b>` title + key:value pairs | `InlineKeyboardMarkup` buttons | Emoji + bold HTML | `reply_photo()` | Text fallback (data in webchat) |
| **slack** | Block Kit `section` with mrkdwn | Block Kit `button` elements | mrkdwn with `:warning:` | Block Kit `image` | Not supported |
| **whatsapp** | Plain text with bold title | Numbered list | Plain text | Image URL | Not supported |

Unsupported block types are filtered per channel via `supported_blocks` in `config/channels.json`.

### Button Callbacks (Telegram)

Action buttons route through `/chat` as synthetic messages (e.g. "View History P001"), preserving the audit trail and allowing the LLM to enrich responses. Confirmation buttons POST directly to `/confirm/{id}`.

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
├── clinibot/                 # Gateway (FastAPI, port 3000)
│   ├── main.py              # App + endpoints (/chat, /chat/stream, /health, /confirm) + reminder background loop
│   ├── agent.py             # Agent loop (native + text tool paths, parallel dispatch, consolidation, streaming)
│   ├── providers.py         # LLM provider abstraction (Anthropic, Gemini, Ollama, OpenAI-compatible) + fallback chain + retry
│   ├── tools.py             # Tool registry + parallel dispatch + tool definitions + param validation + HTTP dispatch + critical gate + gateway tools
│   ├── audit.py             # SQLite audit logging
│   ├── clinical_memory.py   # Fact extraction + storage
│   ├── analyzers.py         # Specialized LLM analyzers (intercept + tool) + preprocessors + vision
│   ├── analyzer_prompts.py  # Domain-specific prompt templates for analyzers
│   ├── session.py           # JSONL-backed session persistence
│   ├── formatter.py         # Channel-aware response formatting + rich block rendering
│   ├── blocks.py            # Tool result → abstract UI block mappers (incl. get_latest_ecg)
│   ├── phi.py               # PHI redaction/restoration
│   ├── Dockerfile
│   └── requirements.txt
├── telegram-bot/              # Telegram bot bridge (polling)
│   ├── bot.py               # Rich rendering + inline keyboards + callback handler
│   ├── Dockerfile
│   └── requirements.txt
├── mcp-*/                    # MCP tool servers (8 services)
│   ├── server.py            # FastMCP stdio server + health endpoint
│   ├── Dockerfile
│   └── requirements.txt
├── synthetic-*/              # Synthetic backends (8 services)
│   ├── app.py               # FastAPI REST API with in-memory data
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

# 2. Set Anthropic API key (default provider)
export ANTHROPIC_API_KEY=sk-ant-...

# 3. (Optional) Configure fallback provider keys
export GEMINI_API_KEY=your-gemini-api-key

# 4. Build and start the full stack
docker compose up -d --build
# First build takes a few minutes (20 images). Subsequent starts are fast.

# 5. Verify
curl http://localhost:3000/health
```

To use Ollama (local GPU) instead of Anthropic, see [Change the Default LLM Model](#change-the-default-llm-model).

### Start

```bash
# Start all services (detached)
docker compose up -d

# Start just the gateway (if backends already running)
docker compose up -d clinibot-gateway
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
docker compose restart clinibot-gateway
```

Sessions persist across restarts (JSONL on disk). Reuse the same `session_id` to continue a conversation.

### Rebuild After Code Changes

```bash
# Rebuild + restart gateway only
docker compose build clinibot-gateway && docker compose up -d clinibot-gateway

# Rebuild a specific service
docker compose build mcp-bloodbank && docker compose up -d mcp-bloodbank

# Rebuild everything
docker compose up -d --build
```

### Change the Default LLM Model

Edit `agents.defaults` in `config/config.json`:

```json
{
  "agents": {
    "defaults": {
      "model": "claude-sonnet-4-20250514",
      "provider": "anthropic"
    }
  }
}
```

Then restart: `docker compose restart clinibot-gateway`

**Switch to Ollama (local GPU):**

```bash
# Pull the model first
docker compose up ollama -d
docker compose exec ollama ollama pull qwen2.5:7b
```

Set `"provider": "ollama"` in config and restart. Ollama is `phi_safe: true` — no PHI redaction needed.

**Switch to Anthropic:**

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Set `"provider": "anthropic"` in config and restart. PHI redaction activates automatically for providers with `"phi_safe": false`.

### Verify Session Persistence

```bash
# Send a message, note the session_id
curl -s -X POST http://localhost:3000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"Show vitals for P001","user_id":"doc1","channel":"webchat","tenant_id":"T1"}' \
  | jq .session_id

# Restart the gateway
docker compose restart clinibot-gateway

# Continue the same session
curl -X POST http://localhost:3000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"What did I ask?","user_id":"doc1","channel":"webchat","tenant_id":"T1","session_id":"<id>"}'

# Inspect session files on disk
docker compose exec clinibot-gateway ls /data/sessions/T1/
```

---

## Debugging & Log Analysis

### Viewing Logs

```bash
# Gateway logs (follow)
docker compose logs -f clinibot-gateway

# Gateway + all backends (trace requests end-to-end)
docker compose logs -f clinibot-gateway synthetic-monitoring synthetic-ehr \
  synthetic-lis synthetic-pharmacy synthetic-radiology synthetic-bloodbank \
  synthetic-erp synthetic-patient-services

# All services
docker compose logs -f

# Specific backend
docker compose logs -f synthetic-monitoring

# Last 50 lines (no follow)
docker compose logs --tail 50 clinibot-gateway
```

### Tracing a Request

To follow a single query through the system, tail the logs in one terminal and fire a request in another.

**Terminal 1:**

```bash
docker compose logs -f clinibot-gateway
```

**Terminal 2:**

```bash
curl -s -X POST http://localhost:3000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"Show vitals for P001","user_id":"doc1","channel":"webchat","tenant_id":"T1"}' | python3 -m json.tool
```

**What appears in order:**

| Step | Logger | What you see |
|------|--------|--------------|
| 1. Request received | `clinibot` | FastAPI access log: `POST /chat` |
| 2. Provider selection | `clinibot.agent` | `[session-id] query="..." provider=anthropic` |
| 3. Provider health check | `clinibot.providers` | `health_check provider=anthropic healthy=True` (skipped if cached within 30s TTL) |
| 4. LLM iteration | `clinibot.agent` | `[session-id] llm iteration=0 provider=anthropic native_tools=True` |
| 5. LLM request | `clinibot.providers` | `anthropic request: model=claude-sonnet-4-20250514 msgs=12 tools=41` |
| 6. LLM response | `clinibot.providers` | `anthropic response: stop=tool_use input_tokens=... output_tokens=...` |
| 7. Tool calls | `clinibot.agent` | `[session-id] llm requested tools: ['get_blood_availability']` |
| 8. Tool dispatch | `clinibot.tools` | `[session-id] dispatch GET http://synthetic-bloodbank:8000/availability` |
| 9. Tool result | `clinibot.agent` | `[session-id] tool_result: get_blood_availability params={} error=False` |
| 10. Final response | `clinibot.agent` | `[session-id] final response: 245 chars, 1 tool_results` |

If the provider hits rate limits (429), you'll see retry attempts:
```
clinibot.providers WARNING anthropic 429 — retry 1/2 in 1.0s
clinibot.providers WARNING anthropic 429 — retry 2/2 in 3.0s
```

If retries exhaust and tool results were already collected, the agent falls back:
```
clinibot.agent WARNING [session-id] provider returned None after 1 tool results — synthesizing from collected data
clinibot.agent INFO [session-id] using fallback provider 'gemini' for synthesis
```

### Using the Streaming Endpoint for Tracing

The SSE endpoint shows you each agent loop iteration in real time — useful for understanding multi-step tool chains:

```bash
curl -N -X POST http://localhost:3000/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"message":"Show vitals for P001","user_id":"doc1","channel":"webchat","tenant_id":"T1"}'
```

Each `data:` line maps to an agent step:

```
data: {"type":"tool_call","tool":"get_vitals","status":"started"}    ← tool selected
data: {"type":"tool_result","tool":"get_vitals","data":{...}}        ← backend responded
data: {"type":"text","content":"Patient P001 vitals are..."}         ← LLM synthesis
data: {"type":"done","session_id":"abc-123"}                         ← complete
```

### Audit Database Queries

Browse the audit UI at `http://localhost:8081`, or query from the command line.

**`audit_log` table columns:** `id`, `tenant_id`, `timestamp`, `session_id`, `user_id`, `channel`, `action`, `tool_name`, `params_hash`, `result_summary`, `confirmation_id`, `provider`, `model`, `latency_ms`

```bash
# Recent actions with timing
docker compose exec audit-db sqlite3 /data/clinic.db \
  "SELECT id, datetime(timestamp), action, tool_name, provider, latency_ms
   FROM audit_log ORDER BY id DESC LIMIT 10;"

# All tool calls for a specific session
docker compose exec audit-db sqlite3 /data/clinic.db \
  "SELECT datetime(timestamp), action, tool_name, result_summary
   FROM audit_log WHERE session_id='<SESSION_ID>' ORDER BY id;"

# Slowest requests (latency > 5s)
docker compose exec audit-db sqlite3 /data/clinic.db \
  "SELECT id, datetime(timestamp), action, tool_name, provider, latency_ms
   FROM audit_log WHERE latency_ms > 5000 ORDER BY latency_ms DESC LIMIT 20;"

# Critical tool confirmations
docker compose exec audit-db sqlite3 /data/clinic.db \
  "SELECT id, datetime(timestamp), tool_name, confirmation_id
   FROM audit_log WHERE action IN ('critical_tool_gated','critical_tool_confirmed')
   ORDER BY id DESC LIMIT 10;"

# Escalation history
docker compose exec audit-db sqlite3 /data/clinic.db \
  "SELECT e.id, a.timestamp, e.escalated_to, e.reason, e.resolved_at
   FROM escalations e JOIN audit_log a ON e.audit_log_id = a.id
   ORDER BY e.id DESC LIMIT 10;"

# Clinical facts for a patient
docker compose exec audit-db sqlite3 /data/clinic.db \
  "SELECT fact_type, fact_data, source_tool, recorded_at
   FROM clinical_facts WHERE patient_id='P001' ORDER BY id DESC LIMIT 10;"

# Provider usage breakdown
docker compose exec audit-db sqlite3 /data/clinic.db \
  "SELECT provider, COUNT(*) as calls, AVG(latency_ms) as avg_ms
   FROM audit_log WHERE action='chat_response' GROUP BY provider;"
```

### Inspecting Sessions on Disk

```bash
# List sessions for a tenant
docker compose exec clinibot-gateway ls /data/sessions/T1/

# Read a session JSONL (line 0 = metadata, lines 1+ = messages)
docker compose exec clinibot-gateway cat /data/sessions/T1/<session_id>.jsonl

# Count messages in a session
docker compose exec clinibot-gateway wc -l /data/sessions/T1/<session_id>.jsonl

# Check consolidation state (summary field in metadata line)
docker compose exec clinibot-gateway head -1 /data/sessions/T1/<session_id>.jsonl | python3 -m json.tool
```

### Common Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `"status":"degraded"` in `/health` | One or more backends down | Check `docker compose ps`, restart failed service |
| All responses are keyword-formatted | All LLM providers unreachable | Check provider logs; verify API keys are set; for Ollama check model is pulled |
| `anthropic 429 — retry` in logs | Anthropic rate limit | Normal — retries with backoff. If persistent, switch default provider or add rate limit headroom |
| `provider returned None after N tool results` | Provider failed mid-synthesis | Normal — agent synthesizes from collected data using fallback provider or text formatters |
| `No healthy providers available` | All configured providers down | Check API keys, network, and provider status pages |
| `OpenAI-compatible chat failed` in logs | Cloud provider error (rate limit, auth, timeout) | Check API key, quota, and `docker compose logs clinibot-gateway` |
| `Ollama chat failed` in logs | Model not loaded or OOM | `docker compose exec ollama ollama list`, check GPU memory |
| `Anthropic chat failed` in logs | Auth error or API issue | Verify `ANTHROPIC_API_KEY` is set; check Anthropic status page |
| `Invalid parameters:` error returned | Tool params failed validation | Check params against schema in `config/tools.json` |
| Session not found after restart | Wrong `tenant_id` on follow-up request | `tenant_id` is part of the session path; must match |
| 10s+ response times | Normal LLM inference for local models | Use a smaller model or cloud provider |
| Provider health check every request | Health cache expired (30s TTL) | Normal; first request after 30s incurs one health probe |

---

## Telegram Bot Setup

Connect Hobot to Telegram so clinicians can chat from their phone.

1. Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` → copy the token
2. Set the token:
   ```bash
   export TELEGRAM_BOT_TOKEN=<your-token>
   ```
3. Start the bot:
   ```bash
   docker compose up -d --build telegram-bot
   ```
4. Open Telegram, find your bot, and send a message (e.g. "Show vitals for P001")

The bot uses long-polling (no public URL or webhook required). Sessions are keyed by Telegram chat ID (`tg-<chat_id>`), so each chat maintains its own conversation history.

**Rich rendering:** When the gateway returns `blocks`, the bot renders them natively:
- `data_table` / `key_value` / `alert` → HTML-formatted messages (`parse_mode="HTML"`)
- `actions` → Telegram `InlineKeyboardMarkup` with callback buttons
- `confirmation` → Warning message with "Confirm" inline button
- `image` → `reply_photo()` with caption
- `chart` / `waveform` → Text fallback (full data available in webchat)

Button presses are handled via `CallbackQueryHandler`: action buttons send a synthetic message to `/chat`; confirmation buttons POST to `/confirm/{id}`.

| Variable | Required | Default |
|----------|----------|---------|
| `TELEGRAM_BOT_TOKEN` | yes | — |
| `GATEWAY_URL` | no | `http://clinibot-gateway:3000` |
| `TENANT_ID` | no | `default` |

### Telegram Bot Examples

**Vitals query** — renders as HTML table with alert and "View History" button:

```
User: Show vitals for P001

Bot response (HTML):
┌──────────────────────────────────┐
│ <b>Vitals — P001</b>            │
│ Heart Rate: 82 bpm              │
│ Blood Pressure: 145/92 mmHg     │
│ SpO2: 97 %                      │
│ Temperature: 37.2 °C            │
│ Respiratory Rate: 18 breaths/min│
│                                  │
│ ⚠️ <b>Blood Pressure abnormal</b>│
│                                  │
│ [View History]  ← inline button │
└──────────────────────────────────┘
```

Tapping **View History** sends a synthetic message `"Show vitals history for P001"` to the gateway, returning a trend chart block.

**Blood availability** — data table rendered as HTML:

```
User: blood availability

Bot response (HTML):
┌──────────────────────────────────┐
│ <b>Blood Availability</b>       │
│ A+: 45 units                    │
│ A-: 12 units                    │
│ B+: 38 units                    │
│ B-: 8 units                     │
│ O+: 52 units                    │
│ O-: 15 units                    │
│ AB+: 20 units                   │
│ AB-: 5 units                    │
└──────────────────────────────────┘
```

**Latest ECG** — waveform data with lead summary:

```
User: Get latest ECG for P001

Bot response (HTML):
┌──────────────────────────────────┐
│ ECG — P001                       │
│ Condition: Normal Sinus Rhythm   │
│ 7 leads, 500 Hz, 10s            │
│ Leads: I, II, III, aVR, aVL,   │
│        aVF, V1                   │
└──────────────────────────────────┘
```

**Code blue** — confirmation block with inline "Confirm" button:

```
User: code blue for P001

Bot response (HTML):
┌──────────────────────────────────┐
│ ⚠️ <b>Confirm Critical Action</b>│
│ initiate_code_blue               │
│ Patient: P001                    │
│                                  │
│ [✅ Confirm]  ← inline button    │
└──────────────────────────────────┘
```

Tapping **Confirm** POSTs to `/confirm/{id}` and the bot replies with the execution result.

**Action buttons** — any `actions` block renders as inline keyboard buttons. Each button triggers a new query to the gateway when tapped, keeping the full conversation in the audit trail.

**Ward rounds report** — per-patient cards sorted by severity:

```
User: rounds report for ICU-A

Bot response (HTML):
┌──────────────────────────────────┐
│ <b>P003 — Ward ICU-A</b>        │
│ Bed: BED3                       │
│ NEWS: 7                         │
│ HR: 115  BP: 90/55  SpO2: 91    │
│ Temp: 38.5                      │
│ Doctor: DR-SMITH                 │
│ Meds: Vancomycin, Norepinephrine│
│ Last Scan: Chest CT             │
│                                  │
│ ⚠️ P003 NEWS=7 — urgent review  │
├──────────────────────────────────┤
│ <b>P001 — Ward ICU-A</b>        │
│ Bed: BED1                       │
│ NEWS: 2                         │
│ HR: 80  BP: 120/78  SpO2: 97    │
│ ...                              │
└──────────────────────────────────┘
```

**Bed-based query** — resolves bed to patient transparently:

```
User: meds for bed 5

Bot response (HTML):
┌──────────────────────────────────┐
│ <b>Medications — P005</b>        │
│ Metoprolol  50mg  PO  BID       │
│ Aspirin     81mg  PO  Daily     │
│ ...                              │
└──────────────────────────────────┘
```

**Scheduling** — appointment confirmation card:

```
User: schedule appointment with Dr Patel for patient in bed 5

Bot response (HTML):
┌──────────────────────────────────┐
│ <b>Appointment Scheduled</b>     │
│ ID: APPT-3F8A2C1B               │
│ Patient: P005                    │
│ Doctor: DR-PATEL                 │
│ When: TBD                       │
│ Status: scheduled               │
└──────────────────────────────────┘
```

**Reminder** — scheduled confirmation + push at trigger time:

```
User: remind me in 2 hours to turn patient in bed 3

Bot response (HTML):
┌──────────────────────────────────┐
│ <b>Reminder Set</b>              │
│ ID: REM-7E4B1A2D                 │
│ Message: remind me in 2 hours...│
│ Fires at: 2026-03-04T18:00:00   │
└──────────────────────────────────┘

... 2 hours later, bot pushes:
⏰ Reminder: remind me in 2 hours to turn patient in bed 3
```

---

## Swapping Synthetic Backends for Real Systems

Each backend URL is configured via environment variable. To connect to a real hospital EHR:

```yaml
# docker-compose.yml
clinibot-gateway:
  environment:
    - EHR_BASE=https://real-ehr.hospital.local/fhir
```

Remove the corresponding `synthetic-*` service and its `mcp-*` dependency. The gateway calls backends directly, so MCP servers are only needed for external MCP clients.

---

## Volumes

| Volume | Mounted On | Purpose |
|--------|-----------|---------|
| `ollama-models` | ollama `/root/.ollama` | Persists downloaded LLM models |
| `audit-data` | clinibot-gateway `/data/audit`, audit-db `/data` | SQLite audit + clinical facts DB |
| `sessions-data` | clinibot-gateway `/data/sessions` | JSONL session files (per-tenant) |
| `hdf5-data` | synthetic-monitoring `/data/hdf5` | Persists HDF5 vitals data across restarts |

---

## License

TBD
