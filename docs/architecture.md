# Architecture & Design

## 1. System Overview

Hobot is a multi-service clinical AI gateway that translates natural language queries from clinicians into structured tool calls across eight hospital backend systems, then synthesizes human-readable responses.

```
                        +------------------+
                        |  Channel Layer   |
                        | Telegram, Web,   |
                        | Slack, WhatsApp  |
                        +--------+---------+
                                 | POST /chat, /confirm
                                 | Authorization: Bearer <key>
                                 v
+----------------------------------------------------------------+
|                  Clinibot Gateway (:3000)                       |
|                                                                |
|  +------------+  +--------------+  +-------------------------+ |
|  | Auth       |  | Rate Limiter |  | Prometheus /metrics     | |
|  | Middleware  |  | (per-client) |  | (optional)              | |
|  +------------+  +--------------+  +-------------------------+ |
|                                                                |
|  +--------------+  +-------------+  +------------------------+ |
|  | Orchestrator |  | Provider    |  | Tool Dispatch          | |
|  | (system      |  | Router      |  | (validate, critical    | |
|  |  prompt.md + |  | (anthropic, |  |  gate, parallel,       | |
|  |  SKILLS.md)  |  |  gemini,    |  |  HTTP to backends)     | |
|  |              |  |  ollama)    |  |                        | |
|  +------+-------+  +------+------+  +----------+-------------+ |
|         |                  |                    |               |
|  +------v-------+  +------v------+  +----------v-------------+ |
|  | Skills       |  | Domain      |  | Clinical Memory        | |
|  | (interpret,  |  | Models      |  | (fact extraction,      | |
|  |  validate,   |  | (LLM-based, |  |  SQLite storage)       | |
|  |  score)      |  |  rule-based)|  |                        | |
|  +--------------+  +-------------+  +------------------------+ |
|                                                                |
|  +------------+  +----------+  +---------+  +---------------+  |
|  | Session    |  | PHI      |  | Audit   |  | Response      |  |
|  | (JSONL)    |  | Redact   |  | (SQLite)|  | Formatter     |  |
|  +------------+  +----------+  +---------+  +---------------+  |
+-------------------------------+--------------------------------+
                                | HTTP (direct dispatch)
+-------------------------------v--------------------------------+
|                    Synthetic Backends (8)                       |
|  EHR (FHIR) | Monitoring | Radiology (Orthanc) | LIS          |
|  Pharmacy   | Blood Bank | ERP                  | Patient Svc  |
+----------------------------------------------------------------+

Separate (for external MCP clients only):
+----------------------------------------------------------------+
|  MCP Tool Servers (8) — FastMCP stdio transport                |
+----------------------------------------------------------------+
```

---

## 2. Orchestrator

The orchestrator (`orchestrator.py`, ~280 lines) is the central agent loop. It replaces the old 3-path agent with a single native-tool-calling loop.

### How It Works

```
1. Load config/system_prompt.md + SKILLS.md as system prompt
2. Build tool definitions (backend tools + skill tools)
3. Send conversation + system prompt + tools to LLM
4. LLM returns tool_call(s) or final text
5. If tool_calls:
   a. Dispatch ALL in parallel (asyncio.gather)
   b. For each result, check if a skill triggers
   c. Run matching skills, merge _analysis into tool_result
   d. Append enriched results to conversation
   e. Loop back to step 3
6. If text: return to user
7. Max 10 iterations
```

### System-Prompt-Driven

The system prompt is loaded from files at runtime:
- `config/system_prompt.md` — orchestration rules (when to resolve beds, critical action handling, PHI policy, response format)
- `SKILLS.md` — documents all skills so the LLM knows what auto-interprets and what to call explicitly (auto-generated via `python scripts/generate_skills_doc.py --write`)

This means behavior changes don't require code changes — edit the markdown files and restart. After adding/modifying skills, regenerate SKILLS.md.

### Skill Auto-Fire

After each tool dispatch, the orchestrator checks the skill registry:

```
tool_result arrives (e.g. get_vitals)
  --> skill_registry.get_interpreter("get_vitals")
  --> InterpretVitalsSkill found
  --> skill.run(SkillInput) --> SkillOutput
  --> tool_result["_analysis"] = skill_output.to_analysis_dict()
  --> LLM sees both raw vitals AND clinical interpretation
```

Skill failures are silent — raw data passes through unchanged.

---

## 3. Skills Framework

Skills are domain-specific capabilities that auto-interpret tool results.

### Skill Architecture

```
BaseSkill (ABC)
  |-- name, domain, required_context, interprets_tools
  |-- run(SkillInput) -> SkillOutput
  |-- domain_models: dict  (injected at startup)

SkillRegistry
  |-- register(skill)
  |-- get_interpreter(tool_name) -> BaseSkill | None
  |-- tool_definitions() -> list[dict]  (for LLM-callable skills)
```

### Skills and Triggers

| Skill | Auto-triggers on | Domain | Model Used |
|-------|-----------------|--------|------------|
| `interpret_vitals` | `get_vitals`, `get_vitals_history`, `get_vitals_trend` | critical_care | vitals_anomaly (NEWS2/MEWS + thresholds) + clinical_reasoning (LLM) |
| `interpret_labs` | `get_lab_results` | pathology | clinical_reasoning (context-aware) |
| `interpret_radiology` | `get_report` | radiology | clinical_reasoning |
| `interpret_ecg` | `get_latest_ecg`, `get_event_ecg` | cardiology | stub (waveform model interface ready, returns metadata only) |
| `medication_validation` | `check_drug_interactions` | pharmacy | drug_interaction (rule-based) |
| `blood_availability` | `get_blood_availability`, `order_blood_crossmatch` | bloodbank | passthrough |
| `service_orchestration` | `request_housekeeping`, `order_diet`, `request_ambulance`, `schedule_appointment` | services | passthrough |
| `care_plan_summary` | `get_care_plan` | core | clinical_reasoning (LLM summary of aggregated plan) |
| `analyze_radiology_image` | *(LLM-dispatched tool)* | radiology | radiology_model (vision — known limitation: GGUF lacks vision) |
| `clinical_context` | *(internal)* | core | builds patient context from memory |
| `clinical_summary` | *(internal)* | core | clinical_reasoning |
| `risk_scoring` | *(internal)* | critical_care | vitals_anomaly (NEWS2/MEWS + configurable thresholds) |

### SkillOutput Format

| Field | Description |
|-------|-------------|
| `status` | `"success"`, `"partial"`, `"error"`, `"skipped"` |
| `status_reason` | `"incomplete_context"`, `"limited_capability"`, `"missing_required_context"`, `"provider_unavailable"` |
| `domain` | Clinical domain label |
| `interpretation` | Free-text clinical interpretation |
| `findings` | Structured findings list |
| `score` | Optional (e.g. `{"news2": 7, "risk": "high", "violations": [...]}`) |
| `context_used` | Which fact types were available |
| `provider_name` | Which LLM/model ran the analysis |

### Error Handling

- Skill timeout (configurable, default 15s) — log, return raw data
- Domain model unavailable — skill skipped
- Missing required context (e.g. no demographics) — skill returns `status: "skipped"`
- PHI: redacted before sending to non-PHI-safe domain model providers
- Skip analysis: `skip_analysis: true` in tool params suppresses skill auto-fire for that tool call

---

## 4. Domain Models

Domain models provide the inference layer. Skills inject them via constructor.

```
DomainModel (ABC)
  |-- name, version
  |-- predict(input: dict) -> ModelResult
  |-- is_available() -> bool
```

| Model | Type | Default Provider | Purpose |
|-------|------|------------------|---------|
| `clinical_reasoning` | LLM-based | MedGemma 4B-IT (Ollama) | General clinical interpretation. Used by interpret_vitals, interpret_labs, interpret_radiology, interpret_ecg, clinical_summary. Configurable: `skills.clinical_reasoning.provider` |
| `radiology_model` | LLM-based (vision) | MedGemma 4B-IT (Ollama) | Fetches DICOM from Orthanc via WADO, sends to LLM. Used by analyze_radiology_image. Configurable: `skills.radiology_model.provider` |
| `vitals_anomaly` | Rule-based | *(local)* | NEWS2 scoring (Royal College of Physicians ranges) + trend detection. No LLM call. Always available. |
| `drug_interaction` | Rule-based | *(local)* | Supplements pharmacy backend results with local high-severity pair rules. No LLM call. Always available. |

**Available clinical LLM providers:**

| Provider | Model | Size (Q4) | Strengths |
|----------|-------|-----------|-----------|
| `medgemma` | MedGemma 4B-IT | ~2.5 GB | Best clinical reasoning quality, follows instructions well, medical-domain trained |
| `openbiollm` | OpenBioLLM 8B | ~4.9 GB | Alternative, broader biomedical knowledge, needs completion-style prompts |
| `anthropic` | Claude Sonnet | Cloud | Highest quality, cloud-only, PHI redaction active |
| `gemini` | Gemini 2.5 Flash | Cloud | Fast, cloud-only, PHI redaction active |

---

## 5. Provider Architecture

### Class Hierarchy

```
LLMProvider (abstract)
├── OllamaProvider           # /api/generate or /api/chat — domain models (PHI-safe)
│                            # Uses /api/generate for single prompts (better with base models)
├── OpenAICompatibleProvider  # /v1/chat/completions — native tools
│   └── GeminiProvider        # /chat/completions (custom URL)
└── AnthropicProvider         # /v1/messages (native Messages API)

Provider selection: explicit "type" field in config, or auto-detected from name/URL.
```

### Fallback Chain

```
get_healthy_provider():
  1. Try default provider (agents.defaults.provider)
  2. Try remaining providers in config order
  3. All unhealthy → return None → orchestrator returns error
```

Health checks are cached with 30s TTL.

### Retry Logic

```
Retryable: 429, 502, 503, 529
Max retries: 2
Backoff: 1.0s, 3.0s
```

### Mid-Request Failover

If a provider fails after tool results are collected, the orchestrator synthesizes from collected data or tries a fallback provider. Tool results are never discarded.

---

## 6. Sample Clinical Workflows

### Flow 1: Vitals Query with Threshold Resolution (1 MCP, 4-layer thresholds)

```
User: "Show vitals for P001"

Classify --> [vitals]
LLM --> get_vitals(patient_id="P001")
Dispatch --> GET monitoring:8000/vitals/P001
Skill --> interpret_vitals:
  1. Build context (demographics, medications, allergies from memory/EHR)
  2. Resolve thresholds:
     Layer 1: code defaults (HR 60-100, BP 90-140, ...)
     Layer 2: hospital config (config.json overrides)
     Layer 3: patient-specific (memory → GET monitoring:8000/thresholds/P001 → cache)
     Layer 4: request params (if any)
  3. vitals_anomaly.predict(vitals, merged_thresholds, scoring="news2")
     → check_thresholds() → violations + NEWS2 score
  4. clinical_reasoning.predict(vitals + thresholds + context → MedGemma)
     → clinical assessment text
LLM --> synthesize response from vitals + score + assessment
```

### Flow 2: Lab Interpretation (3 MCPs, parallel)

```
User: "Interpret labs for patient in bed 5"

LLM --> resolve_bed("BED5") --> P005
LLM --> parallel: get_lab_results(P005) + get_patient(P005) + get_medications(P005)
Dispatch --> LIS + EHR + EHR (parallel)
Skill --> interpret_labs (demographics context from get_patient)
  --> "Potassium 5.8 elevated -- consider renal function with ACE inhibitor"
LLM --> synthesize
```

### Flow 3: Ward Rounds (fan-out, 3 MCPs)

```
User: "Rounds report for ICU-A"

LLM --> get_ward_rounds(ward_id="ICU-A")
Gateway fan-out per patient:
  monitoring/ward/ICU-A/rounds
  + per patient: EHR/medications + radiology/studies (parallel)
Merge --> severity-sorted list
LLM --> synthesize
```

### Flow 4: Radiology Image (vision model)

```
User: "Analyze chest CT for P003"

LLM --> get_studies(P003) --> radiology
LLM --> get_patient(P003) --> EHR
LLM --> analyze_radiology_image(study_id, P003)
Skill --> radiology_model fetches DICOM via WADO, sends to vision LLM
  --> "Bilateral ground-glass opacities, consistent with atypical pneumonia"
LLM --> synthesize
```

### Flow 5: Code Blue (critical gating)

```
User: "Code blue for bed 3"

LLM --> resolve_bed("BED3") --> P003
LLM --> initiate_code_blue(P003) --> CRITICAL
Gate --> returns confirmation_id (not executed)
User --> POST /confirm/{id}
Execute --> POST monitoring/code-blue
Audit --> critical_tool_confirmed
```

### Flow 6: Multi-Domain Assessment (5 MCPs, 3 skills)

```
User: "Full assessment for P003"

LLM --> parallel: get_vitals + get_lab_results + get_latest_study + get_patient + get_medications
Dispatch --> monitoring + LIS + radiology + EHR + EHR (parallel)
Skills: interpret_vitals (NEWS2=7 high) + interpret_labs (parallel)
LLM --> get_report(study_id)
Skill --> interpret_radiology
LLM --> synthesize from 3 skill interpretations
```

### Flow 7: Drug Interactions (rule-based model)

```
User: "Check interactions for P001"

LLM --> get_medications(P001) --> EHR
LLM --> check_drug_interactions(...) --> pharmacy
Skill --> medication_validation (drug_interaction model)
  --> "Warfarin + Aspirin: bleeding risk"
LLM --> synthesize
```

### Flow 8: Parallel Service Requests

```
User: "Diabetic lunch for P005, housekeeping room 301"

LLM --> parallel: order_diet(P005) + request_housekeeping(room 301)
Dispatch --> patient-services (parallel)
Skill --> service_orchestration (formats both)
LLM --> synthesize
```

### Flow 9: Clinical Alarms + Threshold Update (critical)

```
User: "Show alarms for P003 and update their systolic BP threshold to 90"

Classify --> [vitals]
Iteration 1:
  LLM --> parallel: get_active_alarms(P003) + update_patient_thresholds(P003, {bp_systolic: {low: 90}})
  get_active_alarms --> GET monitoring:8000/alarms/P003
    --> 3 active alarms (HR breach, SpO2 breach, ventilator FiO2)
    --> stored in clinical_memory (TTL: 1h)
  update_patient_thresholds --> CRITICAL (requires confirmation)
    --> response includes confirmation_id

User --> POST /confirm/{id}
  --> PUT monitoring:8000/thresholds/P003
  --> patient-specific thresholds updated
  --> clinical_memory: vitals_thresholds fact replaced
  --> audit: critical_tool_confirmed
```

### Flow 10: Care Plan (gateway fan-out, 4 backends)

```
User: "What are the next tasks for P001?"

Classify --> [orders]
LLM --> get_care_plan(patient_id="P001")
Gateway fan-out (parallel):
  1. EHR --> get_orders(P001) --> pending/active orders
  2. Patient Services --> GET /appointments?patient_id=P001
  3. Patient Services --> GET /reminders?patient_id=P001
  4. LIS --> get_lab_results(P001) --> filter status=pending/ordered
Aggregate --> {orders, appointments, reminders, pending_labs}
Skill --> care_plan_summary (MedGemma summarizes priorities)
LLM --> synthesize final response
```

### Flow 11: Ward Risk Ranking (gateway fan-out)

```
User: "Order patients in ICU-A by criticality"

Classify --> [ward]
LLM --> get_ward_risk_ranking(ward_id="ICU-A")
Gateway fan-out:
  GET monitoring:8000/ward/ICU-A/patients
  --> returns patients with latest vitals + NEWS scores
  Sort by NEWS2 descending
  Assign risk levels: high (>=7), medium (>=5), low (<5)
Returns: [{P003, NEWS2=14, high}, {P001, NEWS2=2, low}]
LLM --> synthesize ranked patient list
```

### Flow 12: Appointment with Name Resolution (3 tools, 2 backends)

```
User: "Schedule appointment with Dr Patel for patient in bed 5"

Classify --> [scheduling, ward]
Iteration 1:
  LLM --> parallel: resolve_bed("5") + resolve_doctor(name="Patel")
  resolve_bed --> GET monitoring:8000/bed/BED5/patient --> P005
  resolve_doctor --> GET monitoring:8000/doctor/resolve?name=Patel --> DR-PATEL
Iteration 2:
  LLM --> schedule_appointment(patient_id="P005", doctor_id="DR-PATEL", ...)
  Dispatch --> POST patient-services:8000/appointment
  Skill --> service_orchestration (formats appointment confirmation)
LLM --> synthesize
```

### Flow 13: Conditions + Allergies (2 backends, parallel)

```
User: "What are allergies and comorbidities for P003?"

Classify --> [medications]
LLM --> parallel: get_allergies(P003) + get_conditions(P003)
  get_allergies --> EHR/fhir/AllergyIntolerance?patient=P003
  get_conditions --> GET monitoring:8000/conditions/P003
    --> sepsis, AKI, COPD (with ICD-10 codes)
  Both stored in clinical_memory (allergy: 168h, condition: 168h)
LLM --> synthesize combined view
```

### Flow 14: Skip Analysis (raw data, no skill)

```
User: "Show X-ray report for P001, no interpretation needed"

Classify --> [radiology]
LLM --> get_studies(P001) --> find X-ray study_id
LLM --> get_report(study_id, skip_analysis=true)
Orchestrator --> sees skip_analysis=true in params --> skips interpret_radiology skill
LLM --> returns raw report text without interpretation
```

---

## 7. Session & Memory

### Session Lifecycle

Sessions are JSONL files at `/data/sessions/{tenant_id}/{session_id}.jsonl`.
- Line 0: metadata (user, tenant, last_activity, consolidation state, active patients)
- Lines 1+: messages (PHI-redacted on disk) and consolidation events
- Loaded from disk on first access, cached in memory
- User-bound: session_id is tied to the creating user_id; mismatched user gets a new session
- TTL: sessions expire after `SESSION_TTL_HOURS` (default 24h); stale sessions are evicted from memory periodically

### Memory Consolidation

When messages exceed threshold (default 30), older messages are summarized via LLM. The summary is prepended as a system message. Last 10 messages kept verbatim.

### Clinical Facts

Every tool result is parsed into structured facts and stored in SQLite with TTL:

```
get_vitals              --> vitals (TTL: 4h)
get_vitals_trend        --> vitals_trend (TTL: 4h)
get_medications         --> medication (TTL: 24h)
get_allergies           --> allergy (TTL: 168h)
get_lab_results         --> lab_result (TTL: 12h)
get_patient             --> demographics (TTL: 168h)
get_latest_ecg          --> ecg (TTL: 8h)
get_patient_thresholds  --> vitals_thresholds (TTL: 168h)
update_patient_thresholds --> vitals_thresholds (replaces cached)
get_active_alarms       --> alarm (TTL: 1h)
get_conditions          --> condition (TTL: 168h)
get_report              --> radiology_report (TTL: 168h)
get_studies             --> imaging_study (TTL: 168h)
```

Facts are injected into the LLM system prompt for active patients. Skills also read facts directly (e.g. `interpret_vitals` reads `vitals_thresholds` to resolve patient-specific thresholds).

---

## 8. Response Rendering

```
OrchestratorResult { text, tool_results[] }
  --> build_blocks(tool_results)  (blocks.py)
  --> filter by supported_blocks  (channels.json)
  --> render_blocks(channel)      (formatter.py)
  --> ChatResponse { response, session_id, blocks }
```

Block types: `data_table`, `key_value`, `alert`, `actions`, `confirmation`, `text`, `image`, `chart`, `waveform`.

Each channel has a renderer (webchat: pass-through, telegram: HTML + inline keyboards, slack: Block Kit, whatsapp: plain text).

---

## 9. Deployment Model

Hobot is designed as a **single-instance** deployment behind a hospital firewall. In-memory state is intentional and acceptable:

- **`_pending` confirmations** — TTL-bounded, HMAC-signed, in-memory dict
- **`_sessions` cache** — in-memory with JSONL persistence, TTL-evicted
- **Rate limiter** — in-memory sliding window keyed on authenticated client identity
- **Shared `httpx.AsyncClient`** — connection pooling for backend dispatch

### What changes for HA (not currently needed)

| Component | Single-instance | HA replacement |
|-----------|----------------|----------------|
| Sessions | In-memory + JSONL | Redis / DB-backed sessions |
| Rate limiting | In-memory sliding window | Redis-backed rate limiter |
| Confirmations | In-memory `_pending` dict | Database-backed confirmations |
| HTTP client | Shared `httpx.AsyncClient` | Per-instance (no change) |

---

## 10. Security

### API Key Authentication

All endpoints (except `/health`, `/docs`, `/openapi.json`, `/metrics`) require `Authorization: Bearer <key>`. Keys configured via `API_KEYS` env var (`client_id:key` pairs). The authenticated `client_id` is used for rate limiting and confirmation binding.

### PHI Redaction

Non-PHI-safe providers (cloud APIs) receive redacted messages. The following patterns are detected and replaced with opaque `XPHI_*` tokens (e.g. `P001` → `XPHI_PID_a1b2c3d4`):

- Patient IDs (`P001`, `UHID...`), MRNs
- Dates (YYYY-MM-DD), phone numbers
- Email addresses, SSN (`123-45-6789`), Aadhaar (`1234 5678 9012`)
- Doctor names after "Dr."

Tokens use a bracket-free format (`XPHI_<label>_<hex>`) so LLMs treat them as opaque identifiers and pass them through to tool calls verbatim. Restore is multi-pass: exact token, bracketed, label+hex, and hex-only (word-boundary) to handle various ways LLMs may mangle tokens.

Ollama is PHI-safe (local) — redaction is skipped entirely. SSE tool result events are also redacted for non-PHI-safe providers. Session JSONL files on disk contain PHI-redacted messages (one-way; in-memory keeps originals for current session context).

### Critical Tool Gating

Tools marked `critical` in `tools.json` are not executed immediately. A `confirmation_id` is returned. The clinician must `POST /confirm/{id}` to authorize. Confirmations are hardened with:

- **5-minute TTL** — expired confirmations are rejected and cleaned up (configurable via `CONFIRMATION_TTL_SECONDS`)
- **Client binding** — the confirming client must match the client that created the request
- **HMAC signing** — confirmation IDs are signed with an in-memory secret (regenerated each startup) to prevent forgery

### Session Security

- **User binding** — if a different `user_id` presents an existing `session_id`, a new session is created (prevents session hijacking)
- **TTL expiry** — sessions older than `SESSION_TTL_HOURS` (default 24h) are treated as expired
- **Stale eviction** — expired sessions are periodically removed from the in-memory cache

### Audit Integrity

- Params are SHA-256 hashed (never raw PHI in audit)
- SQLite WAL mode, append-only design
- Audit DB web UI is internal-only (no external port); for dev access, add `ports: ["8081:8080"]` to `audit-db` in `docker-compose.override.yml`

---

## 11. Container Deployment

### Container Topology

| Tier | Containers |
|------|-----------|
| Gateway | clinibot-gateway (:3000) |
| Channels | telegram-bot |
| Models | ollama (GPU, :11435 on host) — MedGemma 4B-IT, OpenBioLLM 8B |
| Backends (8) | synthetic-ehr (:8080), synthetic-monitoring, synthetic-radiology (:8042), synthetic-lis, synthetic-pharmacy, synthetic-bloodbank, synthetic-erp, synthetic-patient-services |
| MCP (8) | mcp-ehr, mcp-monitoring, ... (for external MCP clients, not used by gateway) |
| Observability | audit-db (internal only, no external port) |

### Volumes

| Volume | Purpose |
|--------|---------|
| `ollama-models` | Persisted LLM models |
| `audit-data` | SQLite audit + clinical facts |
| `sessions-data` | JSONL session files |
| `hdf5-data` | HDF5 vitals data |

### Swapping to Production

Change `*_BASE` env vars to real hospital URLs. Remove corresponding `synthetic-*` containers. Same REST interface.

---

## 12. Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Markdown-driven system prompt** | Behavior changes via markdown (`config/system_prompt.md`), not code. Skills docs included so LLM knows capabilities. |
| **Single native-tool-calling loop** | Simpler than 3-path agent. All providers use native tools (Ollama is domain-model-only). |
| **Skills as auto-fire interpreters** | LLM doesn't need to call analyzers explicitly. Interpretation is transparent. |
| **Rule-based domain models** | NEWS2 scoring and drug interactions don't need LLM. Always available, deterministic. |
| **Direct HTTP dispatch** (not MCP) | Lower latency, parallel dispatch. MCP servers exist for external clients only. |
| **JSONL sessions** | Append-only, easy inspection, survives restarts. |
| **Channel-agnostic gateway** | Block abstraction lets any channel render natively. |
| **Critical tool gating** | Prevents catastrophic actions without human approval. |
| **PHI redaction at gateway** | Single enforcement point, transparent to providers. |
| **MedGemma for clinical reasoning** | Medical-domain fine-tuned, 4B params fits alongside orchestrator model on single GPU, instruction-following quality better than OpenBioLLM for structured clinical output. |
| **Ollama `/api/generate` for domain models** | Base/completion models produce better clinical text via generate endpoint than chat endpoint. Auto-selected for single-prompt calls. |
| **4-layer threshold resolution** | Defaults → hospital config → patient-specific (backend/memory) → per-request. Allows hospital-wide policy with physician overrides per patient. |
| **Gateway fan-out tools** | `get_care_plan` and `get_ward_risk_ranking` aggregate from multiple backends in one tool call, avoiding N orchestrator iterations. |
| **`skip_analysis` flag** | Clinicians can suppress auto-interpretation per-request when they just want raw data. |
| **Clinical alarms as vitals-domain tools** | Alarms come from monitoring backend alongside vitals — same domain, consistent classification. `clear_alarm` is critical (requires confirmation + audit reason). |
