# Hobot — Agentic AI Clinician Assistant

Conversational clinical agent that unifies eight hospital systems behind a single chat API. Clinicians ask questions in natural language; Hobot queries EHR, vitals, labs, imaging, pharmacy, blood bank, ERP, and patient services, then returns a synthesized answer.

Built on a **system-prompt-driven orchestrator** with a **skills framework** and **domain models** for automatic clinical interpretation.

---

## Prerequisites

| Dependency | Version | Notes |
|------------|---------|-------|
| Docker + Docker Compose | 24.0+ | Compose V2 (`docker compose`) |
| NVIDIA Container Toolkit | latest | GPU passthrough for Ollama |
| NVIDIA GPU | 6+ GB VRAM | RTX 4050 or better |

No host Python install required — everything runs in containers.

---

## Quick Start

```bash
# 1. Clone and enter repo
git clone <repo-url> && cd hobot

# 2. Set API keys and auth
cp .env.example .env
# Edit .env with your keys (GEMINI_API_KEY, ANTHROPIC_API_KEY, API_KEYS)

# 3. Build and start everything
docker compose up -d --build

# 4. Pull clinical models into Ollama
docker compose exec ollama ollama pull hf.co/SandLogicTechnologies/MedGemma-4B-IT-GGUF:Q4_K_M

# 5. Check health (no auth required)
curl http://localhost:3000/health

# 6. Send a chat message (auth required)
curl -X POST http://localhost:3000/chat \
  -H 'Authorization: Bearer your-secret-key' \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "Show vitals for P001",
    "user_id": "doc1",
    "channel": "webchat",
    "tenant_id": "T1"
  }'
```

---

## Architecture Overview

```
User --> POST /chat --> Orchestrator (system prompt from config/system_prompt.md + SKILLS.md)
                             |
                        LLM Provider (native tool calling)
                             |
                  +----------+----------+
               Skills      Tools     Domain Models
             (interpret,  (HTTP to   (clinical_reasoning,
              validate,   backends)   vitals_anomaly,
              score)                  drug_interaction,
                  |          |        radiology_model)
             Clinical Memory + Audit
```

**20 containers**: 1 gateway, 1 Ollama (MedGemma + OpenBioLLM on GPU), 8 MCP tool servers, 8 synthetic backends, 1 Telegram bot, 1 audit DB viewer.

See [docs/architecture.md](docs/architecture.md) for the full design.

---

## API

All endpoints except `/health`, `/docs`, `/openapi.json`, and `/metrics` require an `Authorization: Bearer <key>` header. Keys are configured via the `API_KEYS` environment variable.

### `POST /chat`

```json
{
  "message": "Show vitals for P001",
  "user_id": "doc1",
  "channel": "webchat",
  "tenant_id": "T1",
  "session_id": null
}
```

Returns `{ response, session_id, blocks }`. Pass `session_id` back to continue the conversation.

### `POST /chat/stream`

Same request body, returns Server-Sent Events (`tool_call` → `tool_result` → `text` → `done`).

### `POST /confirm/{confirmation_id}`

Execute a critical tool that was gated behind human confirmation. Confirmations expire after 5 minutes and are bound to the originating client.

### `GET /health`

Per-backend health status. Returns `"ok"` or `"degraded"`. No authentication required.

---

## Available Tools

40+ tools across 12 clinical domains:

| Domain | Tools | Backend |
|--------|-------|---------|
| **Vitals** | `get_vitals`, `get_vitals_history`, `get_vitals_trend`, `get_patient_thresholds`, `update_patient_thresholds`†, `get_active_alarms`, `clear_alarm`† | synthetic-monitoring |
| **Ward** | `list_wards`, `get_ward_patients`, `resolve_bed`, `resolve_doctor`, `get_ward_risk_ranking`* | synthetic-monitoring + gateway |
| **EHR** | `get_patient`, `get_medications`, `get_allergies`, `get_conditions`, `get_orders`, `write_order`† | synthetic-ehr (HAPI FHIR) + monitoring |
| **ECG** | `get_latest_ecg` (with duration param), `get_event_ecg` | synthetic-monitoring |
| **Radiology** | `get_studies`, `get_report`, `get_latest_study`, `analyze_radiology_image` | synthetic-radiology (Orthanc) + domain model |
| **Labs** | `get_lab_results`, `order_lab`†, `get_order_status` | synthetic-lis |
| **Pharmacy** | `check_drug_interactions`, `dispense_medication`† | synthetic-pharmacy |
| **Blood Bank** | `get_blood_availability`, `order_blood_crossmatch`† | synthetic-bloodbank |
| **Orders** | `get_care_plan`* (fan-out: orders + appointments + reminders + pending labs) | gateway |
| **ERP** | `get_inventory`, `get_equipment_status`, `place_supply_order` | synthetic-erp |
| **Patient Services** | `request_housekeeping`, `order_diet`, `request_ambulance`†, `schedule_appointment`, `set_reminder` | synthetic-patient-services |
| **Emergency** | `initiate_code_blue`†, `escalate` | synthetic-monitoring + gateway |

†Critical tool (requires human confirmation). *Gateway fan-out tool.

---

## Sample Workflows

### Simple Vitals (1 MCP, 1 skill)

```
User: "Show vitals for P001"

Step 1: Classify --> domains: [vitals]
Step 2: LLM --> tool_call: get_vitals(patient_id="P001")
Step 3: Dispatch --> GET synthetic-monitoring/vitals/P001
Step 4: Skill --> interpret_vitals --> NEWS2=2 (low risk)
Step 5: LLM --> synthesize response from tool result + interpretation
```

### Lab Interpretation with Context (3 MCPs, parallel)

```
User: "Interpret labs for patient in bed 5"

Step 1: LLM --> resolve_bed(bed_id="BED5") --> P005
Step 2: LLM --> parallel: get_lab_results(P005) + get_patient(P005) + get_medications(P005)
Step 3: Dispatch --> LIS + EHR + EHR (parallel)
Step 4: Skill --> interpret_labs (uses demographics context)
         --> "Potassium 5.8 elevated -- consider renal function with ACE inhibitor"
Step 5: LLM --> synthesize
```

### Multi-Domain Assessment (5 MCPs, 3 skills)

```
User: "Full assessment for P003 -- vitals, labs, and imaging"

Step 1: LLM --> parallel: get_vitals + get_lab_results + get_latest_study + get_patient + get_medications
Step 2: Dispatch --> monitoring + LIS + radiology + EHR + EHR (parallel)
Step 3: Skills (parallel): interpret_vitals (NEWS2=7, high risk) + interpret_labs
Step 4: LLM --> get_report(study_id=...)
Step 5: Skill --> interpret_radiology
Step 6: LLM --> synthesize comprehensive assessment from 3 skill interpretations
```

### Emergency Code Blue (critical action gating)

```
User: "Code blue for bed 3"

Step 1: LLM --> resolve_bed("BED3") --> P003
Step 2: LLM --> initiate_code_blue(P003) --> CRITICAL, gated
Step 3: Response --> "Requires confirmation" + confirmation_id
Step 4: User --> POST /confirm/{id} --> executes code blue
```

### Drug Interaction Check (2 MCPs, rule-based model)

```
User: "Check interactions for P001's medications"

Step 1: LLM --> get_medications(P001) --> EHR
Step 2: LLM --> check_drug_interactions(...) --> pharmacy
Step 3: Skill --> medication_validation (drug_interaction model)
         --> "Warfarin + Aspirin: increased bleeding risk"
```

### Service Requests (parallel)

```
User: "Order diabetic lunch for P005 and housekeeping for room 301"

Step 1: LLM --> parallel: order_diet(P005, "diabetic", "lunch") + request_housekeeping("room 301")
Step 2: Dispatch --> patient-services (parallel)
Step 3: Skill --> service_orchestration (formats both confirmations)
```

### Clinical Alarms + Threshold Update (critical action)

```
User: "Show alarms for P003 and update their BP threshold to 90"

Step 1: LLM --> get_active_alarms(P003) --> monitoring (3 active alarms)
Step 2: LLM --> update_patient_thresholds(P003, {bp_systolic: {low: 90}})
Step 3: CRITICAL --> requires confirmation
Step 4: User --> POST /confirm/{id} --> threshold updated + cached in memory
```

### Care Plan (gateway fan-out)

```
User: "What are the next tasks for P001?"

Step 1: LLM --> get_care_plan(P001) --> gateway fan-out
Step 2: Parallel: EHR/orders + patient-services/appointments + reminders + LIS/pending labs
Step 3: Skill --> care_plan_summary (LLM synthesis)
```

### Ward Risk Ranking (gateway fan-out)

```
User: "Order patients by criticality in ICU-A"

Step 1: LLM --> get_ward_risk_ranking(ward_id="ICU-A") --> gateway fan-out
Step 2: Fetch ward patients + vitals --> sort by NEWS2 descending
Step 3: Returns: [{P003, NEWS2=14, high}, {P001, NEWS2=2, low}, ...]
```

---

## Key Features

- **System-prompt-driven orchestration** — orchestration rules (`config/system_prompt.md`) and skill docs loaded as system prompt
- **Skills framework** — 12 domain skills auto-fire after tool results (interpret_vitals, interpret_labs, interpret_ecg, interpret_radiology, medication_validation, care_plan_summary, etc.)
- **Domain models** — MedGemma (clinical reasoning, radiology), OpenBioLLM (alternative), rule-based (vitals_anomaly NEWS2, drug_interaction). Swappable via config
- **Multi-provider LLM routing** — Anthropic, Gemini, Ollama (MedGemma/OpenBioLLM), OpenAI-compatible with fallback chain and retry
- **Parallel tool dispatch** — multiple tools per LLM turn via `asyncio.gather`
- **API key authentication** — Bearer token auth on all endpoints, per-client rate limiting
- **Critical action gating** — code blue, medication dispense, etc. require human confirmation (HMAC-signed, 5-min TTL, client-bound)
- **PHI redaction** — automatic for non-PHI-safe cloud providers; opaque `XPHI_*` tokens with multi-pass restore (patient IDs, MRNs, DOBs, phones, emails, SSNs, Aadhaar, doctor names)
- **Clinical memory** — structured facts extracted from every tool call, injected into LLM context
- **Session persistence** — JSONL on disk (PHI-redacted), survives restarts, user-bound with TTL, LLM-based consolidation
- **Streaming SSE** — real-time tool call progress via `/chat/stream`
- **Multi-channel** — webchat, Telegram (rich rendering), Slack, WhatsApp
- **Audit trail** — immutable SQLite log of every action

---

## Configuration

All config lives in `config/`. See [docs/configuration.md](docs/configuration.md) for full reference.

| File | Purpose |
|------|---------|
| `config/config.json` | LLM providers, skills, domain models, classifier, rate limits |
| `config/tools.json` | Tool criticality + parameter schemas |
| `config/channels.json` | Per-channel rendering capabilities |

### Change the Default LLM Provider

Edit `agents.defaults.provider` in `config/config.json`, then `docker compose restart clinibot-gateway`.

### Environment Variables

| Variable | Description |
|----------|-------------|
| `GEMINI_API_KEY` | Required for Gemini provider |
| `ANTHROPIC_API_KEY` | Required for Anthropic provider (optional fallback) |
| `HF_TOKEN` | HuggingFace token for gated models (MedGemma) |
| `API_KEYS` | API auth keys, format: `client_id:key,client_id:key` |
| `TELEGRAM_BOT_TOKEN` | Required for Telegram bot + reminders |
| `TG_BOT_API_KEY` | Telegram bot's API key (must match an entry in `API_KEYS`) |
| `ORTHANC_USER` / `ORTHANC_PASS` | Radiology backend credentials (default: `orthanc`) |
| `SESSION_TTL_HOURS` | Session expiry in hours (default: `24`) |
| `CONFIRMATION_TTL_SECONDS` | Confirmation expiry in seconds (default: `300`) |
| `*_BASE` (8 vars) | Backend URLs (default: synthetic containers) |

---

## Project Structure

```
hobot/
├── CLAUDE.md                 # Claude Code dev instructions (OpenSpec)
├── SKILLS.md                 # Skill docs (auto-generated, loaded as system prompt)
├── config/                   # Runtime configuration (incl. system_prompt.md)
├── schema/init.sql           # Audit DB schema
├── clinibot/                 # Gateway (FastAPI, :3000)
│   ├── main.py               # Endpoints + lifecycle
│   ├── orchestrator.py       # Single native-tool-calling loop (~280 lines)
│   ├── skills/               # 12 domain skill modules
│   ├── domain_models/        # 4 inference models (LLM + rule-based)
│   ├── providers.py          # Multi-LLM abstraction + fallback
│   ├── tools.py              # Tool registry + HTTP dispatch
│   ├── clinical_memory.py    # Fact extraction + storage
│   ├── classifier.py         # Intent classification (regex + LLM)
│   ├── formatter.py          # Channel-aware response rendering
│   ├── auth.py               # API key authentication middleware
│   └── ...                   # session, audit, phi, blocks, metrics, ratelimit
├── telegram-bot/             # Telegram bridge (polling + rich rendering)
├── mcp-*/                    # 8 MCP tool servers (for external clients)
├── synthetic-*/              # 8 synthetic backends (FastAPI)
├── scripts/                 # Dev tooling (generate_skills_doc.py, etc.)
└── docker-compose.yml
```

---

## Operations

```bash
# Start
docker compose up -d

# Stop (data preserved)
docker compose down

# Rebuild gateway after code changes
docker compose up -d --build clinibot-gateway

# Rebuild + reload .env changes (env vars require --force-recreate)
docker compose up -d --build --force-recreate clinibot-gateway

# View logs
docker compose logs -f clinibot-gateway

# Test a skill standalone (from host, no Docker needed for rule-based skills)
python scripts/test_skill.py --list
python scripts/test_skill.py interpret_vitals --tool get_vitals \
  --mock '{"heart_rate":95,"bp_systolic":150,"spo2":97,"temperature":37.5}' \
  --patient P001 --demo default --host-mode

# Regenerate SKILLS.md after modifying skills
python scripts/generate_skills_doc.py --write

# Browse audit DB (dev only — add ports: ["8081:8080"] to audit-db in docker-compose.override.yml)
# open http://localhost:8081
```

---

## Telegram Bot

1. Create a bot via [@BotFather](https://t.me/BotFather), get the token
2. `export TELEGRAM_BOT_TOKEN=<token>`
3. `export TG_BOT_API_KEY=<key>` (must match an entry in `API_KEYS`, e.g., `API_KEYS=tg-bot:<key>`)
4. `docker compose up -d --build telegram-bot`

Rich rendering: data tables as HTML, alerts with severity, inline action buttons, confirmation prompts.

---

## Swapping Synthetic Backends for Real Systems

```yaml
# docker-compose.yml
clinibot-gateway:
  environment:
    - EHR_BASE=https://real-ehr.hospital.local/fhir
```

Remove the corresponding `synthetic-*` service. The gateway dispatches via HTTP — same interface, different URL.

---

## Further Reading

- [Architecture & Design](docs/architecture.md) — orchestrator internals, skills framework, provider architecture, data flows, security
- [Configuration Reference](docs/configuration.md) — all config files, environment variables, provider setup, skills config

---

## License

TBD
