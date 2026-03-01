-- Hobot Audit Database Schema
-- SQLite with WAL mode for concurrent reads

PRAGMA journal_mode=WAL;

-- Immutable log of every action
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    action TEXT NOT NULL,
    tool_name TEXT,
    params_hash TEXT,
    result_summary TEXT,
    confirmation_id TEXT,
    template_version TEXT,
    provider TEXT,
    model TEXT,
    latency_ms INTEGER
);

CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_log(tenant_id);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);

-- Escalation tracking
CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    audit_log_id INTEGER REFERENCES audit_log(id),
    escalated_to TEXT NOT NULL,
    reason TEXT,
    resolved_at TEXT,
    resolved_by TEXT,
    resolution TEXT
);

CREATE INDEX IF NOT EXISTS idx_escalations_tenant ON escalations(tenant_id);

-- Clinical facts — structured medical data, never summarized
CREATE TABLE IF NOT EXISTS clinical_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    fact_type TEXT NOT NULL,
    fact_data TEXT NOT NULL,
    source_tool TEXT,
    recorded_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_facts_tenant ON clinical_facts(tenant_id);
CREATE INDEX IF NOT EXISTS idx_facts_session ON clinical_facts(session_id);
CREATE INDEX IF NOT EXISTS idx_facts_patient ON clinical_facts(patient_id);
CREATE INDEX IF NOT EXISTS idx_facts_type ON clinical_facts(patient_id, fact_type);
