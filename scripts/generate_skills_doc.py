#!/usr/bin/env python3
"""Auto-generate SKILLS.md from skill source code.

Usage:
    python scripts/generate_skills_doc.py          # prints to stdout
    python scripts/generate_skills_doc.py --write   # writes SKILLS.md
"""

import argparse
import ast
import os
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "clinibot" / "skills"
OUTPUT_FILE = REPO_ROOT / "SKILLS.md"


def extract_skill_classes(filepath: Path) -> list[dict]:
    """Parse a skill file and extract class metadata via AST."""
    source = filepath.read_text()
    tree = ast.parse(source, filename=str(filepath))
    skills = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        # Only classes that inherit from BaseSkill (or have skill-like attrs)
        base_names = [
            getattr(b, "id", getattr(b, "attr", "")) for b in node.bases
        ]
        if "BaseSkill" not in base_names:
            continue

        info: dict = {
            "class_name": node.name,
            "file": filepath.name,
            "name": "",
            "domain": "",
            "required_context": [],
            "interprets_tools": [],
            "docstring": ast.get_docstring(node) or "",
            "has_tool_definition": False,
        }

        for item in node.body:
            # Class-level assignments: name = "...", domain = "...", etc.
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    attr = getattr(target, "id", getattr(target, "attr", None))
                    if attr is None:
                        continue
                    val = _eval_literal(item.value)
                    if attr == "name" and isinstance(val, str):
                        info["name"] = val
                    elif attr == "domain" and isinstance(val, str):
                        info["domain"] = val
                    elif attr == "required_context" and isinstance(val, list):
                        info["required_context"] = val
                    elif attr == "interprets_tools" and isinstance(val, list):
                        info["interprets_tools"] = val

            # Check for tool_definition method
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if item.name == "tool_definition":
                    info["has_tool_definition"] = True

        if info["name"]:
            skills.append(info)

    return skills


def _eval_literal(node):
    """Safely evaluate simple AST literals."""
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return None


def classify_skill(info: dict) -> str:
    """Classify a skill as interpretation, workflow, or tool."""
    if info["has_tool_definition"]:
        return "tool"
    if info["interprets_tools"]:
        return "interpretation"
    return "workflow"


def generate_markdown(all_skills: list[dict]) -> str:
    """Generate the SKILLS.md content."""
    interpretation = []
    workflow = []
    tool = []

    for s in all_skills:
        cat = classify_skill(s)
        if cat == "interpretation":
            interpretation.append(s)
        elif cat == "tool":
            tool.append(s)
        else:
            workflow.append(s)

    lines = [
        "---",
        "name: Hobot Skills Reference",
        "description: Auto-generated reference of all clinical skills, their triggers, domains, and capabilities",
        "generated: true",
        "generator: scripts/generate_skills_doc.py",
        "---",
        "",
        "# Hobot Skills Reference",
        "",
        "Skills are domain-specific capabilities that automatically interpret tool results",
        "and provide clinical reasoning. The orchestrator auto-invokes the matching skill",
        "after each tool call.",
        "",
        "**This file is auto-generated.** Run `python scripts/generate_skills_doc.py --write` to regenerate.",
        "",
    ]

    if interpretation:
        lines.append("## Interpretation Skills (auto-invoked after tool results)")
        lines.append("")
        for s in sorted(interpretation, key=lambda x: x["name"]):
            lines.extend(_format_skill(s))

    if workflow:
        lines.append("## Workflow Skills (invoked by orchestrator for complex operations)")
        lines.append("")
        for s in sorted(workflow, key=lambda x: x["name"]):
            lines.extend(_format_skill(s))

    if tool:
        lines.append("## Tool Skills (LLM-dispatched)")
        lines.append("")
        for s in sorted(tool, key=lambda x: x["name"]):
            lines.extend(_format_skill(s))

    return "\n".join(lines)


def _format_skill(s: dict) -> list[str]:
    """Format a single skill as markdown lines."""
    lines = [f"### {s['name']}"]
    lines.append("")
    lines.append(f"- **Class**: `{s['class_name']}` ({s['file']})")
    lines.append(f"- **Domain**: {s['domain']}")

    if s["interprets_tools"]:
        tools_str = ", ".join(f"`{t}`" for t in s["interprets_tools"])
        lines.append(f"- **Triggers**: {tools_str}")

    if s["required_context"]:
        ctx = ", ".join(s["required_context"])
        lines.append(f"- **Required context**: {ctx}")
    else:
        lines.append("- **Required context**: none")

    if s["has_tool_definition"]:
        lines.append("- **Exposed as tool**: yes (LLM can invoke directly)")

    if s["docstring"]:
        # Use first paragraph of docstring as description
        desc = s["docstring"].split("\n\n")[0].strip()
        desc = " ".join(desc.split())  # normalize whitespace
        lines.append(f"- **Description**: {desc}")

    lines.append("")
    return lines


def main():
    parser = argparse.ArgumentParser(description="Generate SKILLS.md from skill source code")
    parser.add_argument("--write", action="store_true", help="Write to SKILLS.md (default: stdout)")
    args = parser.parse_args()

    all_skills = []
    for py_file in sorted(SKILLS_DIR.glob("*.py")):
        if py_file.name == "__init__.py":
            continue
        skills = extract_skill_classes(py_file)
        all_skills.extend(skills)

    if not all_skills:
        print("No skills found!", file=sys.stderr)
        sys.exit(1)

    md = generate_markdown(all_skills)

    if args.write:
        OUTPUT_FILE.write_text(md)
        print(f"Wrote {OUTPUT_FILE} ({len(all_skills)} skills)")
    else:
        print(md)


if __name__ == "__main__":
    main()
