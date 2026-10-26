# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Named-member assembly through real workspace skill discovery and invocation."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from openjiuwen.agent_teams.schema.blueprint import LeaderSpec, TeamAgentSpec
from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec
from openjiuwen.agent_teams.schema.team import TeamMemberSpec, TeamRole
from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
from openjiuwen.harness.factory import resolve_deep_agent_parts
from openjiuwen.harness.rails import SkillUseRail
from openjiuwen.harness.tools.skills.skill_tool import SkillTool
from openjiuwen.harness.workspace.workspace import Workspace

from jiuwenswarm.agents.swarm import enrich_team_spec_for_swarm, registry
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.agents.swarm.providers import skills as skills_provider


def _enriched_spec(mode, configured_skills, config):
    spec = TeamAgentSpec(
        team_name="skills_team",
        leader=LeaderSpec(member_name="team_leader"),
        agents={"leader": DeepAgentSpec(), "analyst": DeepAgentSpec(skills=configured_skills)},
        predefined_members=[TeamMemberSpec(
            member_name="analyst", display_name="Analyst", role_type=TeamRole.TEAMMATE,
        )],
    )
    enrich_team_spec_for_swarm(
        spec, session_id="skills-session", mode=mode, channel_id="web", config_base=config,
    )
    return spec.agents["analyst"]


@pytest.mark.parametrize("mode", ["team", "code.team", "team.plan"])
@pytest.mark.parametrize("configured_skills,named_config,expected", [
    ([" selected "], ["named-default"], ["selected"]),
    ([], ["named-default"], []),
    (None, ["named-default"], ["named-default"]),
    (None, None, ["role-default"]),
])
def test_named_member_skill_precedence(mode, configured_skills, named_config, expected):
    config = {"agents": {"teammate": {"skills": ["role-default"]}}}
    if named_config is not None:
        config["agents"]["analyst"] = {"skills": named_config}
    member = _enriched_spec(mode, configured_skills, config)
    toolkits = [rail for rail in member.rails if rail.type == registry.MEMBER_SKILL_TOOLKIT]
    assert len(toolkits) == 1
    assert toolkits[0].params["skills"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["team", "code.team", "team.plan"])
async def test_named_member_configured_skill_can_be_loaded(mode, tmp_path, monkeypatch):
    global_root = tmp_path / "global-skills"
    skill_root = global_root / "selected"
    skill_root.mkdir(parents=True)
    skill_content = "---\nname: selected\ndescription: Selected skill\n---\nMember instructions."
    (skill_root / "SKILL.md").write_text(skill_content, encoding="utf-8")
    member_root = tmp_path / "analyst"
    workspace = Workspace(root_path=str(member_root))
    member = _enriched_spec(mode, ["selected"], {})
    toolkit = next(rail for rail in member.rails if rail.type == registry.MEMBER_SKILL_TOOLKIT)
    monkeypatch.setattr(skills_provider, "get_agent_workspace_dir", lambda: tmp_path / "shared")
    monkeypatch.setattr(skills_provider, "SkillManager", lambda **kwargs: SimpleNamespace())
    built = toolkit.build(language="cn", context=SwarmBuildContext(
        workspace=workspace, global_skills_dir=str(global_root), session_id="skills-session",
    ))
    assert built is not None
    assert (member_root / "skills" / "selected").resolve() == skill_root.resolve()

    # Use the locked core's actual discovery path; no model/network call occurs.
    model = Model(
        model_client_config=ModelClientConfig(
            client_provider="OpenAI", api_key="test", api_base="http://localhost:1",
        ),
        model_config=ModelRequestConfig(model="test"),
    )
    parts = resolve_deep_agent_parts(
        model=model, workspace=workspace, skills=member.skills,
        enable_skill_discovery=True, enable_sys_operation=False, agent_ras=False,
        enable_security_rail=False, enable_llm_retry_rail=False, enable_read_image_multimodal=False,
    )
    rail = next(rail for rail in parts.rails if isinstance(rail, SkillUseRail))
    await rail.reload_skills()
    assert [skill.name for skill in rail.skills_meta] == ["selected"]

    async def read_file(path):
        return SimpleNamespace(code=0, data=SimpleNamespace(content=Path(path).read_text(encoding="utf-8")))

    tool = SkillTool(
        operation=SimpleNamespace(fs=lambda: SimpleNamespace(read_file=read_file)),
        get_skills=lambda **kwargs: rail.skills_meta,
    )
    result = await tool.invoke({"skill_name": "selected"})
    assert result.success is True, result.error
    assert result.data["skill_content"] == skill_content
