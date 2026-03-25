---
name: Hobot Skills Reference
description: Auto-generated reference of all clinical skills, their triggers, domains, and capabilities
generated: true
generator: scripts/generate_skills_doc.py
---

# Hobot Skills Reference

Skills are domain-specific capabilities that automatically interpret tool results
and provide clinical reasoning. The orchestrator auto-invokes the matching skill
after each tool call.

**This file is auto-generated.** Run `python scripts/generate_skills_doc.py --write` to regenerate.

## Interpretation Skills (auto-invoked after tool results)

### blood_availability

- **Class**: `BloodAvailabilitySkill` (blood_availability.py)
- **Domain**: bloodbank
- **Triggers**: `get_blood_availability`, `order_blood_crossmatch`
- **Required context**: none
- **Description**: Interprets blood bank availability and crossmatch results.

### care_plan_summary

- **Class**: `CarePlanSummarySkill` (care_plan_summary.py)
- **Domain**: core
- **Triggers**: `get_care_plan`
- **Required context**: none
- **Description**: Summarizes aggregated care plan (orders, appointments, reminders, pending labs).

### interpret_ecg

- **Class**: `InterpretECGSkill` (interpret_ecg.py)
- **Domain**: cardiology
- **Triggers**: `get_latest_ecg`, `get_event_ecg`
- **Required context**: demographics
- **Description**: Interprets ECG data using clinical reasoning model (metadata only).

### interpret_labs

- **Class**: `InterpretLabsSkill` (interpret_labs.py)
- **Domain**: pathology
- **Triggers**: `get_lab_results`
- **Required context**: demographics
- **Description**: Interprets lab results using clinical reasoning model.

### interpret_radiology

- **Class**: `InterpretRadiologySkill` (interpret_radiology.py)
- **Domain**: radiology
- **Triggers**: `get_report`
- **Required context**: none
- **Description**: Summarizes radiology reports (auto-intercept) and interprets images (tool).

### interpret_vitals

- **Class**: `InterpretVitalsSkill` (interpret_vitals.py)
- **Domain**: critical_care
- **Triggers**: `get_vitals`, `get_vitals_history`, `get_vitals_trend`
- **Required context**: demographics
- **Description**: Interprets vitals using clinical reasoning model + vitals anomaly model.

### medication_validation

- **Class**: `MedicationValidationSkill` (medication_validation.py)
- **Domain**: pharmacy
- **Triggers**: `check_drug_interactions`
- **Required context**: none
- **Description**: Validates drug interactions using backend + local rules.

### service_orchestration

- **Class**: `ServiceOrchestrationSkill` (service_orchestration.py)
- **Domain**: services
- **Triggers**: `request_housekeeping`, `order_diet`, `request_ambulance`, `schedule_appointment`
- **Required context**: none
- **Description**: Formats and confirms service request results.

## Workflow Skills (invoked by orchestrator for complex operations)

### clinical_context

- **Class**: `ClinicalContextSkill` (clinical_context.py)
- **Domain**: core
- **Required context**: none
- **Description**: Skill that builds patient clinical context.

### clinical_summary

- **Class**: `ClinicalSummarySkill` (clinical_summary.py)
- **Domain**: core
- **Required context**: none
- **Description**: Generates a structured patient summary from all clinical facts.

### risk_scoring

- **Class**: `RiskScoringSkill` (risk_scoring.py)
- **Domain**: critical_care
- **Required context**: none
- **Description**: Computes NEWS2/MEWS risk score from vitals data.

## Tool Skills (LLM-dispatched)

### analyze_radiology_image

- **Class**: `AnalyzeRadiologyImageSkill` (interpret_radiology.py)
- **Domain**: radiology
- **Required context**: none
- **Exposed as tool**: yes (LLM can invoke directly)
- **Description**: Vision-based radiology image interpretation (LLM-dispatched tool).
