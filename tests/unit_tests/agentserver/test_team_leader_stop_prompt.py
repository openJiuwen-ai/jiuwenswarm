# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Prompt-only global pause rules reach the leader, not the teammates."""

from pathlib import Path

import yaml

from jiuwenswarm.agents.harness.team.config_loader import load_team_spec_dict


def _leader_prompt():
    root = Path(__file__).resolve().parents[3]
    config = yaml.safe_load((root / "jiuwenswarm/resources/config.yaml").read_text(encoding="utf-8"))
    return config["agents"]["agent_leader"]["system_prompt"]


def test_existing_loader_preserves_leader_only_prompt():
    from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec

    prompt = _leader_prompt()
    config = {
        "models": {"defaults": [{
            "model_client_config": {
                "api_base": "https://models.example/v1", "api_key": "test-key",
                "model_name": "test-model", "client_provider": "OpenAI",
            },
            "model_config_obj": {},
        }]},
        "agents": {"agent_leader": {"system_prompt": prompt}, "agent_teammate": {}},
        "modes": {"team": {"demo": {
            "team_name": "demo", "agents": {"leader": "$agent_leader", "teammate": "$agent_teammate"},
        }}},
    }
    spec = TeamAgentSpec.model_validate(load_team_spec_dict(config))
    assert spec.agents["leader"].system_prompt == prompt
    assert not spec.agents["teammate"].system_prompt


def test_existing_sdk_rail_includes_pause_rules_in_leader_system_prompt():
    from openjiuwen.agent_teams.rails.team_policy_rail import TeamPolicyRail
    from openjiuwen.agent_teams.schema.team import TeamRole
    from openjiuwen.core.single_agent.prompts.builder import SystemPromptBuilder

    prompt = _leader_prompt()
    rail = TeamPolicyRail(role=TeamRole.LEADER, member_name="team-leader", base_prompt=prompt)
    builder = SystemPromptBuilder(language="cn")
    for section in rail._static_sections:
        builder.add_section(section)
    rendered = builder.build()
    assert prompt in rendered
    assert 'send_message(to="*"' in rendered
