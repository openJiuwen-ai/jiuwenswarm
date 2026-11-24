# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from types import SimpleNamespace

import pytest

from openjiuwen.agent_teams.rails.team_skill_use_rail import TeamSkillUseRail
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.prompts import SystemPromptBuilder
from openjiuwen.harness.rails.skills.skill_use_rail import SkillUseRail

from jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail import RuntimePromptRail
from jiuwenswarm.agents.harness.flash.prompt.offline import assemble


@pytest.mark.parametrize("language", ["cn", "en"])
@pytest.mark.parametrize("team", [False, True])
@pytest.mark.asyncio
async def test_runtime_skill_roots_follow_live_loader(tmp_path, language, team):
    workspace = tmp_path / "agent-workspace"
    residual = workspace / "skills" / "ppt-master"
    residual.mkdir(parents=True)
    (residual / "SKILL.md").write_text("old skill", encoding="utf-8")
    official = tmp_path / "official-skills"
    installed = tmp_path / "installed-skills"
    official.mkdir()
    installed.mkdir()
    roots = [str(official), str(installed)]
    skill_rail = (
        TeamSkillUseRail(skills_dir=roots, visibility_provider=lambda: (set(), set()))
        if team else SkillUseRail(skills_dir=roots)
    )
    agent = DeepAgent(AgentCard(id="runtime-skill-roots", name="test"))
    agent.system_prompt_builder = SystemPromptBuilder(language=language)
    agent.add_rail(skill_rail)
    runtime = RuntimePromptRail(language=language, channel="officeclaw")
    runtime.set_runtime_paths(workspace_dir=str(workspace))
    runtime.init(agent)
    ctx = AgentCallbackContext(agent=SimpleNamespace(), inputs=None, session=None)

    await runtime.before_model_call(ctx)
    prompt = agent.system_prompt_builder.build()
    heading = "当前技能发现根目录（按配置顺序）" if language == "cn" else "Current skill-discovery roots (in configured order)"
    assert heading in prompt
    discovery = prompt.split(heading, 1)[1].split("\n\n", 1)[0]
    assert discovery.index(str(official)) < discovery.index(str(installed))
    assert str(workspace / "skills") not in discovery

    replacement_root = tmp_path / "replacement-skills"
    replacement_root.mkdir()
    replacement = SkillUseRail(skills_dir=str(replacement_root))
    agent.remove_pending_rail(skill_rail)
    agent.add_rail(replacement)
    await runtime.before_model_call(ctx)
    prompt = agent.system_prompt_builder.build()
    assert str(replacement_root) in prompt
    assert str(official) not in prompt
    assert str(installed) not in prompt

    agent.remove_pending_rail(replacement)
    await runtime.before_model_call(ctx)
    prompt = agent.system_prompt_builder.build()
    assert str(replacement_root) not in prompt
    assert str(residual.parent) in prompt
    assert ("未提供可用目录信息" if language == "cn" else "No loading-directory information provided") in prompt


@pytest.mark.asyncio
async def test_runtime_skill_roots_are_agent_scoped_and_keep_workspace_fallback(tmp_path):
    agents = []
    for name in ("first", "second"):
        workspace = tmp_path / name
        roots = workspace / "skills"
        roots.mkdir(parents=True)
        agent = DeepAgent(AgentCard(id=name, name=name))
        agent.system_prompt_builder = SystemPromptBuilder(language="cn")
        agent.add_rail(SkillUseRail(skills_dir=str(roots)))
        runtime = RuntimePromptRail(language="cn", channel="officeclaw")
        runtime.set_runtime_paths(workspace_dir=str(workspace))
        runtime.init(agent)
        agents.append((agent, runtime, roots))

    for agent, runtime, roots in agents:
        await runtime.before_model_call(AgentCallbackContext(agent=None, inputs=None, session=None))
        prompt = agent.system_prompt_builder.build()
        discovery = prompt.split("当前技能发现根目录（按配置顺序）：", 1)[1].split("\n\n", 1)[0]
        assert str(roots) in discovery
        other_root = next(other for _, _, other in agents if other != roots)
        assert str(other_root) not in prompt


@pytest.mark.parametrize("language", ["cn", "en"])
def test_tool_dedup_scope_is_current_user_request(language):
    selection = next(section for section in assemble(language) if section.name == "tool_selection")
    text = selection.render(language)
    if language == "cn":
        assert "工具调用去重仅限本次用户请求内" in text
        assert "即使工具和参数与历史请求相同" in text
        assert "没有状态变化" in text
    else:
        assert "Deduplicate tool calls only within the current user request" in text
        assert "identical tools and arguments from earlier requests" in text
        assert "no state change" in text
