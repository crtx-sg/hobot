# Hobot — Requirements Specification (BDD / OpenAPI)

## 1. Product Overview

Hobot is a conversational clinical AI assistant that unifies eight hospital backend systems behind a single chat API. Clinicians interact via natural language through channel apps (Telegram, webchat, Slack, WhatsApp). The gateway orchestrates LLM-driven intent detection, multi-tool dispatch, clinical safety gates, and structured response synthesis.

---

## 2. Stakeholders

| Role | Description |
|------|-------------|
| Nurse / Clinician | Primary user — queries vitals, meds, labs, orders diets, initiates code blue |
| Attending Physician | Reviews ward rounds, writes orders, escalates cases |
| Hospital Admin | Monitors audit trail, manages tenants |
| System Integrator | Replaces synthetic backends with real hospital systems |

---

## 3. Feature Specifications (BDD)

### F1: Natural Language Chat

```gherkin
Feature: Natural Language Clinical Queries
  As a clinician
  I want to ask clinical questions in plain language
  So that I can get patient data without navigating multiple systems

  Scenario: Simple vitals query
    Given the gateway is running with a healthy LLM provider
    When I POST /chat with message "Show vitals for P001"
    Then the response status is 200
    And the response contains patient vitals (HR, BP, SpO2, Temp)
    And the response includes structured blocks of type "data_table"

  Scenario: Bed-based query with auto-resolution
    Given patient P002 is assigned to BED2
    When I POST /chat with message "meds for bed 2"
    Then the gateway calls resolve_bed with bed_id "BED2"
    And then calls get_medications with the resolved patient_id
    And returns a medication list

  Scenario: Multi-tool query
    Given the LLM provider supports native function calling
    When I POST /chat with message "compare vitals and meds for P003"
    Then the LLM returns two tool_calls in a single response
    And both tools are dispatched in parallel via asyncio.gather
    And the LLM synthesizes a comparison from both results

  Scenario: No LLM provider available
    Given all configured providers are unhealthy
    When I POST /chat with message "vitals for P001"
    Then the gateway falls back to keyword-based intent detection
    And dispatches get_vitals(patient_id="P001")
    And returns a text-formatted summary
```

### F2: Session Persistence & Memory Consolidation

```gherkin
Feature: Conversation Continuity
  As a clinician
  I want my conversation history preserved across gateway restarts
  So that I can continue where I left off

  Scenario: Session persists across restart
    Given I have an active session "sess-123"
    When the gateway restarts
    And I POST /chat with session_id "sess-123"
    Then the gateway loads the session from disk
    And previous context is available

  Scenario: Memory consolidation
    Given a session has accumulated 30+ messages
    When a new message is received
    Then the agent summarizes older messages into a clinical summary
    And the 10 most recent messages are kept verbatim
    And the summary is prepended to the LLM context

  Scenario: Multi-tenant isolation
    Given tenant "hospital-A" has session "sess-A"
    And tenant "hospital-B" has session "sess-B"
    Then sessions are stored in separate directories
    And clinical facts are partitioned by tenant_id
```

### F3: Streaming Responses (SSE)

```gherkin
Feature: Real-time Streaming
  As a web client developer
  I want to receive agent progress in real time
  So that I can show tool execution status to the user

  Scenario: Streaming tool chain
    When I POST /chat/stream with message "Show vitals for P001"
    Then I receive SSE event type "tool_call" with tool "get_vitals"
    And then SSE event type "tool_result" with vitals data
    And then SSE event type "text" with the synthesized response
    And finally SSE event type "done" with the session_id
```

### F4: Multi-Provider LLM Routing

```gherkin
Feature: Provider Fallback Chain
  As the system
  I want to automatically failover between LLM providers
  So that the service remains available during provider outages

  Scenario: Default provider healthy
    Given the default provider is "anthropic" and it is healthy
    When a chat request arrives
    Then the request is routed to the Anthropic Messages API

  Scenario: Default provider rate-limited
    Given the default provider "anthropic" returns 429
    And provider "gemini" is healthy
    When the retry budget (2 retries with backoff) is exhausted
    Then the gateway marks anthropic as unhealthy
    And routes to gemini as fallback

  Scenario: Provider fails mid-synthesis
    Given the LLM has already collected tool results
    And the synthesis call returns 429 after retries
    Then the gateway tries a fallback provider for synthesis
    And tool results are never discarded

  Scenario: All providers down
    Given no configured provider passes health check
    Then the gateway falls back to keyword regex intent detection
    And tool results are formatted using built-in text formatters
```

### F5: Critical Tool Confirmation

```gherkin
Feature: Human-in-the-Loop for Critical Actions
  As a clinician
  I want dangerous actions to require explicit confirmation
  So that accidental orders are prevented

  Scenario: Code blue requires confirmation
    When I POST /chat with message "code blue for P001"
    Then the tool is NOT executed
    And the response contains a confirmation block
    And the block includes a confirmation_id
    And the Telegram bot renders a "Confirm" inline button

  Scenario: Confirming a critical action
    Given a pending confirmation with id "conf-abc"
    When I POST /confirm/conf-abc
    Then the tool is executed against the backend
    And the result is returned
    And the confirmation is removed from pending

  Scenario: Critical tools list
    Then the following tools require confirmation:
      | Tool                    |
      | initiate_code_blue      |
      | write_order             |
      | order_blood_crossmatch  |
      | dispense_medication     |
      | request_ambulance       |
      | order_lab               |
```

### F6: PHI Redaction

```gherkin
Feature: Protected Health Information Safety
  As the system
  I want to redact PHI before sending to non-PHI-safe providers
  So that patient data is not leaked to external APIs

  Scenario: Redaction for cloud providers
    Given provider "gemini" has phi_safe=false
    When messages are sent to gemini
    Then patient IDs (P001), MRNs, dates, phone numbers are replaced with tokens
    And the LLM response tokens are restored before returning to the user

  Scenario: No redaction for local models
    Given provider "ollama" has phi_safe=true
    When messages are sent to ollama
    Then no redaction is applied

  Scenario: Audit logs never contain raw PHI
    When a tool call is logged to the audit table
    Then params are SHA-256 hashed
    And result_summary is redacted
```

### F7: Clinical Memory & Fact Extraction

```gherkin
Feature: Structured Clinical Fact Storage
  As the system
  I want to extract and store structured facts from every tool result
  So that clinical context is available without re-querying backends

  Scenario: Vitals extracted as facts
    When get_vitals returns data for P001
    Then a fact of type "vitals" is stored in clinical_facts
    And it includes HR, BP, SpO2, temperature

  Scenario: Facts injected into LLM context
    Given patient P001 has 5 stored facts
    When a new query mentions P001
    Then the agent prepends known facts to the system prompt
```

### F8: Audit Trail

```gherkin
Feature: Immutable Audit Logging
  As a hospital admin
  I want every clinical action logged immutably
  So that I have a complete audit trail for compliance

  Scenario: Tool call audit
    When tool get_vitals is called for P001
    Then an audit_log entry is created with action="tool_call"
    And params_hash contains SHA-256 of the parameters
    And latency_ms records the execution time

  Scenario: Escalation tracking
    When a case is escalated to "DR-SMITH"
    Then an escalation record is created
    And it links to the originating audit_log entry
    And it can be resolved with a resolution note
```

### F9: Structured Rich Responses

```gherkin
Feature: Channel-Aware Block Rendering
  As a channel client
  I want structured UI blocks alongside plain text
  So that I can render data natively per platform

  Scenario: Telegram renders data tables as HTML
    Given the channel is "telegram"
    When get_vitals returns a data_table block
    Then the block is rendered as HTML with <b> tags
    And action buttons become InlineKeyboardMarkup

  Scenario: Webchat passes blocks through
    Given the channel is "webchat"
    When blocks are generated
    Then they are returned as-is for frontend rendering

  Scenario: Unsupported blocks are filtered
    Given the channel is "whatsapp"
    And whatsapp does not support "actions" blocks
    When blocks include an "actions" block
    Then it is stripped from the response
```

### F10: Ward Rounds Report

```gherkin
Feature: Multi-Service Ward Rounds
  As an attending physician
  I want a severity-sorted ward summary
  So that I can prioritize my rounds

  Scenario: Ward rounds with fan-out
    When I request "rounds report for ICU-A"
    Then the gateway fetches from monitoring, EHR, and radiology in parallel
    And returns patients sorted by NEWS score descending
    And each patient includes: vitals, medications, latest scan
    And patients with NEWS >= 5 get an alert block
```

### F11: ECG Data Retrieval

```gherkin
Feature: ECG Waveform Access
  As a clinician
  I want to view the latest ECG for a patient
  So that I can assess cardiac status without an event ID

  Scenario: Latest ECG
    When I request "Get latest ECG for P001"
    Then the gateway calls get_latest_ecg(patient_id="P001")
    And returns waveform data with leads, sampling rate, duration
    And a waveform block is generated for rendering

  Scenario: Event-specific ECG
    When I request "ECG event EVT-001 for P001"
    Then the gateway calls get_event_ecg(event_id="EVT-001", patient_id="P001")
```

### F12: Timed Reminders

```gherkin
Feature: Clinical Reminders
  As a nurse
  I want to set timed reminders from chat
  So that I don't forget time-sensitive tasks

  Scenario: Reminder fires via Telegram push
    When I say "remind me in 2 hours to turn patient in bed 3"
    Then a reminder is scheduled with delay_minutes=120
    And after 2 hours, the bot pushes a notification to my Telegram chat
```

---

## 4. API Specification (OpenAPI 3.1)

```yaml
openapi: 3.1.0
info:
  title: Hobot Clinical Gateway API
  version: 1.0.0
  description: >
    Conversational clinical AI gateway that orchestrates hospital backend
    systems via LLM-driven tool dispatch.

servers:
  - url: http://localhost:3000
    description: Local development

paths:
  /chat:
    post:
      summary: Send a chat message
      operationId: chat
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/ChatRequest'
      responses:
        '200':
          description: Chat response with optional structured blocks
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ChatResponse'

  /chat/stream:
    post:
      summary: Send a chat message (streaming SSE)
      operationId: chatStream
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/ChatRequest'
      responses:
        '200':
          description: Server-Sent Events stream
          content:
            text/event-stream:
              schema:
                type: string
                description: >
                  SSE events: tool_call, tool_result, text, done

  /confirm/{confirmation_id}:
    post:
      summary: Execute a pending critical tool
      operationId: confirmAction
      parameters:
        - name: confirmation_id
          in: path
          required: true
          schema:
            type: string
            format: uuid
      responses:
        '200':
          description: Tool execution result
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ConfirmResponse'

  /health:
    get:
      summary: Backend health check
      operationId: health
      responses:
        '200':
          description: Health status of all backends
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/HealthResponse'

components:
  schemas:
    ChatRequest:
      type: object
      required: [message, user_id]
      properties:
        message:
          type: string
          description: Natural language query
        user_id:
          type: string
          description: Clinician identifier
        channel:
          type: string
          enum: [webchat, telegram, slack, whatsapp]
          default: webchat
        tenant_id:
          type: string
          default: default
        session_id:
          type: string
          nullable: true
          description: Omit to start new session

    ChatResponse:
      type: object
      required: [response, session_id]
      properties:
        response:
          type: string
          description: Plain text response (always present)
        session_id:
          type: string
        blocks:
          type: array
          nullable: true
          items:
            $ref: '#/components/schemas/Block'
          description: Structured UI blocks for rich rendering

    ConfirmResponse:
      type: object
      properties:
        result:
          type: object
          description: Tool execution result

    HealthResponse:
      type: object
      properties:
        status:
          type: string
          enum: [ok, degraded]
        service:
          type: string
        backends:
          type: object
          additionalProperties:
            type: string
            enum: [ok, error]

    Block:
      type: object
      required: [type]
      properties:
        type:
          type: string
          enum:
            - data_table
            - key_value
            - alert
            - text
            - actions
            - confirmation
            - image
            - chart
            - waveform
            - rendered_image

    SSEEvent:
      type: object
      properties:
        type:
          type: string
          enum: [tool_call, tool_result, text, done]
        tool:
          type: string
        status:
          type: string
        data:
          type: object
        content:
          type: string
        session_id:
          type: string
```

---

## 5. Tool Inventory

| # | Tool | Domain | Critical | Parameters |
|---|------|--------|----------|------------|
| 1 | get_vitals | Monitoring | No | patient_id |
| 2 | get_vitals_history | Monitoring | No | patient_id |
| 3 | get_vitals_trend | Monitoring | No | patient_id, hours? |
| 4 | list_wards | Monitoring | No | — |
| 5 | list_doctors | Monitoring | No | — |
| 6 | get_ward_patients | Monitoring | No | ward_id |
| 7 | get_doctor_patients | Monitoring | No | doctor_id |
| 8 | get_patient_events | Monitoring | No | patient_id |
| 9 | get_event_vitals | Monitoring | No | event_id |
| 10 | get_event_ecg | Monitoring | No | event_id |
| 11 | get_latest_ecg | Monitoring | No | patient_id |
| 12 | initiate_code_blue | Monitoring | **Yes** | patient_id |
| 13 | get_patient | EHR | No | patient_id |
| 14 | get_medications | EHR | No | patient_id |
| 15 | get_allergies | EHR | No | patient_id |
| 16 | get_orders | EHR | No | patient_id |
| 17 | write_order | EHR | **Yes** | patient_id |
| 18 | get_studies | Radiology | No | patient_id |
| 19 | get_report | Radiology | No | study_id |
| 20 | get_latest_study | Radiology | No | patient_id |
| 21 | get_lab_results | LIS | No | patient_id |
| 22 | get_lab_order | LIS | No | order_id |
| 23 | order_lab | LIS | **Yes** | patient_id, test_code, priority? |
| 24 | get_order_status | LIS | No | order_id |
| 25 | check_drug_interactions | Pharmacy | No | drugs |
| 26 | dispense_medication | Pharmacy | **Yes** | patient_id, medication_id |
| 27 | get_blood_availability | Blood Bank | No | — |
| 28 | order_blood_crossmatch | Blood Bank | **Yes** | patient_id, blood_type |
| 29 | get_crossmatch_status | Blood Bank | No | request_id |
| 30 | get_inventory | ERP | No | — |
| 31 | get_equipment_status | ERP | No | equipment_id |
| 32 | place_supply_order | ERP | No | item, quantity |
| 33 | request_housekeeping | Patient Svc | No | room, request_type, priority? |
| 34 | order_diet | Patient Svc | No | patient_id, diet_type, meal, restrictions? |
| 35 | request_ambulance | Patient Svc | **Yes** | patient_id |
| 36 | get_request_status | Patient Svc | No | request_id |
| 37 | get_ward_rounds | Gateway | No | ward_id |
| 38 | resolve_bed | Gateway | No | bed_id |
| 39 | schedule_appointment | Gateway | No | patient_id, doctor, datetime, notes? |
| 40 | set_reminder | Gateway | No | message, delay_minutes |
| 41 | escalate | Gateway | No | patient_id, escalate_to, reason |

---

## 6. Non-Functional Requirements

| Requirement | Target |
|-------------|--------|
| Response latency (cloud LLM) | < 5s for single-tool queries |
| Response latency (local Ollama) | < 15s for single-tool queries |
| Concurrent sessions | 100+ (limited by LLM throughput) |
| Session persistence | Survives gateway restarts |
| Audit completeness | 100% of tool calls logged |
| PHI leakage to cloud APIs | Zero (redacted before transmission) |
| Critical tool false execution | Zero (gated behind /confirm) |
| Provider failover time | < 5s (health cache TTL + retry backoff) |
| Retry budget (429/5xx) | 2 retries, 1s + 3s exponential backoff |
