"""Audit logger — SQLite-backed immutable action and escalation log."""

import hashlib
import json
import os
from datetime import datetime, timezone

import aiosqlite

AUDIT_DB = os.environ.get("AUDIT_DB", "/data/audit/clinic.db")
SCHEMA_PATH = os.environ.get("SCHEMA_PATH", "/app/schema/init.sql")

_db: aiosqlite.Connection | None = None


async def init_db(db_path: str | None = None, schema_path: str | None = None) -> None:
    """Initialize the audit database, creating tables from schema/init.sql."""
    global _db
    path = db_path or AUDIT_DB
    schema = schema_path or SCHEMA_PATH
    _db = await aiosqlite.connect(path)
    _db.row_factory = aiosqlite.Row
    if os.path.exists(schema):
        with open(schema) as f:
            await _db.executescript(f.read())
    await _db.commit()


async def close_db() -> None:
    global _db
    if _db:
        await _db.close()
        _db = None


def _hash_params(params: dict) -> str:
    """SHA-256 hash of params — never store raw PHI in audit."""
    return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()


async def log_action(
    tenant_id: str,
    session_id: str,
    user_id: str,
    channel: str,
    action: str,
    tool_name: str | None = None,
    params: dict | None = None,
    result_summary: str | None = None,
    confirmation_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    latency_ms: int | None = None,
) -> int:
    """Insert an audit log entry. Returns the row id."""
    assert _db is not None, "audit db not initialized"
    now = datetime.now(timezone.utc).isoformat()
    params_hash = _hash_params(params) if params else None
    cur = await _db.execute(
        """INSERT INTO audit_log
           (tenant_id, timestamp, session_id, user_id, channel, action,
            tool_name, params_hash, result_summary, confirmation_id,
            provider, model, latency_ms)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            tenant_id, now, session_id, user_id, channel, action,
            tool_name, params_hash, result_summary, confirmation_id,
            provider, model, latency_ms,
        ),
    )
    await _db.commit()
    return cur.lastrowid


async def log_escalation(
    tenant_id: str,
    audit_log_id: int,
    escalated_to: str,
    reason: str | None = None,
) -> int:
    """Insert an escalation record. Returns the escalation id."""
    assert _db is not None, "audit db not initialized"
    cur = await _db.execute(
        """INSERT INTO escalations (tenant_id, audit_log_id, escalated_to, reason)
           VALUES (?,?,?,?)""",
        (tenant_id, audit_log_id, escalated_to, reason),
    )
    await _db.commit()
    return cur.lastrowid


async def resolve_escalation(
    escalation_id: int,
    resolved_by: str,
    resolution: str,
) -> None:
    """Mark an escalation as resolved."""
    assert _db is not None, "audit db not initialized"
    now = datetime.now(timezone.utc).isoformat()
    await _db.execute(
        """UPDATE escalations SET resolved_at=?, resolved_by=?, resolution=?
           WHERE id=?""",
        (now, resolved_by, resolution, escalation_id),
    )
    await _db.commit()
