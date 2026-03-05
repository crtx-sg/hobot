"""Unit tests for parallel tool dispatch and ward rounds fan-out."""

import asyncio
import json
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "clinibot"))

from tools import call_tools_parallel, _get_ward_rounds


# ---------------------------------------------------------------------------
# Minimal session mock
# ---------------------------------------------------------------------------


def _make_session():
    s = MagicMock()
    s.id = "test-session"
    s.tenant_id = "test-tenant"
    s.user_id = "test-user"
    s.channel = "test"
    return s


# ---------------------------------------------------------------------------
# call_tools_parallel tests
# ---------------------------------------------------------------------------


class TestCallToolsParallel:
    @pytest.mark.asyncio
    async def test_all_succeed(self):
        """Three tool calls all succeed — results returned in order."""
        session = _make_session()

        async def fake_call_tool(name, params, session):
            return {"status": "ok", "tool": name}

        with patch("tools.call_tool", side_effect=fake_call_tool):
            calls = [
                ("get_vitals", {"patient_id": "P001"}),
                ("get_medications", {"patient_id": "P001"}),
                ("get_allergies", {"patient_id": "P001"}),
            ]
            results = await call_tools_parallel(calls, session)

        assert len(results) == 3
        assert results[0]["tool"] == "get_vitals"
        assert results[0]["data"]["status"] == "ok"
        assert results[1]["tool"] == "get_medications"
        assert results[2]["tool"] == "get_allergies"

    @pytest.mark.asyncio
    async def test_one_fails_others_succeed(self):
        """One tool raises, siblings succeed — failure isolated."""
        session = _make_session()

        async def fake_call_tool(name, params, session):
            if name == "get_medications":
                raise RuntimeError("backend down")
            return {"status": "ok", "tool": name}

        with patch("tools.call_tool", side_effect=fake_call_tool):
            calls = [
                ("get_vitals", {"patient_id": "P001"}),
                ("get_medications", {"patient_id": "P001"}),
                ("get_allergies", {"patient_id": "P001"}),
            ]
            results = await call_tools_parallel(calls, session)

        assert len(results) == 3
        # First and third succeed
        assert results[0]["data"]["status"] == "ok"
        assert results[2]["data"]["status"] == "ok"
        # Second failed gracefully
        assert "error" in results[1]["data"]
        assert "backend down" in results[1]["data"]["error"]

    @pytest.mark.asyncio
    async def test_empty_calls(self):
        """Empty calls list returns empty results."""
        session = _make_session()
        results = await call_tools_parallel([], session)
        assert results == []

    @pytest.mark.asyncio
    async def test_parallel_execution(self):
        """Verify tools actually run concurrently, not sequentially."""
        session = _make_session()
        call_times = []

        async def fake_call_tool(name, params, session):
            call_times.append(asyncio.get_event_loop().time())
            await asyncio.sleep(0.05)
            return {"status": "ok"}

        with patch("tools.call_tool", side_effect=fake_call_tool):
            calls = [
                ("tool_a", {}),
                ("tool_b", {}),
                ("tool_c", {}),
            ]
            results = await call_tools_parallel(calls, session)

        assert len(results) == 3
        # All calls should start within ~10ms of each other (parallel)
        if len(call_times) == 3:
            spread = max(call_times) - min(call_times)
            assert spread < 0.03, f"Calls not parallel: spread={spread:.3f}s"


# ---------------------------------------------------------------------------
# Ward rounds parallel fan-out
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal httpx.Response stand-in."""
    def __init__(self, status_code, data):
        self.status_code = status_code
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data


class TestWardRoundsParallel:
    @pytest.mark.asyncio
    async def test_fan_out_parallel(self):
        """Ward rounds enriches patients with meds + scans in parallel."""
        rounds_data = {
            "ward_id": "ICU-A",
            "patients": [
                {"patient_id": "P001", "name": "Alice"},
                {"patient_id": "P002", "name": "Bob"},
            ],
        }

        call_log = []

        async def fake_get(url, **kwargs):
            call_log.append(url)
            if "/rounds" in url:
                return _FakeResponse(200, rounds_data)
            elif "MedicationRequest" in url:
                pid = url.split("patient=")[1]
                return _FakeResponse(200, {"medications": [{"drug": f"med-{pid}"}]})
            elif "dicom-web" in url:
                pid = url.split("PatientID=")[1].split("&")[0]
                return _FakeResponse(200, {"studies": [{"id": f"study-{pid}"}]})
            return _FakeResponse(404, {})

        mock_client = AsyncMock()
        mock_client.get = fake_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        session = _make_session()

        with patch("tools.httpx.AsyncClient", return_value=mock_client):
            result = await _get_ward_rounds({"ward_id": "ICU-A"}, session)

        assert "patients" in result
        patients = result["patients"]
        assert len(patients) == 2

        # Both patients enriched
        assert patients[0]["medications"] == [{"drug": "med-P001"}]
        assert patients[0]["latest_scan"] == {"id": "study-P001"}
        assert patients[1]["medications"] == [{"drug": "med-P002"}]
        assert patients[1]["latest_scan"] == {"id": "study-P002"}

    @pytest.mark.asyncio
    async def test_fan_out_partial_failure(self):
        """One patient's meds fail, rest still enriched."""
        rounds_data = {
            "ward_id": "ICU-A",
            "patients": [
                {"patient_id": "P001"},
                {"patient_id": "P002"},
            ],
        }

        async def fake_get(url, **kwargs):
            if "/rounds" in url:
                return _FakeResponse(200, rounds_data)
            elif "MedicationRequest" in url and "P001" in url:
                return _FakeResponse(500, {"error": "fail"})
            elif "MedicationRequest" in url:
                return _FakeResponse(200, {"medications": [{"drug": "aspirin"}]})
            elif "dicom-web" in url:
                return _FakeResponse(200, {"studies": []})
            return _FakeResponse(404, {})

        mock_client = AsyncMock()
        mock_client.get = fake_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        session = _make_session()

        with patch("tools.httpx.AsyncClient", return_value=mock_client):
            result = await _get_ward_rounds({"ward_id": "ICU-A"}, session)

        patients = result["patients"]
        # P001 meds failed → empty list
        assert patients[0]["medications"] == []
        # P002 meds succeeded
        assert patients[1]["medications"] == [{"drug": "aspirin"}]
        # Both scans returned empty
        assert patients[0]["latest_scan"] is None
        assert patients[1]["latest_scan"] is None
