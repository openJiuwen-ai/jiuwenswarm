# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentGroup assembly through openjiuwen's single template → member implementation.

The expected texts were recorded from the swarm-private assembly that the
openjiuwen functions replaced (SDD-0032 §7 #11); they must not drift.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jiuwenswarm.agents.swarm import assembly
from openjiuwen.agent_teams.experts import load_agent_group_package_bundle
from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec
from openjiuwen.harness.schema.deep_agent_spec import DeepAgentSpec, WorkspaceSpec


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _multilingual_group(tmp_path: Path) -> Path:
    root = tmp_path / "mlgroup"
    _write(
        root / "manifest.json",
        json.dumps(
            {
                "package_type": "agent_group",
                "name": "mlgroup",
                "instruction": "  Team rule for {{language}}  ",
                "agents": ["leader", "writer", "reviewer"],
                "skills": ["shared"],
            }
        ),
    )
    _write(root / "skills" / "shared" / "SKILL.md", "---\nname: shared\n---\n")
    for name, persona in (("leader", "."), ("writer", "persona"), ("reviewer", "persona")):
        manifest = {
            "package_type": "agent_template",
            "name": f"{name}-tpl",
            "description": f"{name} desc",
            "persona": {"dir": persona},
            "display_name": {"zh": f"{name}中文", "en": f"{name} EN"},
        }
        if name == "writer":
            manifest["skills"] = [{"dir": "skills/write"}]
            _write(root / "agents" / name / "skills" / "write" / "SKILL.md", "---\nname: write\n---\n")
        _write(root / "agents" / name / "manifest.json", json.dumps(manifest))
        _write(root / "agents" / name / "persona" / "a.md", f"\n  {name} works in {{{{ workspace }}}} ({{{{language}}}})  \n\n")
        _write(root / "agents" / name / "persona" / "b.md", "   \n")
    _write(root / "agents" / "leader" / "AGENT.md", "# Leader rules {{ unknown }}\n")
    return root


def _base(language: str | None) -> DeepAgentSpec:
    return DeepAgentSpec(language=language, skills=["base-a", "write"], workspace=WorkspaceSpec(root_path=f"/ws/{language}"))


@pytest.mark.parametrize("language", ["cn", "en", None])
def test_agent_group_assembly_output_is_unchanged(tmp_path: Path, language: str | None) -> None:
    group = _multilingual_group(tmp_path)
    spec = TeamAgentSpec(agents={"leader": _base(language), "teammate": _base(language)}, team_name="t")
    assembly._apply_agent_group(spec, "mlgroup", load_agent_group_package_bundle(group))

    rendered = language or "cn"
    assert spec.leader.prompt == (
        f"# Leader rules {{{{ unknown }}}}\n\n\n\n  leader works in /ws/{language} ({rendered})\n\nTeam rule for {rendered}"
    )
    members = {member.member_name: member for member in spec.predefined_members}
    assert list(members) == ["writer", "reviewer"]
    assert members["writer"].prompt == f"writer works in /ws/{language} ({rendered})\n\nTeam rule for {rendered}"
    assert (members["writer"].display_name, members["writer"].desc) == ("writer中文", "writer desc")
    for name in ("leader", "writer", "reviewer"):
        assert spec.agents[name].skills == ["base-a", "write", "shared"]
        assert spec.agents[name].agent_template_spec["prompt_sections"] == []
    assert spec.agents["teammate"].skills == ["base-a", "write"]
    assert spec.agent_group_name == "mlgroup"
    assert spec.metadata["agent_group_name"] == "mlgroup", "the legacy metadata key stays for existing readers"
