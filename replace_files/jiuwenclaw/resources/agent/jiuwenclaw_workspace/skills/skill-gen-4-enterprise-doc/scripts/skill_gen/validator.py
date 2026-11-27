"""Validation helpers for skill generator."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


ALLOWED_FRONTMATTER_KEYS = {
    "name",
    "description",
    "license",
    "allowed-tools",
    "allowed_tools",
    "metadata",
    "compatibility",
}


def validate_skill(skill_path: Path) -> tuple[bool, str, dict[str, Any]]:
    """Validate a skill directory and return structured details."""
    skill_md = skill_path / "SKILL.md"
    details: dict[str, Any] = {"skill_path": str(skill_path), "errors": [], "warnings": []}

    if not skill_md.exists():
        details["errors"].append("SKILL.md not found")
        return False, "SKILL.md not found", details

    content = skill_md.read_text(encoding="utf-8")
    if not content.startswith("---"):
        details["errors"].append("No YAML frontmatter found")
        return False, "No YAML frontmatter found", details

    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        details["errors"].append("Invalid frontmatter format")
        return False, "Invalid frontmatter format", details

    try:
        frontmatter = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        details["errors"].append(f"Invalid YAML in frontmatter: {exc}")
        return False, f"Invalid YAML in frontmatter: {exc}", details

    if not isinstance(frontmatter, dict):
        details["errors"].append("Frontmatter must be a YAML dictionary")
        return False, "Frontmatter must be a YAML dictionary", details

    details["frontmatter"] = frontmatter
    unexpected = sorted(set(frontmatter.keys()) - ALLOWED_FRONTMATTER_KEYS)
    if unexpected:
        message = (
            f"Unexpected key(s) in SKILL.md frontmatter: {', '.join(unexpected)}. "
            f"Allowed properties are: {', '.join(sorted(ALLOWED_FRONTMATTER_KEYS))}"
        )
        details["errors"].append(message)
        return False, message, details

    name = str(frontmatter.get("name", "")).strip()
    description = str(frontmatter.get("description", "")).strip()

    if not name:
        details["errors"].append("Missing 'name' in frontmatter")
    elif not re.match(r"^[a-z0-9-]+$", name):
        details["errors"].append(
            f"Name '{name}' should be kebab-case (lowercase letters, digits, and hyphens only)"
        )
    elif name.startswith("-") or name.endswith("-") or "--" in name:
        details["errors"].append(
            f"Name '{name}' cannot start/end with hyphen or contain consecutive hyphens"
        )
    elif len(name) > 64:
        details["errors"].append(f"Name is too long ({len(name)} characters). Maximum is 64 characters.")

    if not description:
        details["errors"].append("Missing 'description' in frontmatter")
    elif "<" in description or ">" in description:
        details["errors"].append("Description cannot contain angle brackets (< or >)")
    elif len(description) > 1024:
        details["errors"].append(
            f"Description is too long ({len(description)} characters). Maximum is 1024 characters."
        )

    compatibility = frontmatter.get("compatibility", "")
    if compatibility and not isinstance(compatibility, str):
        details["errors"].append(f"Compatibility must be a string, got {type(compatibility).__name__}")
    elif isinstance(compatibility, str) and len(compatibility) > 500:
        details["errors"].append(
            f"Compatibility is too long ({len(compatibility)} characters). Maximum is 500 characters."
        )

    if len(content.splitlines()) > 500:
        details["warnings"].append("SKILL.md is longer than 500 lines; consider progressive disclosure.")

    _check_sop_specific(content, details)

    if details["errors"]:
        return False, details["errors"][0], details
    return True, "Skill is valid!", details


def _check_sop_specific(content: str, details: dict[str, Any]) -> None:
    """Add SOP-specific validation warnings (non-blocking)."""
    body_lower = content.lower()

    has_workflow_section = bool(
        re.search(r"^#{1,3}\s*(workflow|工作流|步骤|操作流程|流程)", content, re.MULTILINE | re.IGNORECASE)
    )
    if not has_workflow_section:
        details["warnings"].append(
            "SOP 技能建议包含 Workflow/步骤/操作流程 章节，以明确 SOP 步骤序列。"
        )

    tool_indicators = ["tool", "工具", "api", "系统", "file_", "read_", "write_", "search_", "execute"]
    has_tool_usage = any(indicator in body_lower for indicator in tool_indicators)
    if not has_tool_usage:
        details["warnings"].append(
            "SOP 技能通常需要 tool 调用（文件操作、搜索、API 等）。建议在 Workflow 中标明需要的工具。"
        )

    has_user_inputs = bool(
        re.search(
            r"^#{1,3}\s*(用户输入与数据|用户输入|预期输入|user\s+inputs?)",
            content,
            re.MULTILINE | re.IGNORECASE,
        )
    )
    if not has_user_inputs:
        details["warnings"].append(
            "建议增加「用户输入与数据」类章节，明确用户可能提供的材料与字段，便于 agent 执行。"
        )

    has_deliverables = bool(
        re.search(
            r"^#{1,3}\s*(标准交付物|交付物|预期输出|deliverables?)",
            content,
            re.MULTILINE | re.IGNORECASE,
        )
    )
    if not has_deliverables:
        details["warnings"].append(
            "建议增加「标准交付物」类章节，明确文件格式（如 .xlsx/.csv）、字段/列与校验要点。"
        )

    step_pattern = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)
    step_matches = step_pattern.findall(content)
    details.setdefault("info", {})["sop_step_count_in_body"] = len(step_matches)
