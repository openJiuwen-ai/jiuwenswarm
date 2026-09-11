# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurboPromptRail should not advertise a disabled acceleration tool."""

# pylint: disable=protected-access

from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.skill_turbo.rails.skill_prompt_rail import (
    SkillTurboPromptRail,
    _SECTION_NAME,
)


class _FakeBuilder:
    def __init__(self) -> None:
        self.sections: dict[str, object] = {}

    def add_section(self, section) -> None:
        self.sections[section.name] = section

    def remove_section(self, name: str) -> None:
        self.sections.pop(name, None)

    def get_section(self, name: str):
        return self.sections.get(name)


@pytest.mark.asyncio
async def test_skill_turbo_prompt_skipped_when_acceleration_disabled():
    """Explicit constructor flag must skip guide even on ReAct-like ctx.agent."""
    builder = _FakeBuilder()
    deep = SimpleNamespace(system_prompt_builder=builder)
    # Production BEFORE_MODEL_CALL bridge: ctx.agent is inner ReAct (no rails API).
    react_like = SimpleNamespace(system_prompt_builder=builder)

    rail = SkillTurboPromptRail(acceleration_disabled=True)
    rail.init(deep)

    await rail.before_model_call(SimpleNamespace(agent=react_like))

    assert _SECTION_NAME not in builder.sections
    assert rail._agent is deep


@pytest.mark.asyncio
async def test_skill_turbo_prompt_injected_when_tool_enabled():
    builder = _FakeBuilder()
    deep = SimpleNamespace(system_prompt_builder=builder)
    react_like = SimpleNamespace(system_prompt_builder=builder)

    rail = SkillTurboPromptRail(acceleration_disabled=False)
    rail.init(deep)

    await rail.before_model_call(SimpleNamespace(agent=react_like))

    assert _SECTION_NAME in builder.sections
    content = builder.sections[_SECTION_NAME].content["cn"]
    assert "skill_acceleration_exec" in content
    assert rail._agent is deep


@pytest.mark.asyncio
async def test_skill_turbo_prompt_removes_stale_section_when_disabled():
    builder = _FakeBuilder()
    builder.sections[_SECTION_NAME] = SimpleNamespace(name=_SECTION_NAME)
    deep = SimpleNamespace(system_prompt_builder=builder)
    react_like = SimpleNamespace(system_prompt_builder=builder)

    rail = SkillTurboPromptRail(acceleration_disabled=True)
    rail.init(deep)
    await rail.before_model_call(SimpleNamespace(agent=react_like))

    assert _SECTION_NAME not in builder.sections
