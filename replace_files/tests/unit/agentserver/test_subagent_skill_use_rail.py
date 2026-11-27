# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for SubagentSkillUseRail.

Covers the two overrides relative to the upstream SkillUseRail:
- ``before_model_call`` injects a lightweight skill_tool / skill_complete
  guidance section (no available-skills listing) so the subagent knows the
  tool exists and prefers it over read_file when the parent task references
  a skill by name or by SKILL.md path.
- ``before_invoke`` consumes pending active-skill hints but skips
  ``_prepare_skills`` and ``_fetch_evolution_texts`` (skill_tool is inherited
  from the parent agent, so subagent-side skill scanning is unnecessary).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openjiuwen.harness.prompts.sections import SectionName

from jiuwenclaw.agentserver.tools.subagent_executor.skill_use_rail_subagent import (
    SubagentSkillUseRail,
)


@pytest.fixture
def rail(tmp_path) -> SubagentSkillUseRail:
    """Build a SubagentSkillUseRail wired to a temp empty skills_dir.

    skills_dir cannot be empty (parent ``_prepare_skills`` raises ValueError),
    so we point at an empty tmp dir which the parent simply skips.
    """
    return SubagentSkillUseRail(
        skills_dir=[str(tmp_path)],
        skill_mode=SubagentSkillUseRail.SKILL_MODE_ALL,
        include_tools=False,
        include_skill_body_tools=False,
    )


@pytest.mark.asyncio
async def test_before_model_call_injects_cn_skill_tool_guidance(
    rail: SubagentSkillUseRail,
) -> None:
    builder = MagicMock()
    builder.language = "cn"
    rail.system_prompt_builder = builder

    await rail.before_model_call(MagicMock())

    builder.add_section.assert_called_once()
    section = builder.add_section.call_args.args[0]
    assert section.name == SectionName.SKILLS
    assert section.priority == 40
    body = section.content["cn"]
    assert body.startswith("# 技能")
    assert "skill_tool" in body
    assert "skill_complete" in body
    # Subagent variant must NOT enumerate available skills.
    assert "可用技能" not in body


@pytest.mark.asyncio
async def test_before_model_call_injects_en_skill_tool_guidance(
    rail: SubagentSkillUseRail,
) -> None:
    builder = MagicMock()
    builder.language = "en"
    rail.system_prompt_builder = builder

    await rail.before_model_call(MagicMock())

    section = builder.add_section.call_args.args[0]
    body = section.content["en"]
    assert body.startswith("# Skills")
    assert "skill_tool" in body
    assert "skill_complete" in body
    assert "Available skills" not in body


@pytest.mark.asyncio
async def test_before_model_call_no_builder_is_noop(
    rail: SubagentSkillUseRail,
) -> None:
    rail.system_prompt_builder = None
    # Should not raise.
    await rail.before_model_call(MagicMock())


@pytest.mark.asyncio
async def test_before_invoke_consumes_hints_and_skips_prep(
    rail: SubagentSkillUseRail,
) -> None:
    ctx = MagicMock()
    with patch.object(
        rail, "_prepare_skills", new=AsyncMock()
    ) as mock_prepare, patch.object(
        rail, "_fetch_evolution_texts", new=AsyncMock()
    ) as mock_fetch, patch.object(
        rail, "_consume_pending_active_skill_hints"
    ) as mock_hints:
        await rail.before_invoke(ctx)

    mock_prepare.assert_not_awaited()
    mock_fetch.assert_not_awaited()
    mock_hints.assert_called_once_with(ctx)
