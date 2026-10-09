# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The research-paper-team skill must pass the framework's own team-skill check.

The skill lives under the workspace skills directory, which ships with the
submission package rather than with git, so the test skips where it is absent.
"""

import asyncio
import re
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

SKILL = (Path(__file__).resolve().parents[5]
         / "jiuwenswarm/resources/agent/workspace/skills/research-paper-team")

pytestmark = pytest.mark.skipif(not SKILL.is_dir(), reason="skill ships with the package")

ROLE_SECTIONS = (
    "## Identity", "## Success Criteria", "## Boundary",
    "## Output Schema", "## Inline Persona for Teammate",
)


def test_framework_validator_accepts_it():
    manager = SkillManager.__new__(SkillManager)
    result = asyncio.run(manager.handle_skills_team_skills_hub_validate({"path": str(SKILL)}))
    assert result["success"], result
    assert result["skill_type"] == "teamskills"


def test_required_files_exist():
    for name in ("SKILL.md", "workflow.md", "bind.md", "dependencies.yaml"):
        assert (SKILL / name).is_file(), name


def test_every_declared_role_has_a_complete_role_file():
    head = (SKILL / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
    role_ids = re.findall(r"^\s*-\s*id:\s*(\S+)", head, flags=re.M)
    assert len(role_ids) >= 2
    for role in role_ids:
        text = (SKILL / "roles" / f"{role}.md").read_text(encoding="utf-8")
        for section in ROLE_SECTIONS:
            assert section in text, f"{role}: {section}"
        assert "**Forbidden**" in text and "**Mandatory**" in text, role


def test_bind_declares_the_budgets_the_spec_requires():
    text = (SKILL / "bind.md").read_text(encoding="utf-8")
    for key in ("max_parallel_teammates", "total_wall_clock_budget", "total_token_budget"):
        assert key in text
