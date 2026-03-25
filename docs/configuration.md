# Configuration Reference

All config files live in `config/` and are mounted into the gateway at `/app/config`.

---

## `config/config.json`

Master configuration file containing providers, skills, classifier, clinical memory, and rate limits.

### Providers

Defines LLM providers. The default is Anthropic Claude.

```json
{
  "providers": {
    "anthropic": {
      "baseUrl": "https://api.anthropic.com",
      "apiKey": "${ANTHROPIC_API_KEY}",
      "model": "claude-sonnet-4-20250514",
      "phi_safe": false,
      "timeout": 30.0,
      "supports_vision": true
    },
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
      "model": "glm-5:cloud",
      "phi_safe": true
    },
    "medgemma": {
      "type": "ollama",
      "baseUrl": "http://ollama:11434",
      "model": "hf.co/SandLogicTechnologies/MedGemma-4B-IT-GGUF:Q4_K_M",
      "phi_safe": true,
      "timeout": 60.0
    },
    "openbiollm": {
      "type": "ollama",
      "baseUrl": "http://ollama:11434",
      "model": "koesn/llama3-openbiollm-8b",
      "phi_safe": true,
      "timeout": 60.0
    }
  },
  "agents": {
    "defaults": {
      "model": "claude-sonnet-4-20250514",
      "provider": "anthropic"
    }
  }
}
```

| Field | Description |
|-------|-------------|
| `baseUrl` | API base URL (path appended per provider type) |
| `apiKey` | API key. Supports `${ENV_VAR}` expansion |
| `model` | Model name/ID sent to the provider |
| `type` | Explicit provider class: `ollama`, `gemini`, `anthropic`, `openai`. Auto-detected from name/URL if omitted |
| `phi_safe` | If `false`, PHI is redacted before sending. Local models (Ollama) are PHI-safe |
| `timeout` | Request timeout in seconds (default: 60) |
| `supports_vision` | Enables image input for radiology image analysis |

**Provider types:**

| Type | Endpoint | Native Tools | Notes |
|------|----------|--------------|-------|
| `anthropic` | `/v1/messages` (native Messages API) | Yes | Cloud, PHI redaction active |
| `gemini` | `/chat/completions` (OpenAI-compat) | Yes | Cloud, PHI redaction active |
| `ollama` | `/api/generate` (single prompt) or `/api/chat` (multi-turn) | Domain models only | Local, PHI-safe |
| `openai` | `/v1/chat/completions` | Yes | Any OpenAI-compatible server (vLLM, TGI, etc.) |

**Clinical models (Ollama):**

| Model | HuggingFace | Size (Q4) | Use |
|-------|-------------|-----------|-----|
| MedGemma 4B-IT | `google/medgemma-4b-it` | ~2.5 GB | Clinical reasoning, radiology interpretation |
| OpenBioLLM 8B | `aaditya/Llama3-OpenBioLLM-8B` | ~4.9 GB | Alternative clinical reasoning |

Pull via: `docker compose exec ollama ollama pull <model-name>`

`agents.defaults.provider` selects the default for orchestration. If unhealthy, the gateway tries others in config order. Domain models use separate provider config under `skills`.

### Skills

Configures the skills framework and domain models.

```json
{
  "skills": {
    "interpret_vitals": {
      "provider": "gemini",
      "timeout": 30,
      "enabled": true
    },
    "interpret_labs": {
      "provider": "gemini",
      "timeout": 30,
      "enabled": true
    },
    "interpret_ecg": {
      "provider": "ollama",
      "timeout": 30,
      "enabled": true
    },
    "analyze_radiology_image": {
      "provider": "gemini",
      "timeout": 20,
      "enabled": true
    },
    "medication_validation": {
      "timeout": 10,
      "enabled": true
    },
    "care_plan_summary": {
      "provider": "medgemma",
      "timeout": 20,
      "enabled": true
    },
    "clinical_reasoning": {
      "provider": "medgemma"
    },
    "radiology_model": {
      "provider": "medgemma"
    }
  }
}
```

| Field | Description |
|-------|-------------|
| `provider` | LLM provider for this skill's domain model (overrides default) |
| `timeout` | Skill timeout in seconds (30s recommended for local models) |
| `enabled` | Set `false` to disable a skill (raw data passes through) |
| `scoring` | Scoring system: `"news2"` (default) or `"mews"` — for interpret_vitals |
| `thresholds` | Hospital-level vital sign thresholds — for interpret_vitals |

**Domain model config** (under `skills` section):

| Key | Controls |
|-----|----------|
| `clinical_reasoning.provider` | Provider for general clinical interpretation (default: `medgemma`) |
| `radiology_model.provider` | Provider for vision-based image analysis (default: `medgemma`) |

Rule-based models (`vitals_anomaly`, `drug_interaction`) have no provider config — they run locally.

**Vitals threshold config** (under `skills.interpret_vitals.thresholds`):

```json
{
  "thresholds": {
    "heart_rate":       {"low": 60, "high": 100, "critical_low": 40, "critical_high": 150},
    "bp_systolic":      {"low": 90, "high": 140, "critical_low": 70, "critical_high": 220},
    "spo2":             {"low": 94, "critical_low": 90},
    "temperature":      {"low": 36.1, "high": 38.0, "critical_low": 35.0, "critical_high": 39.5},
    "respiration_rate": {"low": 12, "high": 20, "critical_low": 8, "critical_high": 30}
  }
}
```

These are hospital defaults. Patient-specific thresholds are fetched from the monitoring backend and cached in clinical memory (TTL: 168h). Per-request overrides via tool params take highest priority.

### Classifier

3-tier intent classifier (regex -> LLM -> fallback).

```json
{
  "classifier": {
    "provider": "gemini",
    "model": "gemini-2.5-flash",
    "timeout": 2,
    "fallback_domains": ["core", "vitals", "labs", "medications", "ward", "emergency"],
    "domain_keywords": {
      "vitals": ["vitals?", "bp", "heart\\s*rate", "spo2"],
      "labs": ["labs?", "cbc", "blood\\s*test"],
      "ecg": ["ecg", "ekg", "rhythm"],
      "radiology": ["x.?ray", "ct\\b", "mri", "scan"],
      "medications": ["medicat", "drug", "allerg"],
      "emergency": ["code\\s*blue", "ambulance", "escalat"]
    }
  }
}
```

### Tool Domains

Maps tools to classification domains. Controls which tools are available per query.

```json
{
  "tool_domains": {
    "core":        ["list_wards", "get_ward_rounds", "get_patient"],
    "vitals":      ["get_vitals", "get_vitals_history", "get_vitals_trend", "get_patient_thresholds",
                    "update_patient_thresholds", "get_active_alarms", "clear_alarm", "analyze_vitals"],
    "labs":        ["get_lab_results", "analyze_lab_results"],
    "medications": ["get_medications", "get_allergies", "get_conditions", "dispense_medication",
                    "check_drug_interactions"],
    "ecg":         ["get_latest_ecg", "get_event_ecg", "analyze_ecg"],
    "radiology":   ["get_latest_study", "get_report", "get_studies", "analyze_radiology_image"],
    "orders":      ["write_order", "get_orders", "order_lab", "get_lab_order", "get_order_status",
                    "order_blood_crossmatch", "get_crossmatch_status", "get_blood_availability",
                    "order_diet", "get_care_plan"],
    "emergency":   ["initiate_code_blue", "request_ambulance", "escalate"],
    "services":    ["request_housekeeping", "get_request_status"],
    "scheduling":  ["schedule_appointment", "set_reminder"],
    "supplies":    ["get_inventory", "get_equipment_status", "place_supply_order"],
    "ward":        ["resolve_bed", "resolve_doctor", "list_doctors", "get_ward_patients",
                    "get_doctor_patients", "get_patient_events", "get_event_vitals",
                    "get_ward_risk_ranking"]
  }
}
```

New tools since initial release:
- `get_patient_thresholds` / `update_patient_thresholds`† — patient-specific vital sign thresholds
- `get_active_alarms` / `clear_alarm`† — clinical alarm management
- `get_conditions` — patient comorbidities (ICD-10)
- `get_care_plan`* — aggregated orders + appointments + reminders + pending labs
- `get_ward_risk_ranking`* — patients sorted by NEWS2 score
- `resolve_doctor` — fuzzy doctor name → ID resolution

†Critical tool (requires confirmation). *Gateway fan-out tool.

### Clinical Memory

TTL-based fact expiry and cleanup.

```json
{
  "clinical_memory": {
    "ttl_hours": {
      "vitals": 4, "vitals_trend": 4,
      "medication": 24, "allergy": 168,
      "demographics": 168, "lab_result": 12,
      "ecg": 8, "alarm": 1, "condition": 168,
      "vitals_thresholds": 168,
      "imaging_study": 168, "radiology_report": 168,
      "lab_interpretation": 12, "ecg_interpretation": 8,
      "vitals_interpretation": 4, "radiology_interpretation": 168
    },
    "cleanup_interval_minutes": 30
  }
}
```

### Rate Limits

Per-client sliding-window rate limiting. The rate limiter keys on the authenticated `client_id` set by the auth middleware (not on body-parsed `user_id`/`tenant_id`). This prevents bypass via spoofed request bodies.

```json
{
  "rate_limits": {
    "per_user": { "requests": 30, "window_seconds": 60 },
    "per_tenant": { "requests": 200, "window_seconds": 60 }
  }
}
```

The `per_user` limit applies per authenticated client identity. The `per_tenant` limit provides a secondary cap per client.

---

## `config/tools.json`

Tool criticality and parameter schemas.

```json
{
  "tools": {
    "initiate_code_blue": {
      "critical": true,
      "params": {
        "patient_id": { "type": "string", "required": true }
      }
    },
    "get_vitals": {
      "critical": false,
      "params": {
        "patient_id": { "type": "string", "required": true }
      }
    },
    "order_lab": {
      "critical": true,
      "params": {
        "patient_id": { "type": "string", "required": true },
        "test_code": { "type": "string", "required": true },
        "priority": { "type": "string", "enum": ["routine", "stat", "urgent"] }
      }
    }
  }
}
```

**Validation rules:**

| Rule | Description |
|------|-------------|
| `type` | `"string"` or `"number"` |
| `required` | Must be present |
| `enum` | Allowed values |
| `pattern` | Regex (matched with `re.match`) |

Tools marked `critical: true` require human confirmation via `POST /confirm/{id}`.

---

## `config/channels.json`

Per-channel rendering capabilities.

```json
{
  "channels": {
    "webchat": {
      "rich_text": true,
      "buttons": true,
      "tables": true,
      "images": true,
      "max_msg_length": null,
      "supported_blocks": ["data_table", "key_value", "alert", "text", "actions", "confirmation", "image", "chart", "waveform"]
    },
    "telegram": {
      "rich_text": true,
      "buttons": true,
      "tables": false,
      "images": true,
      "max_msg_length": 4096,
      "parse_mode": "HTML",
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
| `tables` | `false` converts markdown tables to key:value lines |
| `max_msg_length` | Truncates response text. `null` = no limit |
| `supported_blocks` | Unsupported types stripped from `blocks` array |

---

## Environment Variables

### API Keys & Auth

| Variable | Default | Description |
|----------|---------|-------------|
| `GEMINI_API_KEY` | *(none)* | Gemini provider key |
| `ANTHROPIC_API_KEY` | *(none)* | Anthropic provider key (optional fallback) |
| `HF_TOKEN` | *(none)* | HuggingFace token for gated models (MedGemma). Accept terms first |
| `API_KEYS` | *(none)* | API auth keys, format: `client_id:key,client_id:key`. If empty, auth is disabled |
| `TELEGRAM_BOT_TOKEN` | *(none)* | Telegram bot + reminders |
| `TG_BOT_API_KEY` | *(none)* | Telegram bot's API key (must match an entry in `API_KEYS`) |
| `ORTHANC_USER` | `orthanc` | Radiology (Orthanc) username |
| `ORTHANC_PASS` | `orthanc` | Radiology (Orthanc) password |

### Session, Confirmation & Limits

| Variable | Default | Description |
|----------|---------|-------------|
| `SESSION_TTL_HOURS` | `24` | Session expiry in hours |
| `CONFIRMATION_TTL_SECONDS` | `300` | Confirmation expiry in seconds (5 min) |
| `CONSOLIDATION_THRESHOLD` | `30` | Messages before LLM consolidation |
| `MAX_MESSAGE_LENGTH` | `10000` | Maximum chat message length in characters |
| `MAX_SESSION_FILE_BYTES` | `5242880` | Max session JSONL file size (5 MB); oversized files are truncated |
| `PENDING_MAX_SIZE` | `1000` | Max pending confirmations in memory |
| `CORS_ORIGINS` | *(none)* | Comma-separated allowed CORS origins (e.g., `http://localhost:8080`). Empty = no CORS |

### Paths

| Variable | Default | Description |
|----------|---------|-------------|
| `CONFIG_PATH` | `/app/config/config.json` | Master config |
| `TOOLS_CONFIG` | `/app/config/tools.json` | Tool schemas |
| `CHANNELS_CONFIG` | `/app/config/channels.json` | Channel config |
| `AUDIT_DB` | `/data/audit/clinic.db` | SQLite audit DB path |
| `SCHEMA_PATH` | `/app/schema/init.sql` | DB init schema |
| `SESSIONS_DIR` | `/data/sessions` | Session persistence dir |

### Backend URLs

| Variable | Default | Description |
|----------|---------|-------------|
| `MONITORING_BASE` | `http://synthetic-monitoring:8000` | Vitals backend |
| `EHR_BASE` | `http://synthetic-ehr:8080` | EHR/FHIR backend |
| `LIS_BASE` | `http://synthetic-lis:8000` | Lab backend |
| `PHARMACY_BASE` | `http://synthetic-pharmacy:8000` | Pharmacy backend |
| `RADIOLOGY_BASE` | `http://synthetic-radiology:8042` | Radiology backend |
| `BLOODBANK_BASE` | `http://synthetic-bloodbank:8000` | Blood bank backend |
| `ERP_BASE` | `http://synthetic-erp:8000` | ERP/inventory backend |
| `PATIENT_SERVICES_BASE` | `http://synthetic-patient-services:8000` | Patient services |

To point at real hospital systems, change the `*_BASE` variables to production URLs.

---

## Debugging

### Log Trace

```bash
docker compose logs -f clinibot-gateway
```

| Step | Logger | What you see |
|------|--------|--------------|
| Request | `clinibot` | `POST /chat` |
| Provider | `clinibot.orchestrator` | `query="..." provider=anthropic` |
| LLM call | `clinibot.providers` | `anthropic request: msgs=12 tools=41` |
| Tool calls | `clinibot.orchestrator` | `tool_calls: ['get_vitals']` |
| Dispatch | `clinibot.tools` | `dispatch GET .../vitals/P001` |
| Response | `clinibot.orchestrator` | `final response: 245 chars` |

### Audit Queries

```bash
# Recent actions
docker compose exec audit-db sqlite3 /data/clinic.db \
  "SELECT datetime(timestamp), action, tool_name FROM audit_log ORDER BY id DESC LIMIT 10;"

# Clinical facts for a patient
docker compose exec audit-db sqlite3 /data/clinic.db \
  "SELECT fact_type, source_tool, recorded_at FROM clinical_facts WHERE patient_id='P001' ORDER BY id DESC LIMIT 10;"

# Provider usage
docker compose exec audit-db sqlite3 /data/clinic.db \
  "SELECT provider, COUNT(*), AVG(latency_ms) FROM audit_log WHERE action='chat_response' GROUP BY provider;"
```

### Common Issues

| Symptom | Fix |
|---------|-----|
| `401 Missing or invalid Authorization header` | Set `API_KEYS` env var and pass `Authorization: Bearer <key>` header |
| `"status":"degraded"` in `/health` | Check `docker compose ps`, restart failed service |
| Service-unavailable error | Check API keys, provider health |
| `anthropic 429 — retry` | Normal backoff. Switch provider if persistent |
| 10s+ response times | Use cloud provider instead of local Ollama |
| Session not found after restart | Ensure `tenant_id` matches |
| `Confirmation expired` | Confirmations have a 5-min TTL; retry the critical action |
| `Client mismatch` on confirm | The confirming client must use the same API key as the requester |

---

## Known Limitations

| Area | Limitation | Workaround |
|------|-----------|------------|
| Radiology image analysis | MedGemma GGUF lacks vision capability | Use `gemini` or `anthropic` provider for `radiology_model`; or deploy MedGemma via vLLM |
| ECG waveform analysis | Stub skill — returns metadata only, no waveform interpretation | Future: integrate ECG-specific ML model. Interface is structured and ready |
| Reminder delivery | Only works via Telegram channel | No SMS, webchat push, or per-user notification routing yet |
| `skip_analysis` | Requires LLM to add the flag | Add system prompt instruction: "When user says 'no analysis'/'raw only', add skip_analysis: true" |
