"""Unit tests for nanobot/blocks.py — pure functions, no stack needed."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nanobot"))

from blocks import build_blocks, _build_vitals_blocks, _build_lab_blocks, _build_medications_blocks, _build_allergies_blocks, _build_patient_blocks, _build_blood_availability_blocks, _build_ward_patients_blocks, _build_drug_interactions_blocks, _build_confirmation_blocks, _build_vitals_history_blocks, _build_studies_blocks, _build_ecg_blocks, _build_report_blocks, _build_vitals_trend_blocks


class TestBuildVitalsBlocks:
    def test_basic_vitals(self):
        data = {
            "patient_id": "P001",
            "heart_rate": 82,
            "bp_systolic": 120,
            "bp_diastolic": 80,
            "spo2": 98,
            "temperature": 37.2,
        }
        blocks = _build_vitals_blocks(data, {})
        table = blocks[0]
        assert table["type"] == "data_table"
        assert table["title"] == "Vitals — P001"
        assert len(table["rows"]) == 5
        assert table["columns"] == ["Metric", "Value", "Unit"]

    def test_vitals_with_alert(self):
        data = {
            "patient_id": "P001",
            "heart_rate": 130,  # above 90
            "bp_systolic": 120,
            "bp_diastolic": 80,
            "spo2": 98,
            "temperature": 37.2,
        }
        blocks = _build_vitals_blocks(data, {})
        alerts = [b for b in blocks if b["type"] == "alert"]
        assert len(alerts) == 1
        assert "Heart Rate" in alerts[0]["text"]

    def test_vitals_actions_button(self):
        data = {"patient_id": "P001", "heart_rate": 75, "bp_systolic": 120, "bp_diastolic": 80, "spo2": 98, "temperature": 37.0}
        blocks = _build_vitals_blocks(data, {})
        actions = [b for b in blocks if b["type"] == "actions"]
        assert len(actions) == 1
        assert actions[0]["buttons"][0]["action"] == "get_vitals_history"

    def test_vitals_error_returns_empty(self):
        assert _build_vitals_blocks({"error": "not found"}, {}) == []

    def test_patient_id_from_params(self):
        data = {"heart_rate": 75, "bp_systolic": 120, "bp_diastolic": 80, "spo2": 98, "temperature": 37.0}
        blocks = _build_vitals_blocks(data, {"patient_id": "P002"})
        assert "P002" in blocks[0]["title"]


class TestBuildVitalsHistoryBlocks:
    def test_basic_history(self):
        data = {
            "patient_id": "P001",
            "readings": [
                {"timestamp": "2026-02-25T08:00:00", "heart_rate": 72, "bp_systolic": 118, "spo2": 97, "temperature": 36.8},
                {"timestamp": "2026-02-25T09:00:00", "heart_rate": 75, "bp_systolic": 120, "spo2": 98, "temperature": 37.2},
            ],
        }
        blocks = _build_vitals_history_blocks(data, {})
        table = blocks[0]
        assert table["type"] == "data_table"
        assert len(table["rows"]) == 2

    def test_chart_block_with_multiple_readings(self):
        data = {
            "patient_id": "P001",
            "readings": [
                {"timestamp": "2026-02-25T08:00:00", "heart_rate": 72, "bp_systolic": 118, "spo2": 97, "temperature": 36.8},
                {"timestamp": "2026-02-25T09:00:00", "heart_rate": 75, "bp_systolic": 120, "spo2": 98, "temperature": 37.2},
            ],
        }
        blocks = _build_vitals_history_blocks(data, {})
        charts = [b for b in blocks if b["type"] == "chart"]
        assert len(charts) == 1
        assert charts[0]["chart_type"] == "line"
        assert "heart_rate" in charts[0]["series"]

    def test_no_chart_with_single_reading(self):
        data = {"patient_id": "P001", "readings": [{"timestamp": "2026-02-25T08:00:00", "heart_rate": 72, "bp_systolic": 118, "spo2": 97, "temperature": 36.8}]}
        blocks = _build_vitals_history_blocks(data, {})
        charts = [b for b in blocks if b["type"] == "chart"]
        assert len(charts) == 0

    def test_empty_readings(self):
        assert _build_vitals_history_blocks({"patient_id": "P001", "readings": []}, {}) == []


class TestBuildLabBlocks:
    def test_basic_labs(self):
        data = {
            "patient_id": "P001",
            "labs": [{
                "test_type": "CBC",
                "results": {
                    "WBC": {"value": 6.8, "unit": "x10^3/uL", "ref_range": "4.0-11.0"},
                    "RBC": {"value": 4.9, "unit": "x10^6/uL", "ref_range": "4.5-5.5"},
                },
            }],
        }
        blocks = _build_lab_blocks(data, {})
        assert len(blocks) == 1
        assert blocks[0]["type"] == "data_table"
        assert blocks[0]["title"] == "CBC — P001"
        assert len(blocks[0]["rows"]) == 2
        assert blocks[0]["columns"] == ["Test", "Value", "Unit", "Ref Range"]

    def test_multiple_lab_panels(self):
        data = {
            "patient_id": "P001",
            "labs": [
                {"test_type": "CBC", "results": {"WBC": {"value": 6.8, "unit": "u", "ref_range": "4-11"}}},
                {"test_type": "BMP", "results": {"Na": {"value": 140, "unit": "mEq/L", "ref_range": "136-145"}}},
            ],
        }
        blocks = _build_lab_blocks(data, {})
        assert len(blocks) == 2


class TestBuildMedicationsBlocks:
    def test_basic(self):
        data = {
            "patient_id": "P001",
            "medications": [
                {"medication": "Metformin", "dose": "500mg", "route": "PO", "frequency": "BID", "status": "active"},
            ],
        }
        blocks = _build_medications_blocks(data, {})
        assert blocks[0]["type"] == "data_table"
        assert len(blocks[0]["rows"]) == 1
        assert blocks[0]["columns"][0] == "Medication"


class TestBuildAllergiesBlocks:
    def test_basic(self):
        data = {
            "patient_id": "P001",
            "allergies": [{
                "substance": "Penicillin",
                "criticality": "high",
                "clinical_status": "active",
                "reactions": [{"manifestation": ["urticaria", "angioedema"], "severity": "severe"}],
            }],
        }
        blocks = _build_allergies_blocks(data, {})
        assert blocks[0]["rows"][0][0] == "Penicillin"
        assert "urticaria" in blocks[0]["rows"][0][2]


class TestBuildPatientBlocks:
    def test_basic(self):
        data = {"patient_id": "P001", "name": "John Doe", "gender": "male", "birth_date": "1965-03-15"}
        blocks = _build_patient_blocks(data, {})
        kv = blocks[0]
        assert kv["type"] == "key_value"
        assert kv["items"][0]["value"] == "John Doe"

    def test_action_buttons(self):
        data = {"patient_id": "P001", "name": "John Doe", "gender": "male", "birth_date": "1965-03-15"}
        blocks = _build_patient_blocks(data, {})
        actions = [b for b in blocks if b["type"] == "actions"]
        assert len(actions) == 1
        assert len(actions[0]["buttons"]) == 3


class TestBuildBloodAvailabilityBlocks:
    def test_basic(self):
        data = {"inventory": {"A+": 12, "O-": 6}}
        blocks = _build_blood_availability_blocks(data, {})
        assert blocks[0]["type"] == "data_table"
        assert len(blocks[0]["rows"]) == 2


class TestBuildWardPatientsBlocks:
    def test_basic(self):
        data = {
            "ward_id": "ICU-A",
            "patients": [
                {"patient_id": "P001", "vitals": {"heart_rate": 85, "spo2": 96}, "news_score": 3},
                {"patient_id": "P002", "vitals": {"heart_rate": 110, "spo2": 90}, "news_score": 7},
            ],
        }
        blocks = _build_ward_patients_blocks(data, {})
        table = blocks[0]
        assert len(table["rows"]) == 2
        alerts = [b for b in blocks if b["type"] == "alert"]
        assert len(alerts) == 1  # P002 has NEWS >= 5
        assert "P002" in alerts[0]["text"]


class TestBuildDrugInteractionsBlocks:
    def test_no_interactions(self):
        data = {"medications_checked": ["metformin"], "interaction_count": 0, "interactions": []}
        blocks = _build_drug_interactions_blocks(data, {})
        assert blocks[0]["type"] == "text"

    def test_high_severity_interaction(self):
        data = {
            "interactions": [{"drugs": ["warfarin", "aspirin"], "severity": "high", "description": "Bleeding risk"}],
        }
        blocks = _build_drug_interactions_blocks(data, {})
        assert blocks[0]["type"] == "alert"
        assert blocks[0]["severity"] == "critical"


class TestBuildConfirmationBlocks:
    def test_awaiting(self):
        data = {"status": "awaiting_confirmation", "confirmation_id": "abc-123", "message": "Critical action 'initiate_code_blue' requires confirmation."}
        blocks = _build_confirmation_blocks(data, {})
        assert blocks[0]["type"] == "confirmation"
        assert blocks[0]["confirmation_id"] == "abc-123"
        assert blocks[0]["buttons"][0]["action"] == "confirm"

    def test_non_awaiting(self):
        assert _build_confirmation_blocks({"status": "ok"}, {}) == []


class TestBuildStudiesBlocks:
    def test_basic(self):
        data = {
            "patient_id": "P001",
            "studies": [{"study_id": "s1", "modality": "CT", "date": "20260225", "description": "Chest CT"}],
        }
        blocks = _build_studies_blocks(data, {})
        assert blocks[0]["type"] == "data_table"
        assert len(blocks[0]["rows"]) == 1


class TestBuildEcgBlocks:
    def test_basic(self):
        data = {
            "patient_id": "P001",
            "event_id": "evt1",
            "sampling_rate_hz": 200,
            "duration_s": 12,
            "leads": {"ECG1": [120.5, 119.8], "ECG2": [98.3, 97.9]},
        }
        blocks = _build_ecg_blocks(data, {})
        assert blocks[0]["type"] == "waveform"
        assert blocks[0]["sampling_rate_hz"] == 200
        assert "ECG1" in blocks[0]["leads"]

    def test_no_leads(self):
        assert _build_ecg_blocks({"patient_id": "P001", "leads": {}}, {}) == []


class TestBuildReportBlocks:
    def test_basic(self):
        data = {
            "study_id": "s1",
            "patient_id": "P001",
            "patient_name": "John Doe",
            "modality": "CT",
            "date": "20260225",
            "description": "Chest CT",
            "referring_physician": "DR-SMITH",
        }
        blocks = _build_report_blocks(data, {})
        kv = blocks[0]
        assert kv["type"] == "key_value"
        img = blocks[1]
        assert img["type"] == "image"
        assert "/studies/s1/image" in img["url"]


class TestBuildBlocksOrchestrator:
    def test_multiple_tool_results(self):
        tool_results = [
            {"tool": "get_vitals", "params": {}, "data": {"patient_id": "P001", "heart_rate": 82, "bp_systolic": 120, "bp_diastolic": 80, "spo2": 98, "temperature": 37.2}},
            {"tool": "get_medications", "params": {}, "data": {"patient_id": "P001", "medications": [{"medication": "Metformin", "dose": "500mg", "route": "PO", "frequency": "BID", "status": "active"}]}},
        ]
        blocks = build_blocks(tool_results)
        types = [b["type"] for b in blocks]
        assert "data_table" in types

    def test_unknown_tool_ignored(self):
        tool_results = [{"tool": "unknown_tool", "params": {}, "data": {"something": True}}]
        assert build_blocks(tool_results) == []

    def test_empty_list(self):
        assert build_blocks([]) == []

    def test_awaiting_confirmation_generates_confirmation_block(self):
        """Any tool returning awaiting_confirmation should produce confirmation block."""
        tool_results = [{
            "tool": "initiate_code_blue",
            "params": {"patient_id": "P001"},
            "data": {
                "status": "awaiting_confirmation",
                "confirmation_id": "xyz-789",
                "message": "Critical action 'initiate_code_blue' requires confirmation.",
            },
        }]
        blocks = build_blocks(tool_results)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "confirmation"
        assert blocks[0]["confirmation_id"] == "xyz-789"

    def test_confirmation_skips_tool_builder(self):
        """Awaiting_confirmation should NOT also run the tool's own builder."""
        tool_results = [{
            "tool": "get_vitals",  # has a builder, but status is awaiting_confirmation
            "params": {},
            "data": {"status": "awaiting_confirmation", "confirmation_id": "c1", "message": "msg"},
        }]
        blocks = build_blocks(tool_results)
        types = [b["type"] for b in blocks]
        assert "data_table" not in types
        assert "confirmation" in types

    def test_mixed_normal_and_confirmation_results(self):
        """Blocks from normal tool + confirmation tool in same request."""
        tool_results = [
            {"tool": "get_vitals", "params": {}, "data": {"patient_id": "P001", "heart_rate": 80, "bp_systolic": 120, "bp_diastolic": 80, "spo2": 98, "temperature": 37.0}},
            {"tool": "dispense_medication", "params": {}, "data": {"status": "awaiting_confirmation", "confirmation_id": "c2", "message": "Critical action 'dispense_medication' requires confirmation."}},
        ]
        blocks = build_blocks(tool_results)
        types = [b["type"] for b in blocks]
        assert "data_table" in types
        assert "confirmation" in types

    def test_error_result_produces_no_blocks(self):
        """Tool returning error should produce no blocks."""
        tool_results = [{"tool": "get_vitals", "params": {}, "data": {"error": "Backend unreachable"}}]
        assert build_blocks(tool_results) == []


class TestVitalsMultipleAlerts:
    def test_multiple_abnormal_vitals(self):
        data = {
            "patient_id": "P001",
            "heart_rate": 130,
            "bp_systolic": 200,
            "spo2": 88,
            "bp_diastolic": 80,
            "temperature": 39.5,
        }
        blocks = _build_vitals_blocks(data, {})
        alerts = [b for b in blocks if b["type"] == "alert"]
        assert len(alerts) == 4  # HR, BP systolic, SpO2, Temp all abnormal

    def test_all_normal_no_alerts(self):
        data = {"patient_id": "P001", "heart_rate": 75, "bp_systolic": 120, "bp_diastolic": 80, "spo2": 98, "temperature": 37.0}
        blocks = _build_vitals_blocks(data, {})
        alerts = [b for b in blocks if b["type"] == "alert"]
        assert len(alerts) == 0


class TestWardPatientsAlerts:
    def test_no_alerts_when_all_low_news(self):
        data = {"ward_id": "W1", "patients": [{"patient_id": "P001", "vitals": {"heart_rate": 70, "spo2": 98}, "news_score": 2}]}
        blocks = _build_ward_patients_blocks(data, {})
        alerts = [b for b in blocks if b["type"] == "alert"]
        assert len(alerts) == 0

    def test_multiple_high_news_alerts(self):
        data = {"ward_id": "W1", "patients": [
            {"patient_id": "P001", "vitals": {"heart_rate": 110, "spo2": 88}, "news_score": 7},
            {"patient_id": "P002", "vitals": {"heart_rate": 120, "spo2": 85}, "news_score": 9},
        ]}
        blocks = _build_ward_patients_blocks(data, {})
        alerts = [b for b in blocks if b["type"] == "alert"]
        assert len(alerts) == 2


class TestDrugInteractionsMultiple:
    def test_multiple_interactions(self):
        data = {"interactions": [
            {"drugs": ["warfarin", "aspirin"], "severity": "high", "description": "Bleeding"},
            {"drugs": ["metformin", "contrast"], "severity": "moderate", "description": "Lactic acidosis"},
        ]}
        blocks = _build_drug_interactions_blocks(data, {})
        assert len(blocks) == 2
        assert blocks[0]["severity"] == "critical"
        assert blocks[1]["severity"] == "warning"


class TestBuildVitalsTrendBlocks:
    def _make_trend_data(self, status="stable", confidence="low", n_readings=5):
        readings = []
        for i in range(n_readings):
            readings.append({
                "timestamp": f"2026-02-25T{8+i:02d}:00:00",
                "heart_rate": 75 + i,
                "bp_systolic": 120,
                "spo2": 98,
                "temperature": 37.0,
                "ews_score": 2 + (i if status == "deteriorating" else 0),
            })
        return {
            "patient_id": "P001",
            "hours": 24,
            "readings": readings,
            "trend": {
                "patient_status": status,
                "confidence": confidence,
                "slope": 0.05,
                "r_squared": 0.3,
                "p_value": 0.2,
                "recent_slope": 0.1,
                "clinical_interpretation": f"Average EWS: 2.1, Latest: 3. EWS {status}.",
            },
        }

    def test_stable_trend_no_alert(self):
        data = self._make_trend_data("stable", "low")
        blocks = _build_vitals_trend_blocks(data, {})
        types = [b["type"] for b in blocks]
        assert "data_table" in types
        assert "chart" in types
        assert "text" in types
        assert "alert" not in types

    def test_deteriorating_high_confidence_critical_alert(self):
        data = self._make_trend_data("deteriorating", "high")
        blocks = _build_vitals_trend_blocks(data, {})
        alerts = [b for b in blocks if b["type"] == "alert"]
        assert len(alerts) == 1
        assert alerts[0]["severity"] == "critical"

    def test_deteriorating_low_confidence_warning_alert(self):
        data = self._make_trend_data("deteriorating", "low")
        blocks = _build_vitals_trend_blocks(data, {})
        alerts = [b for b in blocks if b["type"] == "alert"]
        assert len(alerts) == 1
        assert alerts[0]["severity"] == "warning"

    def test_error_returns_single_text_block(self):
        data = {"error": "Not enough data"}
        blocks = _build_vitals_trend_blocks(data, {})
        assert len(blocks) == 1
        assert blocks[0]["type"] == "text"
        assert "Error" in blocks[0]["content"]

    def test_ews_column_in_table(self):
        data = self._make_trend_data("stable", "low")
        blocks = _build_vitals_trend_blocks(data, {})
        table = [b for b in blocks if b["type"] == "data_table"][0]
        assert "EWS" in table["columns"]

    def test_single_reading_no_chart(self):
        data = self._make_trend_data("stable", "low", n_readings=1)
        blocks = _build_vitals_trend_blocks(data, {})
        charts = [b for b in blocks if b["type"] == "chart"]
        assert len(charts) == 0
