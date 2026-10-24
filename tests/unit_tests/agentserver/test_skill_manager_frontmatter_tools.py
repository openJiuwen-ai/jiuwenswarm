"""Skill frontmatter compatibility tests for tool allowlists."""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


@pytest.mark.parametrize("field", ["allowed-tools", "allowed_tools"])
def test_parse_skill_md_accepts_canonical_and_legacy_allowed_tools(
    tmp_path: Path,
    field: str,
) -> None:
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text(
        "---\n"
        "name: tool-limited\n"
        "description: test skill\n"
        f"{field}: [mcp_exec_command]\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )

    parsed = SkillManager._parse_skill_md(skill_file)

    assert parsed is not None
    assert parsed["allowed_tools"] == ["mcp_exec_command"]
