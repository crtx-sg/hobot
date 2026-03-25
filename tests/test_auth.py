"""Tests for auth middleware and confirmation hardening."""

import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "clinibot"))


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

class TestAuthMiddleware:
    def test_load_api_keys(self):
        with patch.dict(os.environ, {"API_KEYS": "tg-bot:secret1,webchat:secret2"}):
            from auth import load_api_keys, _KEYS
            load_api_keys()
            assert _KEYS["secret1"] == "tg-bot"
            assert _KEYS["secret2"] == "webchat"

    def test_load_empty_keys(self):
        with patch.dict(os.environ, {"API_KEYS": ""}):
            from auth import load_api_keys, _KEYS
            load_api_keys()
            # No keys loaded — middleware disabled

    def test_exempt_paths(self):
        from auth import _EXEMPT_PATHS
        assert "/health" in _EXEMPT_PATHS
        assert "/docs" in _EXEMPT_PATHS
        assert "/openapi.json" in _EXEMPT_PATHS


# ---------------------------------------------------------------------------
# Confirmation hardening
# ---------------------------------------------------------------------------

class TestConfirmationGate:
    def test_cleanup_expired(self):
        from tools import _pending, cleanup_expired_confirmations, CONFIRMATION_TTL_SECONDS
        _pending.clear()
        _pending["old"] = {"created_at": time.monotonic() - CONFIRMATION_TTL_SECONDS - 10}
        _pending["fresh"] = {"created_at": time.monotonic()}
        removed = cleanup_expired_confirmations()
        assert removed == 1
        assert "old" not in _pending
        assert "fresh" in _pending
        _pending.clear()

    @pytest.mark.asyncio
    async def test_confirm_rejects_expired(self):
        from tools import _pending, confirm_tool, CONFIRMATION_TTL_SECONDS
        import hashlib, hmac, json
        from tools import _CONFIRM_SECRET

        _pending.clear()
        cid = "test-expired"
        params = {"patient_id": "P001"}
        params_hash = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
        sig = hmac.new(
            _CONFIRM_SECRET,
            f"{cid}:initiate_code_blue:{params_hash}:doc1".encode(),
            "sha256",
        ).hexdigest()
        _pending[cid] = {
            "tool_name": "initiate_code_blue",
            "params": params,
            "session_id": "s1",
            "tenant_id": "t1",
            "user_id": "doc1",
            "channel": "webchat",
            "created_at": time.monotonic() - CONFIRMATION_TTL_SECONDS - 10,
            "client_id": "tg-bot",
            "signature": sig,
        }
        sess = MagicMock()
        sess.id = "s1"
        result = await confirm_tool(cid, sess, client_id="tg-bot")
        assert "expired" in result["error"].lower()
        _pending.clear()

    @pytest.mark.asyncio
    async def test_confirm_rejects_wrong_client(self):
        from tools import _pending, confirm_tool, _CONFIRM_SECRET
        import hashlib, hmac, json

        _pending.clear()
        cid = "test-client"
        params = {"patient_id": "P001"}
        params_hash = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
        sig = hmac.new(
            _CONFIRM_SECRET,
            f"{cid}:initiate_code_blue:{params_hash}:doc1".encode(),
            "sha256",
        ).hexdigest()
        _pending[cid] = {
            "tool_name": "initiate_code_blue",
            "params": params,
            "session_id": "s1",
            "tenant_id": "t1",
            "user_id": "doc1",
            "channel": "webchat",
            "created_at": time.monotonic(),
            "client_id": "tg-bot",
            "signature": sig,
        }
        sess = MagicMock()
        sess.id = "s1"
        result = await confirm_tool(cid, sess, client_id="wrong-client")
        assert "mismatch" in result["error"].lower()
        _pending.clear()


# ---------------------------------------------------------------------------
# Session user binding + TTL
# ---------------------------------------------------------------------------

class TestSessionSecurity:
    def test_user_binding(self, tmp_path):
        """Different user_id on same session_id gets a new session."""
        import session as session_mod
        orig = session_mod.SESSIONS_DIR
        session_mod.SESSIONS_DIR = str(tmp_path)
        try:
            session_mod._sessions.clear()
            s1 = session_mod.get_or_create("sess-1", "t1", "user-A", "webchat")
            assert s1.user_id == "user-A"
            s2 = session_mod.get_or_create("sess-1", "t1", "user-B", "webchat")
            assert s2.user_id == "user-B"
            session_mod._sessions.clear()
        finally:
            session_mod.SESSIONS_DIR = orig

    def test_session_expiry(self, tmp_path):
        """Expired sessions are replaced."""
        import session as session_mod
        from datetime import datetime, timezone, timedelta
        orig = session_mod.SESSIONS_DIR
        session_mod.SESSIONS_DIR = str(tmp_path)
        try:
            session_mod._sessions.clear()
            s1 = session_mod.get_or_create("sess-exp", "t1", "user-A", "webchat")
            old_time = (datetime.now(timezone.utc) - timedelta(hours=session_mod.SESSION_TTL_HOURS + 1)).isoformat()
            s1.last_activity = old_time
            s2 = session_mod.get_or_create("sess-exp", "t1", "user-A", "webchat")
            assert s2 is not s1
            session_mod._sessions.clear()
        finally:
            session_mod.SESSIONS_DIR = orig


# ---------------------------------------------------------------------------
# PHI patterns
# ---------------------------------------------------------------------------

class TestPendingMaxSize:
    def test_pending_cap(self):
        from tools import _pending, _PENDING_MAX_SIZE
        assert _PENDING_MAX_SIZE == 1000  # default


class TestPHIPatterns:
    def test_email_redacted(self):
        from phi import redact
        text = "Contact doctor@hospital.com for info"
        redacted, mapping = redact(text)
        assert "doctor@hospital.com" not in redacted
        assert any("EML" in k for k in mapping)

    def test_ssn_redacted(self):
        from phi import redact
        text = "SSN: 123-45-6789"
        redacted, mapping = redact(text)
        assert "123-45-6789" not in redacted

    def test_aadhaar_redacted(self):
        from phi import redact
        text = "Aadhaar: 1234 5678 9012"
        redacted, mapping = redact(text)
        assert "1234 5678 9012" not in redacted

    def test_doctor_name_redacted(self):
        from phi import redact
        text = "Refer to Dr. Sharma immediately"
        redacted, mapping = redact(text)
        assert "Dr. Sharma" not in redacted or "Dr" not in redacted
