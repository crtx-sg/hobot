#!/usr/bin/env python3
"""Standalone skill tester — run any skill against live synthetic backends.

Usage:
    # Backends run on Docker-internal hostnames, so either:
    #   A) Run inside the gateway container:
    #      docker compose exec clinibot-gateway python /app/scripts/test_skill.py ...
    #   B) Use --mock to supply tool results without backend calls (works from host)
    #   C) Set env overrides: MONITORING_BASE=http://localhost:... (if ports exposed)

    # List all registered skills
    python scripts/test_skill.py --list

    # Test with mock data (no backend needed, --demo default provides test patient)
    python scripts/test_skill.py interpret_vitals --tool get_vitals \
        --mock '{"heart_rate":95,"bp_systolic":150,"spo2":97,"temperature":37.5}' \
        --patient P001 --demo default

    python scripts/test_skill.py blood_availability --tool get_blood_availability \
        --mock '{"A+":{"units":2},"O-":{"units":15},"B+":{"units":3}}'

    python scripts/test_skill.py medication_validation --tool check_drug_interactions \
        --mock '{"interactions":[]}' \
        --params '{"medications": ["warfarin", "aspirin"]}'

    python scripts/test_skill.py service_orchestration --tool order_diet \
        --mock '{"id":"DIET-1","status":"confirmed","type":"Diet Order","diet_type":"diabetic"}'

    python scripts/test_skill.py risk_scoring --tool get_vitals \
        --mock '{"heart_rate":135,"bp_systolic":85,"spo2":90,"temperature":39.5,"respiration_rate":26}' \
        --patient P003 --demo default

    # Custom demographics
    python scripts/test_skill.py interpret_vitals --tool get_vitals \
        --mock '{"heart_rate":95,"bp_systolic":150,"spo2":97}' --patient P001 \
        --demo '{"age":45,"gender":"Female","allergies":[]}'

    # Test against live backends (run inside container or with exposed ports)
    python scripts/test_skill.py interpret_vitals --tool get_vitals --patient P001

    # Verbose mode (show raw tool result + full skill output)
    python scripts/test_skill.py interpret_vitals --tool get_vitals --patient P001 -v
"""

import argparse
import asyncio
import json
import os
import sys

# Add clinibot to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "clinibot"))


def _load_dotenv():
    """Load .env file into os.environ (simple key=value parser)."""
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if key and key not in os.environ:
                    os.environ[key] = val


def load_config():
    config_path = os.path.join(os.path.dirname(__file__), "..", "config", "config.json")
    with open(config_path) as f:
        return json.load(f)


class _MockSession:
    """Minimal session stub for tools._dispatch logging."""
    id = "test-session"
    tenant_id = "T1"


async def fetch_tool_result(tool_name: str, params: dict, config: dict) -> dict:
    """Call a tool against the live synthetic backend."""
    import tools
    tools.load_domain_config(
        os.path.join(os.path.dirname(__file__), "..", "config", "config.json")
    )
    tools.init_http_client()
    result = await tools._dispatch(tool_name, params, _MockSession())
    return result


async def build_skill_registry(config: dict):
    """Build the skill registry with domain models (mirrors main._init_skill_registry)."""
    from domain_models.clinical_reasoning import ClinicalReasoningModel
    from domain_models.radiology_model import RadiologyModel
    from domain_models.vitals_anomaly import VitalsAnomalyModel
    from domain_models.drug_interaction import DrugInteractionModel
    from skills import SkillRegistry
    from skills.blood_availability import BloodAvailabilitySkill
    from skills.clinical_context import ClinicalContextSkill
    from skills.clinical_summary import ClinicalSummarySkill
    from skills.interpret_ecg import InterpretECGSkill
    from skills.interpret_labs import InterpretLabsSkill
    from skills.interpret_radiology import AnalyzeRadiologyImageSkill, InterpretRadiologySkill
    from skills.interpret_vitals import InterpretVitalsSkill
    from skills.medication_validation import MedicationValidationSkill
    from skills.risk_scoring import RiskScoringSkill
    from skills.service_orchestration import ServiceOrchestrationSkill

    skills_config = config.get("skills", {})
    domain_models = {
        "clinical_reasoning": ClinicalReasoningModel(
            provider_name=skills_config.get("clinical_reasoning", {}).get("provider", "gemini"),
        ),
        "radiology_model": RadiologyModel(
            provider_name=skills_config.get("radiology_model", {}).get("provider", "gemini"),
        ),
        "vitals_anomaly": VitalsAnomalyModel(),
        "drug_interaction": DrugInteractionModel(),
    }

    registry = SkillRegistry()
    skill_classes = [
        InterpretVitalsSkill, InterpretLabsSkill, InterpretRadiologySkill,
        AnalyzeRadiologyImageSkill, InterpretECGSkill, MedicationValidationSkill,
        BloodAvailabilitySkill, ServiceOrchestrationSkill, ClinicalContextSkill,
        ClinicalSummarySkill, RiskScoringSkill,
    ]
    for cls in skill_classes:
        skill_cfg = skills_config.get(cls.name, {}) if hasattr(cls, 'name') else {}
        skill = cls(config=skill_cfg, domain_models=domain_models)
        registry.register(skill)

    return registry


async def init_clinical_memory(config: dict):
    """Initialize clinical memory with in-memory SQLite."""
    import clinical_memory
    import aiosqlite
    schema_path = os.path.join(os.path.dirname(__file__), "..", "schema", "init.sql")
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    if os.path.exists(schema_path):
        with open(schema_path) as f:
            await db.executescript(f.read())
    clinical_memory._db = db
    return db


_DEFAULT_DEMOGRAPHICS = {
    "patient_id": "P001",
    "name": "Test Patient",
    "age": 65,
    "gender": "Male",
    "blood_group": "O+",
    "allergies": ["Penicillin"],
    "active_medications": ["Metoprolol 50mg", "Amlodipine 5mg"],
}


# Docker hostname -> localhost rewrites for host-mode testing
# Docker Ollama is exposed on port 11435 (host Ollama uses 11434)
_HOST_REWRITES = {
    "http://ollama:11434": "http://localhost:11435",
    "http://vllm-medgemma:8000": "http://localhost:8000",
}


async def run_skill(skill_name: str, tool_name: str, patient_id: str,
                    params_json: str, mock_data: str, mock_demo: str,
                    provider_override: str, host_mode: bool,
                    verbose: bool):
    config = load_config()

    # Resolve env vars in config (for API keys)
    import re
    config_str = json.dumps(config)
    for match in re.finditer(r'\$\{(\w+)\}', config_str):
        env_key = match.group(1)
        env_val = os.environ.get(env_key, "")
        config_str = config_str.replace(match.group(0), env_val)
    config = json.loads(config_str)

    # Rewrite Docker hostnames to localhost for host-mode testing
    if host_mode:
        for docker_url, local_url in _HOST_REWRITES.items():
            for pname, pconf in config.get("providers", {}).items():
                if pconf.get("baseUrl") == docker_url:
                    pconf["baseUrl"] = local_url
                    print(f"Host-mode: {pname} -> {local_url}")

    # Override skill provider if requested
    if provider_override:
        for sname, sconf in config.get("skills", {}).items():
            if "provider" in sconf:
                sconf["provider"] = provider_override
        print(f"Provider override: all skills -> {provider_override}")

    # Initialize providers (LLM backends)
    import providers
    config_path = os.path.join(os.path.dirname(__file__), "..", "config", "config.json")
    providers.load_providers(config_path, config_override=config)

    registry = await build_skill_registry(config)
    db = await init_clinical_memory(config)

    skill = registry.get_skill(skill_name)
    if not skill:
        print(f"ERROR: Skill '{skill_name}' not found.")
        print(f"Available: {[s.name for s in registry.all_skills()]}")
        sys.exit(1)

    print(f"Skill:   {skill.name}")
    print(f"Domain:  {skill.domain}")
    print(f"Enabled: {skill.enabled}")
    print(f"Triggers: {skill.interprets_tools}")
    print()

    # Build params
    params = json.loads(params_json) if params_json else {}
    if patient_id:
        params["patient_id"] = patient_id

    # Get tool result
    if mock_data:
        tool_result = json.loads(mock_data)
        print(f"Using mock data for {tool_name}")
    elif tool_name:
        print(f"Calling {tool_name}({json.dumps(params)}) ...")
        try:
            tool_result = await fetch_tool_result(tool_name, params, config)
        except Exception as e:
            print(f"ERROR calling tool: {e}")
            sys.exit(1)
    else:
        tool_result = {}

    if verbose:
        print(f"\n--- Tool Result ---")
        print(json.dumps(tool_result, indent=2, default=str)[:2000])
        print()

    # Load demographics into clinical memory
    import clinical_memory
    if patient_id and tool_name != "get_patient":
        if mock_demo:
            if mock_demo == "default":
                demo_result = dict(_DEFAULT_DEMOGRAPHICS, patient_id=patient_id)
            else:
                demo_result = json.loads(mock_demo)
            await clinical_memory.extract_and_store(
                "get_patient", demo_result, patient_id, "test-session", "T1")
            print(f"Loaded mock demographics for {patient_id}")
        else:
            try:
                demo_result = await fetch_tool_result("get_patient", {"patient_id": patient_id}, config)
                await clinical_memory.extract_and_store(
                    "get_patient", demo_result, patient_id, "test-session", "T1")
                print(f"Loaded demographics for {patient_id}")
            except Exception:
                print(f"Warning: could not load demographics (use --demo default to skip backend)")

    # Store the tool result itself in clinical memory
    if patient_id and tool_name:
        import clinical_memory
        await clinical_memory.extract_and_store(
            tool_name, tool_result, patient_id, "test-session", "T1")

    # Build skill input
    from skills import SkillInput
    skill_input = SkillInput(
        tool_name=tool_name or "",
        tool_result=tool_result,
        params=params,
        patient_id=patient_id or "",
        session_id="test-session",
        tenant_id="T1",
    )

    # Run the skill
    print(f"\nRunning {skill.name} ...")
    try:
        output = await asyncio.wait_for(skill.run(skill_input), timeout=skill.timeout)
    except asyncio.TimeoutError:
        print(f"TIMEOUT after {skill.timeout}s")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # Display result
    print(f"\n{'='*60}")
    print(f"Status:  {output.status}" + (f" ({output.status_reason})" if output.status_reason else ""))
    print(f"Domain:  {output.domain}")
    if output.score:
        print(f"Score:   {json.dumps(output.score)}")
    if output.context_used:
        print(f"Context: {output.context_used}")
    if output.provider_name:
        print(f"Model:   {output.provider_name}/{output.model_name}")
    print(f"{'='*60}")
    print(f"\n{output.interpretation}")
    if output.findings:
        print(f"\nFindings:")
        for f in output.findings:
            print(f"  - {f}")

    if verbose:
        print(f"\n--- Full SkillOutput ---")
        print(json.dumps(output.to_analysis_dict(), indent=2, default=str))

    await db.close()


def list_skills(config):
    """Synchronously list all skills."""
    async def _list():
        registry = await build_skill_registry(config)
        print(f"{'Skill':<28} {'Domain':<15} {'Triggers':<45} {'Enabled'}")
        print("-" * 100)
        for s in sorted(registry.all_skills(), key=lambda x: x.name):
            triggers = ", ".join(s.interprets_tools) or "(explicit)"
            print(f"{s.name:<28} {s.domain:<15} {triggers:<45} {s.enabled}")
    asyncio.run(_list())


def main():
    _load_dotenv()
    parser = argparse.ArgumentParser(
        description="Test a skill standalone against live backends",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("skill", nargs="?", help="Skill name (e.g. interpret_vitals)")
    parser.add_argument("--tool", "-t", help="Tool name to call (e.g. get_vitals)")
    parser.add_argument("--patient", "-p", default="", help="Patient ID (e.g. P001)")
    parser.add_argument("--params", help="Extra params as JSON string")
    parser.add_argument("--mock", help="Mock tool result as JSON (skip backend call)")
    parser.add_argument("--demo", help="Mock demographics JSON (skip backend fetch). "
                        'Use "default" for built-in test patient.')
    parser.add_argument("--provider", help="Override LLM provider for the skill "
                        "(e.g. anthropic, gemini, ollama)")
    parser.add_argument("--host-mode", action="store_true",
                        help="Rewrite Docker hostnames (ollama:11434, etc.) to localhost")
    parser.add_argument("--list", "-l", action="store_true", help="List all registered skills")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show full tool result and skill output")
    args = parser.parse_args()

    config = load_config()

    if args.list:
        list_skills(config)
        return

    if not args.skill:
        parser.print_help()
        sys.exit(1)

    asyncio.run(run_skill(
        skill_name=args.skill,
        tool_name=args.tool or "",
        patient_id=args.patient,
        params_json=args.params or "",
        mock_data=args.mock or "",
        mock_demo=args.demo or "",
        provider_override=args.provider or "",
        host_mode=args.host_mode,
        verbose=args.verbose,
    ))


if __name__ == "__main__":
    main()
