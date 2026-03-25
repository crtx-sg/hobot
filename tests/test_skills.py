"""Unit tests for skills framework and individual skills."""

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "clinibot"))

from domain_models import ModelResult
from skills import BaseSkill, SkillInput, SkillOutput, SkillRegistry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_session():
    s = MagicMock()
    s.id = "test-session"
    s.tenant_id = "test-tenant"
    s.user_id = "test-user"
    s.channel = "webchat"
    return s


def _mock_clinical_model(content="Interpreted result"):
    model = AsyncMock()
    model.is_available = AsyncMock(return_value=True)
    model.predict = AsyncMock(return_value=ModelResult(
        content=content, model_name="gemini", model_version="test-model",
    ))
    return model


def _mock_vitals_model():
    model = AsyncMock()
    model.is_available = AsyncMock(return_value=True)
    model.predict = AsyncMock(return_value=ModelResult(
        content="NEWS2=3 (low risk)",
        findings=["heart_rate=95 (NEWS2 score: 1)"],
        score={"news2": 3, "risk": "low", "params": {"heart_rate": 1, "spo2": 0}},
        model_name="vitals_anomaly",
        model_version="1.0",
    ))
    return model


def _make_input(tool_name="get_vitals", tool_result=None, params=None, patient_id="P001"):
    return SkillInput(
        tool_name=tool_name,
        tool_result=tool_result or {},
        params=params or {"patient_id": patient_id},
        patient_id=patient_id,
        session_id="test-session",
        tenant_id="test-tenant",
    )


# ---------------------------------------------------------------------------
# SkillRegistry
# ---------------------------------------------------------------------------

class TestSkillRegistry:
    def test_register_and_get(self):
        registry = SkillRegistry()

        class DummySkill(BaseSkill):
            name = "dummy"
            domain = "test"
            interprets_tools = ["get_test"]
            async def run(self, input):
                return SkillOutput(status="success", domain="test", interpretation="ok")

        skill = DummySkill()
        registry.register(skill)

        assert registry.get_skill("dummy") is skill
        assert registry.get_interpreter("get_test") is skill
        assert registry.get_interpreter("get_unknown") is None

    def test_disabled_skill_not_returned(self):
        registry = SkillRegistry()

        class DummySkill(BaseSkill):
            name = "dummy"
            domain = "test"
            interprets_tools = ["get_test"]
            async def run(self, input):
                return SkillOutput(status="success", domain="test", interpretation="ok")

        skill = DummySkill(config={"enabled": False})
        registry.register(skill)
        assert registry.get_interpreter("get_test") is None

    def test_tool_definitions(self):
        registry = SkillRegistry()

        class ToolSkill(BaseSkill):
            name = "img_analyzer"
            domain = "radiology"
            interprets_tools = []
            async def run(self, input):
                return SkillOutput(status="success", domain="radiology", interpretation="ok")
            def tool_definition(self):
                return {"name": "analyze_image", "description": "Analyze", "parameters": {}}

        registry.register(ToolSkill())
        defs = registry.tool_definitions()
        assert len(defs) == 1
        assert defs[0]["name"] == "analyze_image"


# ---------------------------------------------------------------------------
# SkillOutput
# ---------------------------------------------------------------------------

class TestSkillOutput:
    def test_to_analysis_dict(self):
        output = SkillOutput(
            status="success",
            domain="pathology",
            interpretation="BUN elevated",
            findings=["BUN: 28 mg/dL (ref 7-20)"],
            context_used=["demographics", "medication"],
            context_summary="72M, lisinopril",
            provider_name="gemini",
            model_name="gemini-2.5-flash",
            score={"news2": 3},
        )
        d = output.to_analysis_dict()
        assert d["status"] == "success"
        assert d["domain"] == "pathology"
        assert d["interpretation"] == "BUN elevated"
        assert "demographics" in d["context_used"]
        assert d["score"] == {"news2": 3}
        assert "status_reason" not in d

    def test_to_analysis_dict_with_reason(self):
        output = SkillOutput(
            status="partial", domain="cardiology",
            interpretation="ECG metadata only",
            status_reason="limited_capability",
        )
        d = output.to_analysis_dict()
        assert d["status_reason"] == "limited_capability"


# ---------------------------------------------------------------------------
# InterpretVitalsSkill
# ---------------------------------------------------------------------------

class TestInterpretVitalsSkill:
    @pytest.mark.asyncio
    async def test_success_with_context(self):
        from skills.interpret_vitals import InterpretVitalsSkill

        mock_demo = [{"fact_type": "demographics", "fact_data": {"age": 72}}]

        async def mock_get_facts(pid, tid, fact_type=None, limit=50):
            if fact_type == "demographics":
                return mock_demo
            return []

        skill = InterpretVitalsSkill(
            domain_models={
                "clinical_reasoning": _mock_clinical_model("BP elevated, consider adjusting meds"),
                "vitals_anomaly": _mock_vitals_model(),
            },
        )

        input = _make_input(
            tool_name="get_vitals",
            tool_result={"heart_rate": 95, "bp_systolic": 150, "spo2": 97},
        )

        with patch("clinical_memory.get_facts", side_effect=mock_get_facts), \
             patch("skills.clinical_context.fetch_from_backend", return_value=None):
            output = await skill.run(input)

        assert output.status == "success"
        assert output.domain == "critical_care"
        assert "BP elevated" in output.interpretation
        assert output.score is not None
        assert output.score["news2"] == 3

    @pytest.mark.asyncio
    async def test_skips_without_demographics(self):
        from skills.interpret_vitals import InterpretVitalsSkill

        async def mock_get_facts(pid, tid, fact_type=None, limit=50):
            return []

        skill = InterpretVitalsSkill(domain_models={
            "clinical_reasoning": _mock_clinical_model(),
            "vitals_anomaly": _mock_vitals_model(),
        })

        input = _make_input(tool_name="get_vitals", tool_result={"heart_rate": 80})

        with patch("clinical_memory.get_facts", side_effect=mock_get_facts), \
             patch("skills.clinical_context.fetch_from_backend", return_value=None):
            output = await skill.run(input)

        assert output.status == "skipped"
        assert output.status_reason == "missing_required_context"


# ---------------------------------------------------------------------------
# InterpretLabsSkill
# ---------------------------------------------------------------------------

class TestInterpretLabsSkill:
    @pytest.mark.asyncio
    async def test_success(self):
        from skills.interpret_labs import InterpretLabsSkill

        mock_demo = [{"fact_type": "demographics", "fact_data": {"age": 72}}]

        async def mock_get_facts(pid, tid, fact_type=None, limit=50):
            if fact_type == "demographics":
                return mock_demo
            return []

        skill = InterpretLabsSkill(domain_models={
            "clinical_reasoning": _mock_clinical_model("WBC within normal limits"),
        })

        input = _make_input(
            tool_name="get_lab_results",
            tool_result={"patient_id": "P001", "labs": [{"WBC": "7.2"}]},
        )

        with patch("clinical_memory.get_facts", side_effect=mock_get_facts), \
             patch("skills.clinical_context.fetch_from_backend", return_value=None):
            output = await skill.run(input)

        assert output.status == "success"
        assert output.domain == "pathology"

    @pytest.mark.asyncio
    async def test_skips_missing_context(self):
        from skills.interpret_labs import InterpretLabsSkill

        async def mock_get_facts(pid, tid, fact_type=None, limit=50):
            return []

        skill = InterpretLabsSkill(domain_models={
            "clinical_reasoning": _mock_clinical_model(),
        })

        input = _make_input(tool_name="get_lab_results", tool_result={"labs": []})

        with patch("clinical_memory.get_facts", side_effect=mock_get_facts), \
             patch("skills.clinical_context.fetch_from_backend", return_value=None):
            output = await skill.run(input)

        assert output.status == "skipped"


# ---------------------------------------------------------------------------
# InterpretECGSkill
# ---------------------------------------------------------------------------

class TestInterpretECGSkill:
    @pytest.mark.asyncio
    async def test_preprocessor_applied(self):
        from skills.interpret_ecg import InterpretECGSkill

        mock_demo = [{"fact_type": "demographics", "fact_data": {"age": 65}}]

        async def mock_get_facts(pid, tid, fact_type=None, limit=50):
            if fact_type == "demographics":
                return mock_demo
            return []

        skill = InterpretECGSkill(domain_models={
            "clinical_reasoning": _mock_clinical_model("ECG metadata analysis"),
        })

        ecg_data = {
            "patient_id": "P001",
            "ecg": {
                "sampling_rate_hz": 500,
                "duration_s": 10,
                "leads": {"I": [0.1], "II": [0.3]},
            },
        }

        input = _make_input(tool_name="get_latest_ecg", tool_result=ecg_data)

        with patch("clinical_memory.get_facts", side_effect=mock_get_facts), \
             patch("skills.clinical_context.fetch_from_backend", return_value=None):
            output = await skill.run(input)

        assert output.status == "partial"
        assert output.status_reason == "limited_capability"


# ---------------------------------------------------------------------------
# ECG Stub Preprocessor
# ---------------------------------------------------------------------------

class TestECGStubPreprocessor:
    def test_strips_arrays(self):
        from skills.interpret_ecg import ecg_stub_preprocessor
        data = {
            "ecg": {
                "sampling_rate_hz": 500,
                "duration_s": 10,
                "leads": {"I": [0.1], "II": [0.3], "V1": [0.5]},
                "waveform": [1, 2, 3],
            }
        }
        result, capability = ecg_stub_preprocessor(data)
        assert capability == "limited"
        ecg = result["ecg"]
        assert "leads" not in ecg
        assert "waveform" not in ecg
        assert ecg["lead_count"] == 3
        assert set(ecg["lead_names"]) == {"I", "II", "V1"}


# ---------------------------------------------------------------------------
# MedicationValidationSkill
# ---------------------------------------------------------------------------

class TestMedicationValidationSkill:
    @pytest.mark.asyncio
    async def test_with_backend_result(self):
        from skills.medication_validation import MedicationValidationSkill

        drug_model = AsyncMock()
        drug_model.predict = AsyncMock(return_value=ModelResult(
            content="No interactions found.",
            findings=[],
            model_name="drug_interaction",
            model_version="1.0",
        ))

        skill = MedicationValidationSkill(domain_models={"drug_interaction": drug_model})

        input = _make_input(
            tool_name="check_drug_interactions",
            tool_result={"interactions": []},
            params={"medications": ["aspirin", "metformin"]},
        )

        output = await skill.run(input)
        assert output.status == "success"


# ---------------------------------------------------------------------------
# VitalsAnomalyModel (domain model)
# ---------------------------------------------------------------------------

class TestVitalsAnomalyModel:
    @pytest.mark.asyncio
    async def test_news2_scoring(self):
        from domain_models.vitals_anomaly import VitalsAnomalyModel

        model = VitalsAnomalyModel()
        result = await model.predict({
            "vitals": {
                "heart_rate": 95,
                "bp_systolic": 150,
                "spo2": 97,
                "temperature": 37.5,
                "respiration_rate": 18,
            },
        })

        assert result.score is not None
        assert "news2" in result.score
        assert result.score["risk"] in ("none", "low", "medium", "high")

    @pytest.mark.asyncio
    async def test_high_risk(self):
        from domain_models.vitals_anomaly import VitalsAnomalyModel

        model = VitalsAnomalyModel()
        result = await model.predict({
            "vitals": {
                "heart_rate": 135,
                "bp_systolic": 85,
                "spo2": 90,
                "temperature": 39.5,
                "respiration_rate": 26,
            },
        })

        assert result.score["risk"] == "high"
        assert result.score["news2"] >= 7

    @pytest.mark.asyncio
    async def test_always_available(self):
        from domain_models.vitals_anomaly import VitalsAnomalyModel
        model = VitalsAnomalyModel()
        assert await model.is_available() is True


# ---------------------------------------------------------------------------
# DrugInteractionModel
# ---------------------------------------------------------------------------

class TestDrugInteractionModel:
    @pytest.mark.asyncio
    async def test_local_rules(self):
        from domain_models.drug_interaction import DrugInteractionModel

        model = DrugInteractionModel()
        result = await model.predict({
            "medications": ["warfarin", "aspirin"],
            "backend_result": None,
        })

        assert "bleeding" in result.content.lower()
        assert len(result.findings) > 0

    @pytest.mark.asyncio
    async def test_no_interactions(self):
        from domain_models.drug_interaction import DrugInteractionModel

        model = DrugInteractionModel()
        result = await model.predict({
            "medications": ["metformin", "lisinopril"],
            "backend_result": None,
        })

        assert "no known" in result.content.lower()


# ---------------------------------------------------------------------------
# RiskScoringSkill
# ---------------------------------------------------------------------------

class TestRiskScoringSkill:
    @pytest.mark.asyncio
    async def test_scoring(self):
        from skills.risk_scoring import RiskScoringSkill

        skill = RiskScoringSkill(domain_models={"vitals_anomaly": _mock_vitals_model()})
        input = _make_input(
            tool_result={"heart_rate": 95, "bp_systolic": 150, "spo2": 97},
        )

        output = await skill.run(input)
        assert output.status == "success"
        assert output.score is not None


# ---------------------------------------------------------------------------
# BloodAvailabilitySkill
# ---------------------------------------------------------------------------

class TestBloodAvailabilitySkill:
    @pytest.mark.asyncio
    async def test_low_stock_alert(self):
        from skills.blood_availability import BloodAvailabilitySkill

        skill = BloodAvailabilitySkill()
        input = _make_input(
            tool_name="get_blood_availability",
            tool_result={
                "A+": {"units": 2},
                "O-": {"units": 15},
                "B+": {"units": 3},
            },
        )

        output = await skill.run(input)
        assert output.status == "success"
        assert len(output.findings) == 2  # A+ and B+ are low


# ---------------------------------------------------------------------------
# ServiceOrchestrationSkill
# ---------------------------------------------------------------------------

class TestServiceOrchestrationSkill:
    @pytest.mark.asyncio
    async def test_formats_result(self):
        from skills.service_orchestration import ServiceOrchestrationSkill

        skill = ServiceOrchestrationSkill()
        input = _make_input(
            tool_name="order_diet",
            tool_result={
                "id": "DIET-001",
                "status": "confirmed",
                "type": "Diet Order",
                "patient_id": "P001",
                "diet_type": "diabetic",
                "meal": "lunch",
            },
        )

        output = await skill.run(input)
        assert output.status == "success"
        assert "DIET-001" in output.interpretation
        assert "diabetic" in output.interpretation
