# POC Setup & Operations Guide

Complete setup, configuration, testing, and tracing guide for Hobot PoC/pilot deployment.

---

## 1. Prerequisites

| Requirement | Version | Check |
|-------------|---------|-------|
| Docker + Compose | 24.0+ | `docker compose version` |
| NVIDIA Container Toolkit | latest | `nvidia-ctdi --version` |
| NVIDIA GPU | 6+ GB VRAM | `nvidia-smi` |
| Gemini API key | — | [Get one](https://aistudio.google.com/apikey) |

---

## 2. Initial Setup

### 2a. Create `.env`

```bash
cd hobot
cp .env.example .env
```

Edit `.env`:

```bash
# Required — at least one LLM provider
GEMINI_API_KEY=your-gemini-api-key

# Optional — Anthropic as fallback provider
ANTHROPIC_API_KEY=your-anthropic-api-key

# Optional — HuggingFace token for gated models (MedGemma)
# Accept terms at https://huggingface.co/google/medgemma-4b-it then set token
HF_TOKEN=your-hf-token

# Auth — generate real secrets for pilot
API_KEYS=tg-bot:$(openssl rand -hex 16),webchat:$(openssl rand -hex 16)

# Telegram (optional — skip if not using)
TELEGRAM_BOT_TOKEN=your-bot-token
TG_BOT_API_KEY=<same key as tg-bot above>

# Defaults are fine for POC:
# ORTHANC_USER=orthanc
# ORTHANC_PASS=orthanc
# SESSION_TTL_HOURS=24
# CONFIRMATION_TTL_SECONDS=300
```

### 2b. First Build & Start

```bash
# Build everything (first time takes 5-10 min)
docker compose up -d --build

# Watch startup logs
docker compose logs -f clinibot-gateway --since 1m
```

Wait for: `Clinibot gateway started` in logs.

### 2c. Pull Clinical Models (Ollama)

The gateway uses domain-specific models for clinical reasoning. Pull them into the Docker Ollama instance:

```bash
# MedGemma — medical reasoning (used by clinical_reasoning + radiology_model)
docker compose exec ollama ollama pull hf.co/SandLogicTechnologies/MedGemma-4B-IT-GGUF:Q4_K_M

# OpenBioLLM — alternative clinical model (optional)
docker compose exec ollama ollama pull koesn/llama3-openbiollm-8b

# Warm up the default model (first load takes ~60-90s for GPU init)
docker compose exec ollama ollama run hf.co/SandLogicTechnologies/MedGemma-4B-IT-GGUF:Q4_K_M "test" --nowordwrap
```

Verify models are loaded:
```bash
docker compose exec ollama ollama list
```

### 2d. Verify Health

```bash
curl -s http://localhost:3000/health | python3 -m json.tool
```

Expected: all backends `"ok"`. Some may take 30-60s to start (especially `synthetic-ehr` HAPI FHIR).

If degraded, check which backend is down:
```bash
docker compose ps
docker compose logs synthetic-ehr --tail 20
```

### 2e. Verify Auth

```bash
# Should return 401
curl -s -o /dev/null -w "%{http_code}" \
  -X POST http://localhost:3000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"hi","user_id":"doc1","tenant_id":"T1"}'

# Should return 200
curl -s -X POST http://localhost:3000/chat \
  -H "Authorization: Bearer $(grep webchat .env | cut -d: -f3 | cut -d, -f1)" \
  -H 'Content-Type: application/json' \
  -d '{"message":"List all wards","user_id":"doc1","tenant_id":"T1"}' | python3 -m json.tool
```

---

## 3. Configuration Files

All in `config/`, mounted at `/app/config` in the container.

| File | What it controls | When to change |
|------|------------------|----------------|
| `config.json` | Providers, skills, classifier, rate limits, memory TTL | Change default LLM, tune rate limits, toggle skills |
| `tools.json` | Tool criticality, param schemas | Mark tools critical, add param validation |
| `channels.json` | Per-channel rendering | Adjust message limits, enable/disable block types |
| `system_prompt.md` | LLM behavior rules | Change orchestration behavior without code changes |

After editing any config:
```bash
docker compose restart clinibot-gateway
```

### Key Config Switches

**Change default provider** (in `config.json`):
```json
"agents": { "defaults": { "provider": "gemini" } }
```

**Disable a skill** (in `config.json`):
```json
"interpret_ecg": { "enabled": false }
```

**Make a tool critical** (in `tools.json`):
```json
"order_diet": { "critical": true, "params": { "patient_id": { "type": "string", "required": true } } }
```

---

## 4. Test Sequences

### 4a. Basic Vitals Query

```bash
API_KEY="your-webchat-key"

curl -s -X POST http://localhost:3000/chat \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "Show vitals for P001",
    "user_id": "doc1",
    "channel": "webchat",
    "tenant_id": "T1"
  }' | python3 -m json.tool
```

**What happens**: classify → [vitals] → get_vitals(P001) → interpret_vitals skill → NEWS2 score → LLM synthesis.

### 4b. Multi-Tool Parallel (Labs + Meds)

```bash
curl -s -X POST http://localhost:3000/chat \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "Interpret labs for P001 and check their medications",
    "user_id": "doc1",
    "channel": "webchat",
    "tenant_id": "T1"
  }' | python3 -m json.tool
```

### 4c. Bed Resolution

```bash
curl -s -X POST http://localhost:3000/chat \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "Show vitals for patient in bed 2",
    "user_id": "doc1",
    "channel": "webchat",
    "tenant_id": "T1"
  }' | python3 -m json.tool
```

### 4d. Critical Tool Gating (Code Blue)

```bash
# Step 1: Request code blue (will be gated)
RESP=$(curl -s -X POST http://localhost:3000/chat \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "Initiate code blue for P001",
    "user_id": "doc1",
    "channel": "webchat",
    "tenant_id": "T1"
  }')
echo "$RESP" | python3 -m json.tool

# Step 2: Extract confirmation_id and confirm
CID=$(echo "$RESP" | python3 -c "import sys,json,re; m=re.search(r'Confirmation ID: ([a-f0-9-]+)', json.load(sys.stdin)['response']); print(m.group(1) if m else '')")
echo "Confirmation ID: $CID"

curl -s -X POST "http://localhost:3000/confirm/$CID" \
  -H "Authorization: Bearer $API_KEY" | python3 -m json.tool
```

### 4e. Ward Rounds (fan-out)

```bash
curl -s -X POST http://localhost:3000/chat \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "Ward rounds report for WARD-A",
    "user_id": "doc1",
    "channel": "webchat",
    "tenant_id": "T1"
  }' | python3 -m json.tool
```

### 4f. Streaming (SSE)

```bash
curl -N -X POST http://localhost:3000/chat/stream \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "Show vitals for P002",
    "user_id": "doc1",
    "channel": "webchat",
    "tenant_id": "T1"
  }'
```

Watch events: `tool_call` → `tool_result` → `text` → `done`.

### 4g. Standalone Skill Testing

Test individual skills without the full orchestrator, using `scripts/test_skill.py`:

```bash
# List all skills and their triggers
python scripts/test_skill.py --list

# Test with mock data + mock demographics (no backends needed)
python scripts/test_skill.py interpret_vitals --tool get_vitals \
  --mock '{"heart_rate":95,"bp_systolic":150,"spo2":97,"temperature":37.5}' \
  --patient P001 --demo default --host-mode

# Rule-based skills (no LLM needed)
python scripts/test_skill.py risk_scoring --tool get_vitals \
  --mock '{"heart_rate":135,"bp_systolic":85,"spo2":90,"respiration_rate":26}' \
  --patient P003 --demo default

python scripts/test_skill.py medication_validation --tool check_drug_interactions \
  --mock '{"interactions":[]}' \
  --params '{"medications":["warfarin","aspirin"]}'

# Override LLM provider (e.g. use Anthropic from host)
python scripts/test_skill.py interpret_vitals --tool get_vitals \
  --mock '{"heart_rate":95,"bp_systolic":150,"spo2":97,"temperature":37.5}' \
  --patient P001 --demo default --provider anthropic

# Custom demographics
python scripts/test_skill.py interpret_labs --tool get_lab_results \
  --mock '{"labs":[{"WBC":"12.5"},{"Creatinine":"2.1"}]}' \
  --patient P002 --demo '{"age":78,"gender":"Female","active_medications":["Lisinopril 10mg"]}'
```

**Flags:**
| Flag | Description |
|------|-------------|
| `--mock` | Mock tool result JSON (skip backend call) |
| `--demo default` | Use built-in test patient demographics |
| `--demo '{...}'` | Custom demographics JSON |
| `--host-mode` | Rewrite Docker hostnames to localhost (Ollama on :11435) |
| `--provider X` | Override all skill LLM providers (e.g. `anthropic`, `medgemma`) |
| `-v` | Verbose — show raw tool result and full SkillOutput JSON |

The script auto-loads `.env` for API keys. Rule-based skills (risk_scoring, medication_validation, blood_availability, service_orchestration) work fully offline.

### 4h. Session Continuity

```bash
# First message — note the session_id in response
RESP=$(curl -s -X POST http://localhost:3000/chat \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"message":"Show vitals for P001","user_id":"doc1","channel":"webchat","tenant_id":"T1"}')
SID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")

# Follow-up — pass session_id back
curl -s -X POST http://localhost:3000/chat \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d "{\"message\":\"Now check their labs\",\"user_id\":\"doc1\",\"session_id\":\"$SID\",\"channel\":\"webchat\",\"tenant_id\":\"T1\"}" | python3 -m json.tool
```

### 4i. Clinical Alarms

```bash
# List active alarms for P003 (deteriorating patient — should have 3 alarms)
curl -s -X POST http://localhost:3000/chat \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"message":"Show active alarms for P003","user_id":"doc1","channel":"webchat","tenant_id":"T1"}' | python3 -m json.tool
```

### 4j. Threshold Update (critical tool — requires confirmation)

```bash
# Request threshold update
RESP=$(curl -s -X POST http://localhost:3000/chat \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"message":"Update systolic BP threshold for P001 to 90","user_id":"doc1","channel":"webchat","tenant_id":"T1"}')
echo "$RESP" | python3 -m json.tool

# Extract confirmation_id and confirm
CID=$(echo "$RESP" | python3 -c "import sys,json,re; m=re.search(r'Confirmation ID: ([a-f0-9-]+)', json.load(sys.stdin)['response']); print(m.group(1) if m else '')")
curl -s -X POST "http://localhost:3000/confirm/$CID" \
  -H "Authorization: Bearer $API_KEY" | python3 -m json.tool
```

### 4k. Conditions / Comorbidities

```bash
curl -s -X POST http://localhost:3000/chat \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"message":"What are allergies and comorbidities for P003?","user_id":"doc1","channel":"webchat","tenant_id":"T1"}' | python3 -m json.tool
```

Expected: allergies from EHR + conditions (sepsis, AKI, COPD) from monitoring.

### 4l. Care Plan (fan-out)

```bash
curl -s -X POST http://localhost:3000/chat \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"message":"What are the next tasks for P001?","user_id":"doc1","channel":"webchat","tenant_id":"T1"}' | python3 -m json.tool
```

Fan-out aggregates: orders + appointments + reminders + pending labs. `care_plan_summary` skill summarizes.

### 4m. Ward Risk Ranking

```bash
curl -s -X POST http://localhost:3000/chat \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"message":"Order patients in ICU-A by criticality","user_id":"doc1","channel":"webchat","tenant_id":"T1"}' | python3 -m json.tool
```

Returns patients sorted by NEWS2 score (highest risk first).

### 4n. Doctor Resolution + Appointment

```bash
curl -s -X POST http://localhost:3000/chat \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"message":"Schedule appointment with Dr Patel for patient in bed 5","user_id":"doc1","channel":"webchat","tenant_id":"T1"}' | python3 -m json.tool
```

Flow: resolve_bed → resolve_doctor → schedule_appointment → service_orchestration skill formats result.

### 4o. Skip Analysis (raw data only)

```bash
curl -s -X POST http://localhost:3000/chat \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"message":"Show X-ray report for P001, no analysis needed","user_id":"doc1","channel":"webchat","tenant_id":"T1"}' | python3 -m json.tool
```

LLM adds `skip_analysis: true` to tool params; skill interpretation is skipped.

---

## 5. Pre-Live Validation Checklist

Run through these checks before going live to confirm all subsystems work:

```bash
API_KEY="your-webchat-key"

# 1. Health check — all backends "ok"
curl -s http://localhost:3000/health | python3 -m json.tool

# 2. Auth — 401 without token, 200 with token
curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:3000/chat \
  -H 'Content-Type: application/json' -d '{"message":"hi","user_id":"doc1","tenant_id":"T1"}'

# 3. Vitals + skill interpretation (monitoring + MedGemma)
curl -s -X POST http://localhost:3000/chat \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"message":"Show vitals for P001","user_id":"doc1","channel":"webchat","tenant_id":"T1"}' \
  | python3 -c "import sys,json; r=json.load(sys.stdin); print('OK' if 'heart' in r['response'].lower() or 'vital' in r['response'].lower() else 'FAIL:', r['response'][:100])"

# 4. Labs + interpretation (LIS + skill)
curl -s -X POST http://localhost:3000/chat \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"message":"Lab results for P001","user_id":"doc1","channel":"webchat","tenant_id":"T1"}' \
  | python3 -c "import sys,json; r=json.load(sys.stdin); print('OK' if r['response'] else 'FAIL')"

# 5. Alarms (monitoring)
curl -s -X POST http://localhost:3000/chat \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"message":"Active alarms for P003","user_id":"doc1","channel":"webchat","tenant_id":"T1"}' \
  | python3 -c "import sys,json; r=json.load(sys.stdin); print('OK' if 'alarm' in r['response'].lower() else 'FAIL:', r['response'][:100])"

# 6. Care plan fan-out (gateway + EHR + patient-services + LIS)
curl -s -X POST http://localhost:3000/chat \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"message":"What are next tasks for P001?","user_id":"doc1","channel":"webchat","tenant_id":"T1"}' \
  | python3 -c "import sys,json; r=json.load(sys.stdin); print('OK' if r['response'] else 'FAIL')"

# 7. Ward risk ranking (gateway fan-out)
curl -s -X POST http://localhost:3000/chat \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"message":"Rank patients in ICU-A by risk","user_id":"doc1","channel":"webchat","tenant_id":"T1"}' \
  | python3 -c "import sys,json; r=json.load(sys.stdin); print('OK' if r['response'] else 'FAIL')"

# 8. Standalone skill tests (rule-based, no backends needed)
python scripts/test_skill.py risk_scoring --tool get_vitals \
  --mock '{"heart_rate":135,"bp_systolic":85,"spo2":90,"respiration_rate":26}' \
  --patient P003 --demo default 2>&1 | grep -q "high" && echo "OK: risk_scoring" || echo "FAIL: risk_scoring"

python scripts/test_skill.py medication_validation --tool check_drug_interactions \
  --mock '{"interactions":[]}' --params '{"medications":["warfarin","aspirin"]}' 2>&1 \
  | grep -q "bleeding" && echo "OK: medication_validation" || echo "FAIL: medication_validation"

# 9. Prometheus metrics
curl -s http://localhost:3000/metrics | grep -c '^hobot_' && echo "OK: metrics exported"

echo "--- Pre-live validation complete ---"
```

---

## 6. Tracing Requests Through the System

Every request gets an 8-char `request_id` that appears in:
- All structured JSON log lines
- The audit database (`request_id` column)
- `X-Request-ID` header sent to backend services

### 5a. Watch Live Logs (Structured JSON)

```bash
# All gateway logs (JSON formatted)
docker compose logs -f clinibot-gateway --since 1m

# Pretty-print with jq (if installed)
docker compose logs -f clinibot-gateway --since 1m 2>&1 | grep -o '{.*}' | jq .
```

### 5b. Trace a Single Request

Send a request and note the `request_id` from the logs:

```bash
# In terminal 1: watch logs
docker compose logs -f clinibot-gateway --since 1m 2>&1 | grep -o '{.*}' | jq -r '[.request_id, .name, .message] | @tsv'

# In terminal 2: send request
curl -s -X POST http://localhost:3000/chat \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"message":"Show vitals for P001","user_id":"doc1","channel":"webchat","tenant_id":"T1"}' > /dev/null
```

Sample log trace for a vitals query:

```
request_id  logger                    message
a1b2c3d4    clinibot.orchestrator     [sess-id] orchestrator iteration=0 provider=gemini
a1b2c3d4    clinibot.orchestrator     [sess-id] tool_calls: ['get_vitals']
a1b2c3d4    clinibot.tools            [sess-id] dispatch GET http://synthetic-monitoring:8000/vitals/P001
a1b2c3d4    clinibot.tools            [sess-id] dispatch get_vitals -> 200
a1b2c3d4    clinibot.orchestrator     [sess-id] orchestrator iteration=1 provider=gemini
```

### 5c. Query Audit DB by Request ID

```bash
# Find all actions for a specific request
docker compose exec audit-db sqlite3 /data/clinic.db \
  "SELECT datetime(timestamp), action, tool_name, latency_ms, request_id
   FROM audit_log WHERE request_id='a1b2c3d4' ORDER BY id;"
```

### 5d. Common Audit Queries

```bash
# Last 10 requests with latency
docker compose exec audit-db sqlite3 -header -column /data/clinic.db \
  "SELECT request_id, action, tool_name, latency_ms, datetime(timestamp)
   FROM audit_log ORDER BY id DESC LIMIT 10;"

# Slow requests (> 5 seconds)
docker compose exec audit-db sqlite3 -header -column /data/clinic.db \
  "SELECT request_id, action, provider, latency_ms, datetime(timestamp)
   FROM audit_log WHERE latency_ms > 5000 AND action='chat_response' ORDER BY latency_ms DESC LIMIT 10;"

# Tool call frequency
docker compose exec audit-db sqlite3 -header -column /data/clinic.db \
  "SELECT tool_name, COUNT(*) as calls FROM audit_log
   WHERE tool_name IS NOT NULL GROUP BY tool_name ORDER BY calls DESC;"

# Provider usage and avg latency
docker compose exec audit-db sqlite3 -header -column /data/clinic.db \
  "SELECT provider, COUNT(*) as calls, ROUND(AVG(latency_ms)) as avg_ms
   FROM audit_log WHERE action='chat_response' AND provider IS NOT NULL
   GROUP BY provider;"

# Clinical facts for a patient
docker compose exec audit-db sqlite3 -header -column /data/clinic.db \
  "SELECT fact_type, source_tool, datetime(recorded_at), datetime(expires_at)
   FROM clinical_facts WHERE patient_id='P001' ORDER BY id DESC LIMIT 10;"

# Escalations
docker compose exec audit-db sqlite3 -header -column /data/clinic.db \
  "SELECT e.id, e.escalated_to, e.reason, datetime(a.timestamp)
   FROM escalations e JOIN audit_log a ON e.audit_log_id = a.id
   ORDER BY e.id DESC LIMIT 10;"
```

### 5e. Prometheus Metrics

```bash
# Raw metrics
curl -s http://localhost:3000/metrics

# Key metrics to watch
curl -s http://localhost:3000/metrics | grep -E '^hobot_(requests|tool_calls|llm_calls)_total'
curl -s http://localhost:3000/metrics | grep -E '^hobot_(request|tool_call|llm_call)_duration_seconds'
```

### 5f. Session Files

```bash
# List session files
docker compose exec clinibot-gateway ls -la /data/sessions/T1/ 2>/dev/null || echo "No sessions yet"

# Read a session transcript (PHI-redacted on disk)
docker compose exec clinibot-gateway cat /data/sessions/T1/<session-id>.jsonl 2>/dev/null | python3 -m json.tool
```

---

## 7. Rerun / Restart Sequences

### After code changes

```bash
docker compose up -d --build clinibot-gateway
```

### After config changes only

```bash
docker compose restart clinibot-gateway
```

### After `.env` changes

Environment variables require container recreation (restart is not enough):
```bash
docker compose up -d --force-recreate clinibot-gateway
```

### Full rebuild (clean)

```bash
docker compose down
docker compose up -d --build
```

### Reset data (sessions + audit)

```bash
docker compose down
docker volume rm hobot_audit-data hobot_sessions-data
docker compose up -d
```

### Restart a crashed backend

```bash
docker compose restart synthetic-monitoring
# Wait 10s, then check
curl -s http://localhost:3000/health | python3 -m json.tool
```

---

## 8. Troubleshooting

| Symptom | Diagnosis | Fix |
|---------|-----------|-----|
| `401` on all requests | API_KEYS not set or wrong key | Check `.env`, `echo $API_KEYS` |
| `"status":"degraded"` | One or more backends down | `docker compose ps`, restart failed service |
| `Provider returned None` | LLM API key invalid or quota hit | Check API key, try different provider |
| `429 Rate limit exceeded` | Too many requests | Wait, or increase `per_user.requests` in config.json |
| `Confirmation expired` | Took > 5 min to confirm | Re-request the critical action |
| `Client mismatch` on confirm | Different API key used to confirm | Use same API key as the original request |
| No tool calls in response | Classifier picked wrong domains | Check logs for `classified domains=`, adjust `domain_keywords` |
| Slow responses (10s+) | Using local Ollama for orchestration | Set `agents.defaults.provider` to `gemini` or `anthropic` |
| `Backend unreachable` | Synthetic service not running | `docker compose up -d synthetic-<name>` |
| Import errors in gateway | Dockerfile missing files | Rebuild: `docker compose build clinibot-gateway` |
| Skill returns `partial (provider_unavailable)` | Ollama model not pulled or cold | Pull model: `docker compose exec ollama ollama pull <model>`, then warm it |
| `.env` changes not taking effect | `docker compose restart` doesn't reload env | Use `docker compose up -d --force-recreate clinibot-gateway` |
| Ollama 404 on `/api/chat` | Model not found in Ollama | `docker compose exec ollama ollama list` to check; pull if missing |
| First Ollama call times out | GPU model cold-load takes ~60-90s | Warm up: `docker compose exec ollama ollama run <model> "test"` |

### Log levels

Set `LOG_LEVEL` for more detail:
```bash
# In docker-compose.yml under clinibot-gateway environment:
- LOG_LEVEL=DEBUG
```

---

## 9. Architecture Quick Reference

```
Request Flow:
  Client
    → Auth Middleware (Bearer token)
    → Rate Limiter (per-client + per-user)
    → /chat endpoint (sets request_id)
    → Classifier (regex → LLM → fallback)
    → Orchestrator loop (max 10 iterations):
        → LLM Provider (Gemini/Anthropic/Ollama)
        → Tool dispatch (parallel, X-Request-ID header)
        → Skill auto-fire (interpret results)
        → Audit log (request_id stored)
    → Response formatter (channel-specific blocks)
  Client ←

Background:
  _cleanup_loop (every 30 min): expired facts, confirmations, stale sessions
  _reminder_loop (every 30s): poll patient-services, push via Telegram
```

### Containers

```
Gateway:     clinibot-gateway (:3000)
Channel:     telegram-bot
Models:      ollama (:11435 on host, GPU — runs MedGemma + OpenBioLLM)
MCP (8):     mcp-{ehr,monitoring,radiology,lis,pharmacy,bloodbank,erp,patient-services}
Backends (8): synthetic-{ehr,monitoring,radiology,lis,pharmacy,bloodbank,erp,patient-services}
Audit:       audit-db (internal only)
```

### Data Volumes

| Volume | Content | Survives `docker compose down` |
|--------|---------|-------------------------------|
| `audit-data` | SQLite audit DB + clinical facts | Yes |
| `sessions-data` | JSONL session transcripts | Yes |
| `ollama-models` | LLM model weights | Yes |
| `hdf5-data` | Synthetic vitals/ECG data | Yes |
