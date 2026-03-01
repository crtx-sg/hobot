"""
Sanity smoke tests for the Hobot stack.

Requires the full stack to be running:
    docker compose up -d --build

Run:
    pip install -r tests/requirements.txt
    pytest tests/test_sanity.py -v
"""

import httpx
import pytest

BASE = "http://localhost:3000"
TIMEOUT = 30.0

EXPECTED_BACKENDS = {
    "synthetic-monitoring",
    "synthetic-ehr",
    "synthetic-lis",
    "synthetic-pharmacy",
    "synthetic-radiology",
    "synthetic-bloodbank",
    "synthetic-erp",
    "synthetic-patient-services",
}


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE, timeout=TIMEOUT) as c:
        yield c


# ── 1. Health endpoint ──────────────────────────────────────────────


def test_health_returns_200(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "degraded")
    assert isinstance(body["backends"], dict)


def test_health_lists_all_backends(client):
    r = client.get("/health")
    body = r.json()
    assert set(body["backends"].keys()) == EXPECTED_BACKENDS


# ── 2. Chat endpoint ────────────────────────────────────────────────


def test_chat_basic(client):
    r = client.post(
        "/chat",
        json={
            "message": "Show vitals for P001",
            "user_id": "test-user",
            "channel": "webchat",
            "tenant_id": "test",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert "response" in body
    assert "session_id" in body


# ── 3. Session continuity ───────────────────────────────────────────


def test_chat_session_continuity(client):
    # First message — start a new session
    r1 = client.post(
        "/chat",
        json={
            "message": "Show vitals for P001",
            "user_id": "test-user",
            "channel": "webchat",
            "tenant_id": "test",
        },
    )
    assert r1.status_code == 200
    sid = r1.json()["session_id"]

    # Follow-up — same session
    r2 = client.post(
        "/chat",
        json={
            "message": "What about P002?",
            "user_id": "test-user",
            "channel": "webchat",
            "tenant_id": "test",
            "session_id": sid,
        },
    )
    assert r2.status_code == 200
    assert r2.json()["session_id"] == sid


# ── 4. Telegram channel respects max length ─────────────────────────


def test_chat_telegram_max_length(client):
    r = client.post(
        "/chat",
        json={
            "message": "Show vitals for P001",
            "user_id": "test-user",
            "channel": "telegram",
            "tenant_id": "test",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["response"]) <= 4096


# ── 5. Confirm with invalid ID ──────────────────────────────────────


def test_confirm_invalid_id(client):
    r = client.post("/confirm/invalid-id-does-not-exist")
    body = r.json()
    # Gateway returns 200 with error nested in result
    assert r.status_code in (400, 404, 422) or "error" in str(body)
