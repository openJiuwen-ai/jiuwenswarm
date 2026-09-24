# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Strict AgentGroup package loading tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jiuwenswarm.agents.swarm.agent_group import (
    load_agent_group_package,
)
from openjiuwen.agent_teams.external import external_cli_agent_spec_from_template


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _minimal_group(tmp_path: Path) -> Path:
    group = tmp_path / "group"
    _write_json(
        group / "manifest.json",
        {
            "name": "group",
            "package_type": "agent_group",
            "agents": ["leader", "member1"],
        },
    )
    for name in ("leader", "member1"):
        _write_json(
            group / "agents" / name / "manifest.json",
            {
                "package_type": "agent_template",
                "name": f"{name} display name",
                "description": f"{name} description",
                "persona": {"dir": "." if name == "leader" else "./persona"},
            },
        )
        persona = group / "agents" / name / "persona" / f"{name}.md"
        persona.parent.mkdir(parents=True, exist_ok=True)
        persona.write_text(f"# {name}\n", encoding="utf-8")
    (group / "agents" / "leader" / "AGENT.md").write_text(
        "# Leader rules\n",
        encoding="utf-8",
    )
    return group


def test_load_agent_group_uses_directory_as_id_and_name_as_display_name(
    tmp_path: Path,
) -> None:
    group = _minimal_group(tmp_path)

    templates = load_agent_group_package(group)

    assert templates["leader"].agent_card.id == "leader"
    assert templates["leader"].agent_card.name == "leader display name"
    assert templates["member1"].agent_card.id == "member1"
    assert templates["member1"].agent_card.name == "member1 display name"


def test_load_agent_group_rejects_member_agent_md(tmp_path: Path) -> None:
    group = _minimal_group(tmp_path)
    (group / "agents" / "member1" / "AGENT.md").write_text(
        "# unexpected\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must not contain AGENT.md"):
        load_agent_group_package(group)


def test_load_agent_group_discovers_unlisted_skills(tmp_path: Path) -> None:
    group = _minimal_group(tmp_path)
    skill_dir = group / "skills" / "discovered_skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "# Discovered skill\n",
        encoding="utf-8",
    )

    templates = load_agent_group_package(group)

    for template in templates.values():
        assert [Path(skill.dir).name for skill in template.skills] == [
            "discovered_skill"
        ]


def test_load_agent_group_merges_declared_and_discovered_skills(
    tmp_path: Path,
) -> None:
    group = _minimal_group(tmp_path)
    manifest_path = group / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["skills"] = ["declared_skill"]
    _write_json(manifest_path, manifest)

    for name in ("declared_skill", "another_skill"):
        skill_dir = group / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")

    templates = load_agent_group_package(group)

    for template in templates.values():
        assert [Path(skill.dir).name for skill in template.skills] == [
            "declared_skill",
            "another_skill",
        ]


@pytest.mark.parametrize(
    ("provider_name", "cli_agent"),
    [("codex", "codex"), ("claudecode", "claude")],
)
def test_load_external_runtime_reuses_cli_config_and_manifest_skill_paths(
    tmp_path: Path,
    provider_name: str,
    cli_agent: str,
) -> None:
    group = _minimal_group(tmp_path)
    shared = group / "skills" / "shared"
    shared.mkdir(parents=True)
    (shared / "SKILL.md").write_text("# shared\n", encoding="utf-8")
    manifest_path = group / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["skills"] = [{"dir": "./skills/shared", "mode": "all"}]
    _write_json(manifest_path, manifest)

    member_skill = group / "agents" / "member1" / "skills" / "review"
    member_skill.mkdir(parents=True)
    (member_skill / "SKILL.md").write_text("# review\n", encoding="utf-8")
    member_manifest_path = group / "agents" / "member1" / "manifest.json"
    member_manifest = json.loads(member_manifest_path.read_text(encoding="utf-8"))
    member_manifest["skills"] = [{"dir": "./skills/review", "mode": "all"}]
    member_manifest["runtime"] = {
        "provider_name": provider_name,
        "provider_version": "0.1.0",
        "config": {"skill_conflict": "replace"},
    }
    _write_json(member_manifest_path, member_manifest)

    templates = load_agent_group_package(group)
    template = templates["member1"]
    assert template.runtime is not None
    assert template.runtime.provider_name == provider_name
    config = external_cli_agent_spec_from_template(template)
    assert config is not None
    assert config.cli_agent == cli_agent
    assert config.skill_conflict == "replace"
    assert [Path(skill["dir"]).name for skill in config.skills] == ["review", "shared"]


def test_load_external_runtime_rejects_leader(tmp_path: Path) -> None:
    group = _minimal_group(tmp_path)
    leader_path = group / "agents" / "leader" / "manifest.json"
    leader = json.loads(leader_path.read_text(encoding="utf-8"))
    leader["runtime"] = {
        "provider_name": "codex",
        "provider_version": "0.1.0",
        "config": {},
    }
    _write_json(leader_path, leader)

    with pytest.raises(ValueError, match="leader does not support"):
        load_agent_group_package(group)


@pytest.mark.parametrize(
    "agents",
    [
        ["member1"],
        ["leader", "leader"],
        ["leader", "../member1"],
    ],
)
def test_load_agent_group_rejects_invalid_roster(
    tmp_path: Path,
    agents: list[str],
) -> None:
    group = _minimal_group(tmp_path)
    manifest = json.loads((group / "manifest.json").read_text(encoding="utf-8"))
    manifest["agents"] = agents
    _write_json(group / "manifest.json", manifest)

    with pytest.raises(ValueError):
        load_agent_group_package(group)
