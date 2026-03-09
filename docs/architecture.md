# Hobot — Architecture & Design Document

## 1. System Overview

Hobot is a multi-service clinical AI gateway that translates natural language queries from clinicians into structured tool calls across eight hospital backend systems, then synthesizes human-readable responses.

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Channel Layer                                │
│  ┌───────────┐  ┌───────────┐  ┌───────────┐  ┌───────────┐       │
│  │ Telegram  │  │  Webchat  │  │   Slack   │  │ WhatsApp  │       │
│  │   Bot     │  │  Client   │  │   Bot     │  │  Bridge   │       │
│  └─────┬─────┘  └─────┬─────┘  └─────┬─────┘  └─────┬─────┘       │
│        └───────────────┴───────────────┴───────────────┘            │
│                        POST /chat, /confirm                         │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
┌────────────────────────────────▼────────────────────────────────────┐
│                   Clinibot Gateway (:3000)                           │
│                                                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐           │
│  │ Agent    │  │ Provider │  │ Tool     │  │ Response │           │
│  │ Loop     │  │ Router   │  │ Dispatch │  │ Formatter│           │
│  │          │  │          │  │          │  │          │           │
│  │ intent   │  │ anthropic│  │ validate │  │ blocks   │           │
│  │ detect   │  │ gemini   │  │ critical │  │ channel  │           │
│  │ tool     │  │ ollama   │  │ gate     │  │ render   │           │
│  │ chain    │  │ fallback │  │ parallel │  │ filter   │           │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └──────────┘           │
│       │              │              │                               │
│  ┌────▼─────┐  ┌────▼─────┐  ┌────▼─────┐  ┌──────────┐           │
│  │ Session  │  │ PHI      │  │ Clinical │  │ Audit    │           │
│  │ Manager  │  │ Redact   │  │ Memory   │  │ Logger   │           │
│  │ (JSONL)  │  │ /Restore │  │ (Facts)  │  │ (SQLite) │           │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘           │
└────────────────────────────────┬────────────────────────────────────┘
                                 │ HTTP (direct dispatch)
┌────────────────────────────────▼────────────────────────────────────┐
│                     Synthetic Backends                               │
│                                                                     │
│  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐      │
│  │ EHR (FHIR) │ │ Monitoring │ │ Radiology  │ │ LIS        │      │
│  │ :8080      │ │ (HDF5)     │ │ (Orthanc)  │ │            │      │
│  └────────────┘ └────────────┘ └────────────┘ └────────────┘      │
│  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐      │
│  │ Pharmacy   │ │ Blood Bank │ │ ERP        │ │ Patient Svc│      │
│  └────────────┘ └────────────┘ └────────────┘ └────────────┘      │
└─────────────────────────────────────────────────────────────────────┘

Separate from tool dispatch (MCP protocol for external clients):
┌─────────────────────────────────────────────────────────────────────┐
│                     MCP Tool Servers (8)                             │
│  mcp-ehr, mcp-monitoring, mcp-radiology, mcp-lis,                  │
│  mcp-pharmacy, mcp-bloodbank, mcp-erp, mcp-patient-services        │
│  (FastMCP stdio — for external MCP clients, not used by gateway)    │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. Component Architecture

### 2.1 Gateway (clinibot/)

The gateway is a FastAPI application with six internal modules:

| Module | File | Responsibility |
|--------|------|----------------|
| **API Layer** | `main.py` | HTTP endpoints, startup lifecycle, reminder loop |
| **Agent Loop** | `agent.py` | LLM orchestration, intent detection, tool chaining, synthesis |
| **Provider Router** | `providers.py` | Multi-LLM abstraction, health checks, fallback chain, retry |
| **Tool Dispatch** | `tools.py` | Backend HTTP calls, param validation, critical gating, gateway tools |
| **Response Formatter** | `formatter.py` + `blocks.py` + `renderers.py` | Tool result → abstract blocks → channel-native rendering |
| **Cross-cutting** | `session.py`, `clinical_memory.py`, `audit.py`, `phi.py` | Persistence, fact extraction, audit logging, PHI safety |

### 2.2 Channel Layer (telegram-bot/)

Channel apps are thin bridges that:
1. Receive platform messages (Telegram polling, webhook, etc.)
2. Forward to `POST /chat` with channel-specific session ID
3. Render response `blocks` using platform-native UI (HTML, Block Kit, etc.)
4. Handle confirmation button presses via `POST /confirm/{id}`

The gateway is channel-agnostic. All channel logic lives in the channel app + formatter.

### 2.3 Synthetic Backends

Each backend is a standalone FastAPI service with in-memory seed data. They implement the same REST interface that a real hospital system would expose. Swapping to production requires changing only the `*_BASE` environment variable.

### 2.4 MCP Tool Servers

Eight MCP servers expose the same tools via the MCP (Model Context Protocol) stdio transport. These are for **external MCP clients** (e.g., Claude Desktop). The gateway does **not** use MCP — it dispatches directly via HTTP to backends.

---

## 3. Interface Specifications

### 3.1 Channel → Gateway

```
Channel App ──POST /chat──▶ Gateway ──JSON──▶ Channel App
             (ChatRequest)              (ChatResponse)
```

**ChatRequest:**
```json
{
  "message": "Show vitals for P001",
  "user_id": "nurse-jane",
  "channel": "telegram",
  "tenant_id": "hospital-a",
  "session_id": "tg-123456789"
}
```

**ChatResponse:**
```json
{
  "response": "Patient P001 vitals: HR 82 bpm, BP 120/80...",
  "session_id": "tg-123456789",
  "blocks": [
    {"type": "data_table", "title": "Vitals", "columns": [...], "rows": [...]},
    {"type": "alert", "severity": "warning", "text": "BP elevated"},
    {"type": "actions", "buttons": [{"label": "View History", ...}]}
  ]
}
```

Blocks are **optional** — null when no tools were called. Old clients ignore `blocks` and use `response`.

### 3.2 Gateway → LLM Provider

```
Agent Loop ──chat(messages, tools)──▶ Provider ──ChatResult──▶ Agent Loop
```

**ChatResult (unified across all providers):**
```python
@dataclass
class ChatResult:
    content: str | None          # Text response
    tool_calls: list[ToolCall]   # Native function calls
    raw_message: dict | None     # For re-feeding to provider

@dataclass
class ToolCall:
    id: str        # Provider-assigned call ID
    name: str      # Tool name (e.g., "get_vitals")
    params: dict   # Tool parameters
```

**Provider-specific wire formats:**

| Provider | API Endpoint | Auth | Tool Format | Message Format |
|----------|-------------|------|-------------|----------------|
| Anthropic | `/v1/messages` | `x-api-key` header | `input_schema` + `tool_use`/`tool_result` blocks | `system` as separate param, content as block arrays |
| Gemini | `{baseUrl}/chat/completions` | `Bearer` token | OpenAI `functions` format | OpenAI message format |
| Ollama | `{baseUrl}/api/chat` | None | N/A (text-based JSON parsing) | Ollama message format |
| OpenAI-compat | `{baseUrl}/v1/chat/completions` | `Bearer` token | OpenAI `functions` format | OpenAI message format |

### 3.3 Gateway → Backend (Tool Dispatch)

```
Tool Dispatch ──HTTP GET/POST──▶ Synthetic Backend ──JSON──▶ Tool Dispatch
```

**Dispatch mapping** (`TOOL_BACKENDS` dict):
```python
"get_vitals":    (MONITORING_BASE, "GET",  "/vitals/{patient_id}")
"get_patient":   (EHR_BASE,       "GET",  "/fhir/Patient/{patient_id}")
"get_studies":   (RADIOLOGY_BASE, "GET",  "/dicom-web/studies?PatientID={patient_id}")
"order_lab":     (LIS_BASE,       "POST", "/lab/order")
```

Path template variables are substituted from tool params. Remaining params go as query string (GET) or JSON body (POST). Orthanc (radiology) requires basic auth (`orthanc:orthanc`).

### 3.4 Gateway → Audit DB

```
Agent/Tools ──log_action()──▶ SQLite (WAL mode)
```

All writes go through `audit.py`. Params are SHA-256 hashed. Three tables:

| Table | Purpose | Mutability |
|-------|---------|------------|
| `audit_log` | Every action (tool_call, chat_response, confirmation) | Append-only |
| `escalations` | Human escalation tracking | Append + resolve |
| `clinical_facts` | Structured medical data per patient | Append-only |

### 3.5 MCP Server → Backend

```
MCP Client ──stdio──▶ MCP Server ──HTTP──▶ Synthetic Backend
```

MCP servers are independent of the gateway. They expose the same tools via MCP protocol for external clients (Claude Desktop, etc.). Each server has:
- FastMCP stdio transport (MCP protocol)
- Background FastAPI health endpoint (`:8000/health`)
- Local response cache for degraded mode

---

## 4. Data Flow: End-to-End Request

```
1. User sends "code blue for bed 2" on Telegram
   │
2. telegram-bot/bot.py
   ├── session_id = "tg-{chat_id}"
   └── POST /chat { message, user_id, channel:"telegram", session_id }
       │
3. main.py: /chat endpoint
   ├── Load/create session (JSONL on disk)
   └── run_agent(message, session)
       │
4. agent.py: run_agent
   ├── get_healthy_provider() → tries anthropic → gemini → ollama
   ├── Build system prompt + clinical context (injected facts)
   └── _run_with_provider() loop:
       │
       ├── Iteration 0: LLM returns tool_call: resolve_bed(bed_id="2")
       │   ├── tools.py: _resolve_bed → normalize "2" → "BED2"
       │   ├── HTTP GET synthetic-monitoring:8000/bed/BED2/patient
       │   └── Returns { patient_id: "P002" }
       │
       ├── Iteration 1: LLM returns tool_call: initiate_code_blue(patient_id="P002")
       │   ├── tools.py: is_critical("initiate_code_blue") → true
       │   ├── GATED: Store pending confirmation, return awaiting_confirmation
       │   └── agent.py: Detects awaiting_confirmation → return immediately
       │
5. formatter.py: format_rich_response
   ├── blocks.py: build_blocks(tool_results) → confirmation block
   ├── Filter by supported_blocks for channel
   └── _render_telegram → HTML text + InlineKeyboardButton
       │
6. main.py: Return ChatResponse { response, session_id, blocks }
   │
7. telegram-bot/bot.py
   ├── Render confirmation block with "Confirm" button
   └── User taps "Confirm"
       │
8. telegram-bot/bot.py: handle_callback
   └── POST /confirm/{confirmation_id}
       │
9. main.py: /confirm endpoint
   ├── tools.py: confirm_tool() → pop from pending, execute tool
   ├── HTTP POST synthetic-monitoring:8000/code-blue
   ├── audit.py: log critical_tool_confirmed
   └── Return result to Telegram
```

---

## 5. Agent Loop Design

### 5.1 Execution Paths

The agent has two execution paths with shared post-processing:

```
                    ┌─────────────────┐
                    │   User Message  │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │ Healthy Provider│
                    │   Available?    │
                    └──┬──────────┬───┘
                     Yes          No
                       │          │
              ┌────────▼──┐  ┌───▼──────────┐
              │ LLM Path  │  │ Keyword Path │
              │ (native   │  │ (regex       │
              │  tools or │  │  intent      │
              │  text     │  │  detection)  │
              │  parsing) │  │              │
              └─────┬─────┘  └──────┬───────┘
                    │               │
              ┌─────▼───────────────▼─────┐
              │   Shared Post-Processing   │
              │ • clinical_memory extract  │
              │ • audit logging            │
              │ • PHI restoration          │
              │ • block generation         │
              │ • channel rendering        │
              └───────────────────────────┘
```

### 5.2 LLM Path — Native Tool Calling

For providers with native function calling (Anthropic, Gemini, OpenAI):

```
Loop (max 10 iterations):
  1. Send messages[] + tool_defs[] to LLM
  2. LLM returns ChatResult
     ├── tool_calls[] → dispatch ALL in parallel (asyncio.gather)
     │   ├── If any result is awaiting_confirmation → return immediately
     │   ├── Append tool results to messages
     │   └── Continue loop
     └── text content → return as final response
```

### 5.3 LLM Path — Text Parsing (Ollama)

For providers without native function calling:

```
Loop (max 10 iterations):
  1. Send messages[] to LLM (tools described in system prompt)
  2. LLM returns text
     ├── Contains JSON {"tool":"...","params":{}} → parse, dispatch ONE tool
     │   ├── If awaiting_confirmation → return immediately
     │   ├── Append result as user message
     │   └── Continue loop
     └── No JSON → return as final response
```

### 5.4 Keyword Path — Regex Fallback

When no LLM is available:

```
1. Match message against ordered regex patterns
2. Extract tool name + params
3. Auto-resolve bed_id → patient_id if needed
4. Dispatch single tool
5. Synthesize response:
   ├── Try fallback LLM provider (if any healthy)
   └── Else use built-in text formatters
```

---

## 6. Provider Architecture

### 6.1 Class Hierarchy

```
LLMProvider (abstract)
├── OllamaProvider           # /api/chat, text-based tool parsing
├── OpenAICompatibleProvider  # /v1/chat/completions, native tools
│   └── GeminiProvider        # /chat/completions (custom URL)
└── AnthropicProvider         # /v1/messages (native Messages API)
```

### 6.2 Fallback Chain

```
get_healthy_provider():
  1. Try default provider (config.agents.defaults.provider)
     ├── Healthy (cached 30s TTL) → return
     └── Unhealthy → continue
  2. Try each remaining provider in config order
     ├── Healthy → return as fallback
     └── Unhealthy → continue
  3. All unhealthy → return None (keyword fallback)
```

### 6.3 Retry Logic

All providers share `_request_with_retry()`:

```
Retryable status codes: 429, 502, 503, 529
Max retries: 2
Backoff: 1.0s, 3.0s (exponential)

request → 429 → wait 1s → retry → 429 → wait 3s → retry → 429 → raise
```

### 6.4 Mid-Request Failover

When a provider fails **after** tool results are collected:

```
1. Tool calls succeed (data collected)
2. Synthesis call → 429 (retries exhausted)
3. Mark provider unhealthy
4. get_healthy_provider() → find fallback
5. Synthesize using fallback provider
6. If no fallback → use built-in text formatters
7. Tool results are NEVER discarded
```

---

## 7. Session & Memory Architecture

### 7.1 Session Lifecycle

```
┌──────────┐   get_or_create()   ┌──────────────┐
│  Request  │ ──────────────────▶ │  In-Memory   │
│  arrives  │                     │  Cache        │
└──────────┘                     └───────┬──────┘
                                         │ miss
                                  ┌──────▼──────┐
                                  │  JSONL File  │
                                  │  on Disk     │
                                  └─────────────┘

File: /data/sessions/{tenant_id}/{session_id}.jsonl
Line 0: {"type":"metadata", "session_id":"...", "user_id":"...", ...}
Line 1: {"type":"message", "role":"user", "content":"...", "timestamp":"..."}
Line 2: {"type":"message", "role":"assistant", "content":"...", "timestamp":"..."}
...
```

### 7.2 Memory Consolidation

```
Messages: [m1, m2, m3, ..., m28, m29, m30, m31]
                                        │
           Threshold (30) exceeded ─────┘
                                        │
Consolidate m1..m21 into summary ──────▶ [summary] + [m22..m31]
                                         ▲                ▲
                                    prepended         kept verbatim
                                    as system msg     (last 10)
```

### 7.3 Clinical Fact Extraction

Every tool result passes through fact extractors:

```
Tool Result ──▶ Extractor ──▶ clinical_facts table
                (per-tool)

Extractors:
  get_vitals      → fact_type: "vitals"
  get_medications → fact_type: "medication" (one per med)
  get_allergies   → fact_type: "allergy"
  get_lab_results → fact_type: "lab_result"
  get_patient     → fact_type: "demographics"
  get_studies     → fact_type: "imaging_study"
  ...
```

Facts are injected into the LLM system prompt for active patients:
```
Known facts for P001:
  - [vitals] {"heart_rate": 82, "bp_systolic": 120, ...}
  - [medication] {"medication": "Metoprolol", "dose": "50mg", ...}
```

---

## 8. Response Rendering Pipeline

```
AgentResult { text, tool_results[] }
      │
      ▼
build_blocks(tool_results)
      │ Tool-specific block builders (blocks.py)
      │ e.g., get_vitals → data_table + alert + actions
      ▼
filter by supported_blocks (channels.json)
      │ e.g., WhatsApp doesn't support "actions"
      ▼
render_blocks(blocks, channel)
      │ Channel-specific renderers (formatter.py)
      │ e.g., Telegram → HTML + InlineKeyboardMarkup
      ▼
ChatResponse { response, session_id, blocks }
```

**Block types and their rendering:**

| Block | Webchat | Telegram | Slack | WhatsApp |
|-------|---------|----------|-------|----------|
| `data_table` | JSON pass-through | HTML `<b>` + key:value | mrkdwn `section` | Plain text |
| `key_value` | JSON pass-through | HTML `<b>` + key:value | mrkdwn `section` | Plain text |
| `alert` | JSON pass-through | Emoji + bold HTML | `:warning:` mrkdwn | Plain text |
| `actions` | JSON pass-through | `InlineKeyboardMarkup` | Block Kit buttons | Numbered list |
| `confirmation` | JSON pass-through | HTML + Confirm button | Not supported | Not supported |
| `image` | JSON pass-through | `reply_photo()` | Block Kit image | Image URL |
| `chart`/`waveform` | JSON pass-through | Server-rendered PNG | Not supported | Not supported |

---

## 9. Security Architecture

### 9.1 PHI Redaction

```
User message ─▶ Session messages ─▶ [PHI Redaction] ─▶ LLM Provider
                                     (non-PHI-safe       (cloud API)
                                      providers only)

Patterns redacted:
  • Patient IDs: P001, UHID12345 → [PATIENT_ID_abc123]
  • MRN: MRN00456 → [MRN_def789]
  • Dates: 1980-01-15 → [DATE_ghi012]
  • Phone numbers → [PHONE_jkl345]

LLM response ─▶ [PHI Restoration] ─▶ User
                  (reverse mapping)
```

### 9.2 Critical Tool Gating

```
Tool Call ─▶ is_critical(tool_name)?
              │
              ├── No  → Execute immediately
              └── Yes → Store in _pending dict
                        Return { status: "awaiting_confirmation",
                                 confirmation_id: UUID }
                                 │
                        User confirms via /confirm/{id}
                                 │
                        Pop from _pending, execute, audit log
```

### 9.3 Audit Integrity

- All params are SHA-256 hashed before storage (never raw PHI in audit)
- Result summaries are truncated and redacted
- SQLite WAL mode for concurrent read safety
- Immutable append-only design (no UPDATE/DELETE on audit_log)

---

## 10. Deployment Architecture

### 10.1 Container Topology

```
┌──────────────────────────────────────────────┐
│              Docker Compose Stack             │
│                                              │
│  Gateway Tier                                │
│  ├── clinibot-gateway (:3000)                │
│  └── telegram-bot                            │
│                                              │
│  Model Tier                                  │
│  └── ollama (GPU passthrough)                │
│                                              │
│  Backend Tier                                │
│  ├── synthetic-ehr (:8080, HAPI FHIR)        │
│  ├── synthetic-monitoring                    │
│  ├── synthetic-radiology (:8042, Orthanc)    │
│  ├── synthetic-lis                           │
│  ├── synthetic-pharmacy                      │
│  ├── synthetic-bloodbank                     │
│  ├── synthetic-erp                           │
│  └── synthetic-patient-services              │
│                                              │
│  MCP Tier (external clients only)            │
│  ├── mcp-ehr         ├── mcp-pharmacy        │
│  ├── mcp-monitoring  ├── mcp-bloodbank       │
│  ├── mcp-radiology   ├── mcp-erp             │
│  ├── mcp-lis         └── mcp-patient-services│
│                                              │
│  Observability                               │
│  └── audit-db (:8081, sqlite-web)            │
│                                              │
│  Volumes                                     │
│  ├── ollama-models    ├── audit-data          │
│  ├── sessions-data    └── hdf5-data           │
└──────────────────────────────────────────────┘
```

### 10.2 Exposed Ports

| Port | Service | Purpose |
|------|---------|---------|
| 3000 | clinibot-gateway | Chat API |
| 8080 | synthetic-ehr | FHIR R4 server |
| 8042 | synthetic-radiology | Orthanc DICOM viewer |
| 8081 | audit-db | Audit log browser |

### 10.3 Swapping to Production

Each backend is configured via environment variable:

```yaml
# docker-compose.yml
clinibot-gateway:
  environment:
    - EHR_BASE=https://real-ehr.hospital.local/fhir
    - LIS_BASE=https://real-lis.hospital.local/api
    - MONITORING_BASE=https://real-monitoring.hospital.local
```

Remove the corresponding `synthetic-*` container. The gateway's HTTP dispatch is backend-agnostic — same REST interface, different URL.

---

## 11. Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Direct HTTP dispatch** (not MCP) for gateway | Lower latency, simpler error handling, parallel dispatch. MCP servers exist for external clients only. |
| **JSONL sessions** (not DB) | Append-only writes, no schema migrations, easy inspection, survives restarts. |
| **SQLite for audit** (not Postgres) | Zero-config, WAL for concurrency, sufficient for single-gateway deployments. |
| **Provider fallback chain** | Maximizes availability across rate-limited cloud APIs. |
| **Channel-agnostic gateway** | Block abstraction lets any channel render natively without gateway changes. |
| **Critical tool gating** | Prevents catastrophic actions (code blue, medication dispense) without human approval. |
| **PHI redaction at gateway** | Single enforcement point, transparent to LLM providers. |
| **Keyword fallback** | Ensures basic functionality even when all LLM providers are down. |
| **Facts stored separately from conversation** | Conversation can be consolidated/summarized; clinical facts are never lost. |
| **Bed normalization in gateway** | LLMs send inconsistent formats ("2", "bed2", "BED2"); normalizing at the tool layer is more reliable than prompt engineering. |
