"""Session manager — JSONL-backed conversation state with in-memory cache."""

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SESSIONS_DIR = os.environ.get("SESSIONS_DIR", "/data/sessions")


@dataclass
class Session:
    id: str
    tenant_id: str
    user_id: str
    channel: str
    messages: list[dict] = field(default_factory=list)
    active_patients: set[str] = field(default_factory=set)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_consolidated: int = 0
    summary: str = ""

    # -- JSONL persistence --

    def _jsonl_path(self) -> Path:
        return Path(SESSIONS_DIR) / self.tenant_id / f"{self.id}.jsonl"

    def _append_line(self, data: dict) -> None:
        path = self._jsonl_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(data) + "\n")

    def _write_metadata(self) -> None:
        """Rewrite the JSONL file with updated metadata on line 0."""
        path = self._jsonl_path()
        if not path.exists():
            return
        lines = path.read_text().splitlines()
        meta = {
            "type": "metadata",
            "session_id": self.id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "channel": self.channel,
            "created_at": self.created_at,
            "last_consolidated": self.last_consolidated,
            "summary": self.summary,
            "active_patients": list(self.active_patients),
        }
        lines[0] = json.dumps(meta)
        path.write_text("\n".join(lines) + "\n")

    def _persist_new(self) -> None:
        """Write initial metadata line for a brand-new session."""
        meta = {
            "type": "metadata",
            "session_id": self.id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "channel": self.channel,
            "created_at": self.created_at,
            "last_consolidated": 0,
            "summary": "",
            "active_patients": [],
        }
        self._append_line(meta)

    def append_message(self, role: str, content: str) -> None:
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.messages.append(msg)
        self._append_line({"type": "message", **msg})

    def get_context(self, max_messages: int = 20) -> list[dict]:
        """Return recent messages, prepending consolidated summary if available."""
        recent = self.messages[-max_messages:]
        if self.summary:
            return [{"role": "system", "content": f"[Conversation summary]: {self.summary}"}] + recent
        return recent

    def save_consolidation(self, summary: str, new_pointer: int) -> None:
        """Persist a consolidation event and update metadata."""
        self.summary = summary
        self.last_consolidated = new_pointer
        self._append_line({
            "type": "consolidation",
            "summary": summary,
            "pointer": new_pointer,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        self._write_metadata()


def _load_from_jsonl(path: Path) -> Session | None:
    """Load a session from a JSONL file."""
    if not path.exists():
        return None
    lines = path.read_text().splitlines()
    if not lines:
        return None
    meta = json.loads(lines[0])
    if meta.get("type") != "metadata":
        return None
    sess = Session(
        id=meta["session_id"],
        tenant_id=meta["tenant_id"],
        user_id=meta["user_id"],
        channel=meta["channel"],
        created_at=meta.get("created_at", ""),
        last_consolidated=meta.get("last_consolidated", 0),
        summary=meta.get("summary", ""),
        active_patients=set(meta.get("active_patients", [])),
    )
    for line in lines[1:]:
        entry = json.loads(line)
        if entry.get("type") == "message":
            sess.messages.append({
                "role": entry["role"],
                "content": entry["content"],
                "timestamp": entry.get("timestamp", ""),
            })
        # consolidation events update summary/pointer (last one wins)
        elif entry.get("type") == "consolidation":
            sess.summary = entry.get("summary", sess.summary)
            sess.last_consolidated = entry.get("pointer", sess.last_consolidated)
    return sess


_sessions: dict[str, Session] = {}


def get_or_create(
    session_id: str | None,
    tenant_id: str,
    user_id: str,
    channel: str,
) -> Session:
    """Return existing session (memory or disk) or create a new one."""
    if session_id and session_id in _sessions:
        return _sessions[session_id]

    # Try loading from disk
    if session_id:
        path = Path(SESSIONS_DIR) / tenant_id / f"{session_id}.jsonl"
        sess = _load_from_jsonl(path)
        if sess:
            _sessions[session_id] = sess
            return sess

    sid = session_id or str(uuid.uuid4())
    sess = Session(id=sid, tenant_id=tenant_id, user_id=user_id, channel=channel)
    sess._persist_new()
    _sessions[sid] = sess
    return sess


def get(session_id: str) -> Session | None:
    return _sessions.get(session_id)
