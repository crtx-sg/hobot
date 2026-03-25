"""Prompt templates for clinical analyzers."""

ANALYZER_PROMPTS = {
    # Intercept (self-contained, no context)
    "radiology_report_summary": """Summarize the following radiology report. Highlight key findings, impressions, and any recommendations for follow-up.

Report:
{data}""",

    # Tool analyzers (context-dependent, capability-agnostic)
    "lab_analysis": """Review the following lab results as a clinical pathologist and write a clinical interpretation.

Patient context: {context}

Lab results:
{data}

In your interpretation, cover each result with its reference range and whether normal or abnormal, any critical values requiring immediate attention, and clinical correlation interpreting abnormal findings in context of patient conditions and medications.""",

    "ecg_analysis": """Interpret the following ECG data as a cardiologist and write a clinical assessment.

Patient context: {context}

ECG data:
{data}

In your assessment, cover rhythm, rate, axis, and any abnormalities, clinical correlation with patient conditions and medications, and recommended follow-up actions.""",

    "vitals_analysis": """Patient: {context}

Vital signs: {data}

Hospital thresholds ({scoring} scoring):
{thresholds}

Provide a detailed clinical assessment in plain text. Assess each vital sign against the hospital thresholds above, flag any values outside normal or critical ranges, consider medication effects such as beta-blockers on heart rate and antihypertensives on blood pressure, and provide 2-4 specific recommendations.

Clinical Assessment:""",

    "radiology_image_analysis": """Interpret the following medical image as a radiologist. The patient context is provided below.

Patient context: {context}

Describe findings systematically including pathology, alignment, foreign bodies, and air/fluid levels. Consider patient history when interpreting (known conditions vs new findings). Recommend follow-up imaging if appropriate.""",
}
