"""
Sanity smoke tests for the Hobot stack.

Requires the full stack to be running:
    docker compose up -d --build

Run:
    pip install -r tests/requirements.txt
    pytest tests/test_sanity.py -v
"""

import json

import httpx
import pytest

BASE = "http://localhost:3000"
TIMEOUT = 120.0  # LLM inference can be slow with local models

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


# ── 6. Structured rich response (blocks) ──────────────────────────


def test_chat_response_contains_blocks_key(client):
    """Response always contains a 'blocks' key (may be null)."""
    r = client.post(
        "/chat",
        json={
            "message": "hello",
            "user_id": "test-user",
            "channel": "webchat",
            "tenant_id": "test",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert "blocks" in body
    assert "response" in body
    assert "session_id" in body


def test_chat_vitals_returns_blocks_webchat(client):
    """Vitals query should return data_table block for webchat (passthrough)."""
    r = client.post(
        "/chat",
        json={
            "message": "vitals for P001",
            "user_id": "test-blocks",
            "channel": "webchat",
            "tenant_id": "test-blocks",
        },
    )
    assert r.status_code == 200
    body = r.json()
    blocks = body.get("blocks")
    # blocks may be null if LLM answered without tool calls
    if blocks is not None:
        types = [b["type"] for b in blocks]
        assert "data_table" in types
        table = next(b for b in blocks if b["type"] == "data_table")
        assert "columns" in table
        assert "rows" in table


def test_chat_vitals_telegram_rendered_html(client):
    """Telegram blocks should be rendered as HTML text, not raw data_table."""
    r = client.post(
        "/chat",
        json={
            "message": "vitals for P001",
            "user_id": "test-blocks",
            "channel": "telegram",
            "tenant_id": "test-blocks-tg",
        },
    )
    assert r.status_code == 200
    body = r.json()
    blocks = body.get("blocks")
    if blocks is not None:
        # Telegram renderer converts data_table to {"type":"text","html":...}
        text_blocks = [b for b in blocks if b.get("type") == "text"]
        assert len(text_blocks) > 0
        assert any("html" in b for b in text_blocks)


def test_chat_blood_availability_returns_blocks(client):
    """Blood availability should return a data_table block."""
    r = client.post(
        "/chat",
        json={
            "message": "blood availability",
            "user_id": "test-blocks",
            "channel": "webchat",
            "tenant_id": "test-blocks",
        },
    )
    assert r.status_code == 200
    body = r.json()
    blocks = body.get("blocks")
    if blocks is not None:
        types = [b["type"] for b in blocks]
        assert "data_table" in types


def test_chat_code_blue_returns_confirmation_block(client):
    """Critical action should return a confirmation block with button."""
    r = client.post(
        "/chat",
        json={
            "message": "code blue for P001",
            "user_id": "test-blocks",
            "channel": "webchat",
            "tenant_id": "test-blocks-confirm",
        },
    )
    assert r.status_code == 200
    body = r.json()
    blocks = body.get("blocks")
    if blocks is not None:
        types = [b["type"] for b in blocks]
        assert "confirmation" in types
        conf = next(b for b in blocks if b["type"] == "confirmation")
        assert "confirmation_id" in conf
        assert len(conf.get("buttons", [])) > 0


def test_chat_code_blue_telegram_has_confirm_button(client):
    """Telegram confirmation block should have inline keyboard callback_data."""
    r = client.post(
        "/chat",
        json={
            "message": "code blue for P001",
            "user_id": "test-blocks",
            "channel": "telegram",
            "tenant_id": "test-blocks-tg-confirm",
        },
    )
    assert r.status_code == 200
    body = r.json()
    blocks = body.get("blocks")
    if blocks is not None:
        conf_blocks = [b for b in blocks if b.get("type") == "confirmation"]
        assert len(conf_blocks) > 0
        cb_data = json.loads(conf_blocks[0]["buttons"][0]["callback_data"])
        # Formatter uses short keys ("a"/"p") for Telegram's 64-byte callback_data limit
        action = cb_data.get("a", cb_data.get("action", ""))
        params = cb_data.get("p", cb_data.get("params", {}))
        assert action == "confirm"
        assert "confirmation_id" in params


def test_chat_slack_blocks_are_block_kit(client):
    """Slack blocks should use Block Kit format (section type)."""
    r = client.post(
        "/chat",
        json={
            "message": "blood availability",
            "user_id": "test-blocks",
            "channel": "slack",
            "tenant_id": "test-blocks-slack",
        },
    )
    assert r.status_code == 200
    body = r.json()
    blocks = body.get("blocks")
    if blocks is not None:
        # Slack data_table may render as "section" (mrkdwn) or "rendered_image"
        # (when render_as_image includes data_table in channels.json)
        assert any(b.get("type") in ("section", "rendered_image") for b in blocks)


def test_chat_whatsapp_filters_unsupported_blocks(client):
    """WhatsApp should not contain actions or confirmation blocks."""
    r = client.post(
        "/chat",
        json={
            "message": "vitals for P001",
            "user_id": "test-blocks",
            "channel": "whatsapp",
            "tenant_id": "test-blocks-wa",
        },
    )
    assert r.status_code == 200
    body = r.json()
    blocks = body.get("blocks")
    if blocks is not None:
        types = {b["type"] for b in blocks}
        assert "actions" not in types
        assert "confirmation" not in types


def test_chat_backward_compatibility(client):
    """Old clients that only read 'response' + 'session_id' should still work."""
    r = client.post(
        "/chat",
        json={
            "message": "vitals for P001",
            "user_id": "test-compat",
            "channel": "webchat",
            "tenant_id": "test",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["response"], str)
    assert len(body["response"]) > 0
    assert isinstance(body["session_id"], str)
