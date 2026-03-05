"""Clinical facts — structured extraction and storage from tool results."""

import json
from datetime import datetime, timezone

import aiosqlite

_db: aiosqlite.Connection | None = None


def bind_db(db: aiosqlite.Connection) -> None:
    """Share the audit database connection (same SQLite file)."""
    global _db
    _db = db


# ---------------------------------------------------------------------------
# Fact extraction rules per tool
# ---------------------------------------------------------------------------

def _extract_vitals(result: dict, patient_id: str) -> list[dict]:
    """Extract vitals facts from get_vitals / get_vitals_history results."""
    facts = []
    # Single vitals snapshot
    if "heart_rate" in result or "bp_systolic" in result:
        facts.append({"fact_type": "vitals", "fact_data": result})
    # History list
    for entry in result.get("history", []):
        facts.append({"fact_type": "vitals", "fact_data": entry})
    return facts


def _extract_list(result: dict, key: str, fact_type: str) -> list[dict]:
    """Generic extractor for list-valued results (medications, allergies, etc.)."""
    items = result.get(key, result.get("entry", []))
    if isinstance(items, list):
        return [{"fact_type": fact_type, "fact_data": item} for item in items]
    return [{"fact_type": fact_type, "fact_data": result}]


EXTRACTORS: dict[str, callable] = {
    "get_vitals": lambda r, pid: _extract_vitals(r, pid),
    "get_vitals_history": lambda r, pid: _extract_vitals(r, pid),
    "get_medications": lambda r, pid: _extract_list(r, "medications", "medication"),
    "get_allergies": lambda r, pid: _extract_list(r, "allergies", "allergy"),
    "get_lab_results": lambda r, pid: _extract_list(r, "results", "lab_result"),
    "get_lab_order": lambda r, pid: [{"fact_type": "lab_order", "fact_data": r}],
    "get_patient": lambda r, pid: [{"fact_type": "demographics", "fact_data": r}],
    "get_orders": lambda r, pid: _extract_list(r, "orders", "order"),
    "get_studies": lambda r, pid: _extract_list(r, "studies", "imaging_study"),
    "get_report": lambda r, pid: [{"fact_type": "radiology_report", "fact_data": r}],
    "get_blood_availability": lambda r, pid: [{"fact_type": "blood_inventory", "fact_data": r}],
    "get_crossmatch_status": lambda r, pid: [{"fact_type": "crossmatch", "fact_data": r}],
}


async def extract_and_store(
    tool_name: str,
    tool_result: dict,
    patient_id: str,
    session_id: str,
    tenant_id: str,
) -> int:
    """Extract clinical facts from a tool result and store them. Returns count stored."""
    extractor = EXTRACTORS.get(tool_name)
    if not extractor or not patient_id:
        return 0
    facts = extractor(tool_result, patient_id)
    for fact in facts:
        await store_fact(
            session_id=session_id,
            tenant_id=tenant_id,
            patient_id=patient_id,
            fact_type=fact["fact_type"],
            fact_data=fact["fact_data"],
            source_tool=tool_name,
        )
    return len(facts)


async def store_fact(
    session_id: str,
    tenant_id: str,
    patient_id: str,
    fact_type: str,
    fact_data: dict,
    source_tool: str | None = None,
) -> None:
    """Insert a clinical fact into the database."""
    assert _db is not None, "clinical_memory db not bound"
    now = datetime.now(timezone.utc).isoformat()
    await _db.execute(
        """INSERT INTO clinical_facts
           (session_id, tenant_id, patient_id, fact_type, fact_data, source_tool, recorded_at)
           VALUES (?,?,?,?,?,?,?)""",
        (session_id, tenant_id, patient_id, fact_type, json.dumps(fact_data), source_tool, now),
    )
    await _db.commit()


async def get_facts(
    patient_id: str,
    tenant_id: str,
    fact_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Retrieve recent clinical facts for a patient."""
    assert _db is not None, "clinical_memory db not bound"
    if fact_type:
        cur = await _db.execute(
            """SELECT fact_type, fact_data, source_tool, recorded_at
               FROM clinical_facts
               WHERE patient_id=? AND tenant_id=? AND fact_type=?
               ORDER BY recorded_at DESC LIMIT ?""",
            (patient_id, tenant_id, fact_type, limit),
        )
    else:
        cur = await _db.execute(
            """SELECT fact_type, fact_data, source_tool, recorded_at
               FROM clinical_facts
               WHERE patient_id=? AND tenant_id=?
               ORDER BY recorded_at DESC LIMIT ?""",
            (patient_id, tenant_id, limit),
        )
    rows = await cur.fetchall()
    return [
        {
            "fact_type": row[0],
            "fact_data": json.loads(row[1]),
            "source_tool": row[2],
            "recorded_at": row[3],
        }
        for row in rows
    ]
