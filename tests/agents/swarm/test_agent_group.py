# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Strict AgentGroup package loading tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jiuwenswarm.agents.swarm.agent_group import (
    load_agent_group_package,
    load_agent_group_package_bundle,
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


def test_load_agent_group_bundle_reuses_manifest_and_templates(tmp_path: Path) -> None:
    group = _minimal_group(tmp_path)
    manifest_path = group / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "instruction": "  analyze carefully  ",
            "capabilities": ["analysis", "risk", "analysis", ""],
        }
    )
    _write_json(manifest_path, manifest)

    package = load_agent_group_package_bundle(group)

    assert package.package_dir == group.resolve()
    assert package.manifest["name"] == "group"
    assert set(package.templates) == {"leader", "member1"}
    assert package.instruction == "analyze carefully"
    assert package.capabilities == ("analysis", "risk")


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


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")


def _update_group(group: Path, **fields) -> None:
    path = group / "manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    _write_json(path, {**payload, **fields})


def _update_member(group: Path, name: str = "member1", **fields) -> None:
    path = group / "agents" / name / "manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    _write_json(path, {**payload, **fields})


def _skill(group: Path, name: str = "review") -> Path:
    path = group / "skills" / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text("# Review\n", encoding="utf-8")
    return path


def test_agents_root_cannot_resolve_outside_package(tmp_path: Path, monkeypatch) -> None:
    from jiuwenswarm.agents.swarm import agent_group

    group = _minimal_group(tmp_path)
    outside = tmp_path / "external-agents"
    (group / "agents").rename(outside)
    _symlink(group / "agents", outside)
    loaded = []
    monkeypatch.setattr(agent_group, "load_agent_template_package", loaded.append)

    with pytest.raises(ValueError, match="agents directory escapes"):
        load_agent_group_package(group)
    assert loaded == []


def test_agents_root_alias_inside_package_is_allowed(tmp_path: Path) -> None:
    group = _minimal_group(tmp_path)
    target = group / "templates"
    (group / "agents").rename(target)
    _symlink(group / "agents", target)

    templates = load_agent_group_package(group)

    assert list(templates) == ["leader", "member1"]
    assert templates["member1"].agent_card.id == "member1"


def test_member_directory_cannot_escape_agents_root(tmp_path: Path) -> None:
    group = _minimal_group(tmp_path)
    target = tmp_path / "external-member"
    (group / "agents" / "member1").rename(target)
    _symlink(group / "agents" / "member1", target)

    with pytest.raises(ValueError, match="agent directory.*escapes"):
        load_agent_group_package(group)


@pytest.mark.parametrize("relative", ["manifest.json", "agents/leader/manifest.json", "agents/leader/AGENT.md"])
def test_package_files_cannot_be_symlinks_to_external_files(tmp_path: Path, relative: str) -> None:
    group = _minimal_group(tmp_path)
    source = group / relative
    external = tmp_path / "outside-file"
    source.rename(external)
    _symlink(source, external)

    with pytest.raises(ValueError, match="escapes"):
        load_agent_group_package(group)


def test_persona_directory_cannot_escape_member(tmp_path: Path) -> None:
    group = _minimal_group(tmp_path)
    _update_member(group, persona={"dir": "../leader"})

    with pytest.raises(ValueError, match="persona.dir escapes"):
        load_agent_group_package(group)


def test_persona_markdown_cannot_point_outside_member(tmp_path: Path) -> None:
    group = _minimal_group(tmp_path)
    external = tmp_path / "external.md"
    external.write_text("# Not part of this member\n", encoding="utf-8")
    _symlink(group / "agents" / "member1" / "persona" / "linked.md", external)

    with pytest.raises(ValueError, match="persona Markdown escapes"):
        load_agent_group_package(group)


@pytest.mark.parametrize("value", ["", "   ", "C:persona", "C:/persona", "\\\\host\\share", "\\persona", "bad\x00path"])
def test_persona_paths_reject_invalid_and_foreign_absolute_paths(tmp_path: Path, value: str) -> None:
    group = _minimal_group(tmp_path)
    _update_member(group, persona={"dir": value})

    with pytest.raises(ValueError, match="persona.dir must be package-relative"):
        load_agent_group_package(group)


@pytest.mark.parametrize("relative", ["agents", "agents/member1", "agents/member1/persona"])
def test_missing_package_directories_are_reported_as_validation_errors(tmp_path: Path, relative: str) -> None:
    group = _minimal_group(tmp_path)
    path = group / relative
    path.rename(tmp_path / "removed")

    with pytest.raises(ValueError, match="missing or invalid"):
        load_agent_group_package(group)


@pytest.mark.parametrize("relative", ["agents", "agents/member1", "agents/member1/persona"])
def test_files_in_directory_slots_are_rejected(tmp_path: Path, relative: str) -> None:
    group = _minimal_group(tmp_path)
    path = group / relative
    path.rename(tmp_path / "removed")
    path.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError):
        load_agent_group_package(group)


@pytest.mark.parametrize("relative", ["manifest.json", "agents/leader/manifest.json", "agents/leader/AGENT.md"])
def test_directories_in_file_slots_are_rejected(tmp_path: Path, relative: str) -> None:
    group = _minimal_group(tmp_path)
    path = group / relative
    path.unlink()
    path.mkdir()

    with pytest.raises(ValueError, match="must be a file"):
        load_agent_group_package(group)


@pytest.mark.parametrize("relative", ["manifest.json", "agents", "agents/member1/persona"])
def test_symlink_loops_become_validation_errors(tmp_path: Path, relative: str) -> None:
    group = _minimal_group(tmp_path)
    path = group / relative
    path.rename(tmp_path / "removed")
    _symlink(path, path)

    with pytest.raises(ValueError, match="missing or invalid"):
        load_agent_group_package(group)


def test_package_root_symlink_loop_becomes_validation_error(tmp_path: Path) -> None:
    path = tmp_path / "loop"
    _symlink(path, path)

    with pytest.raises(ValueError, match="package directory not found"):
        load_agent_group_package(path)


@pytest.mark.parametrize("name", ["C:member", "D:", "bad\x00name", "bad\nname", "bad\tname"])
def test_roster_rejects_drive_relative_and_control_character_names(tmp_path: Path, name: str) -> None:
    group = _minimal_group(tmp_path)
    _update_group(group, agents=["leader", name])

    with pytest.raises(ValueError, match="invalid agent name"):
        load_agent_group_package(group)


def test_unicode_member_names_keep_their_identity(tmp_path: Path) -> None:
    group = _minimal_group(tmp_path)
    (group / "agents" / "member1").rename(group / "agents" / "评审专家")
    _update_group(group, agents=["leader", "评审专家"])

    templates = load_agent_group_package(group)

    assert list(templates) == ["leader", "评审专家"]
    assert templates["评审专家"].agent_card.id == "评审专家"


@pytest.mark.parametrize("declared", [True, False])
def test_shared_skill_directory_cannot_escape_package(tmp_path: Path, declared: bool) -> None:
    group = _minimal_group(tmp_path)
    skill = _skill(group)
    target = tmp_path / "external-skill"
    skill.rename(target)
    _symlink(skill, target)
    if declared:
        _update_group(group, skills=["review"])

    with pytest.raises(ValueError, match="shared skill.*escapes"):
        load_agent_group_package(group)


def test_shared_skills_root_cannot_escape_package(tmp_path: Path) -> None:
    group = _minimal_group(tmp_path)
    _skill(group)
    target = tmp_path / "external-skills"
    (group / "skills").rename(target)
    _symlink(group / "skills", target)

    with pytest.raises(ValueError, match="skills directory escapes"):
        load_agent_group_package(group)


def test_shared_skill_markdown_cannot_escape_skill(tmp_path: Path) -> None:
    group = _minimal_group(tmp_path)
    skill = _skill(group)
    external = tmp_path / "external.md"
    (skill / "SKILL.md").rename(external)
    _symlink(skill / "SKILL.md", external)

    with pytest.raises(ValueError, match="SKILL.md escapes"):
        load_agent_group_package(group)


@pytest.mark.parametrize("value", ["", "  ", "C:skills", "C:/skills", "\\\\host\\share", "bad\x00path", "../outside"])
def test_shared_skill_mapping_rejects_invalid_paths(tmp_path: Path, value: str) -> None:
    group = _minimal_group(tmp_path)
    (tmp_path / "outside").mkdir()
    _update_group(group, skills=[{"dir": value}])

    with pytest.raises(ValueError, match="skill"):
        load_agent_group_package(group)


def test_shared_skill_aliases_are_deduplicated_during_discovery(tmp_path: Path) -> None:
    group = _minimal_group(tmp_path)
    skill = _skill(group)
    _symlink(group / "skills" / "alias", skill)
    _update_group(group, skills=["review"])

    templates = load_agent_group_package(group)

    for template in templates.values():
        assert [Path(item.dir) for item in template.skills] == [skill.resolve()]


def test_explicit_duplicate_skill_aliases_are_rejected(tmp_path: Path) -> None:
    group = _minimal_group(tmp_path)
    skill = _skill(group)
    _symlink(group / "skills" / "alias", skill)
    _update_group(group, skills=["review", {"dir": "skills/alias"}])

    with pytest.raises(ValueError, match="duplicate shared skill"):
        load_agent_group_package(group)


@pytest.mark.parametrize("relative", ["manifest.json", "agents/member1/manifest.json"])
@pytest.mark.parametrize("raw", ['{"name":"first","name":"second"}', '{"persona":{"dir":".","dir":"../outside"}}'])
def test_duplicate_json_keys_are_rejected_in_all_manifests(tmp_path: Path, relative: str, raw: str) -> None:
    group = _minimal_group(tmp_path)
    (group / relative).write_text(raw, encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON key"):
        load_agent_group_package(group)


@pytest.mark.parametrize("raw", [b"\xff", b"{", b"[]", b"null", b'"text"'])
def test_malformed_group_manifests_produce_validation_errors(tmp_path: Path, raw: bytes) -> None:
    group = _minimal_group(tmp_path)
    (group / "manifest.json").write_bytes(raw)

    with pytest.raises(ValueError, match="manifest"):
        load_agent_group_package(group)


def test_missing_shared_skill_produces_validation_error(tmp_path: Path) -> None:
    group = _minimal_group(tmp_path)
    _update_group(group, skills=["missing"])

    with pytest.raises(ValueError, match="shared skill.*missing or invalid"):
        load_agent_group_package(group)


def test_leader_rules_outside_persona_are_mounted_once(tmp_path: Path) -> None:
    group = _minimal_group(tmp_path)
    _update_member(group, "leader", persona={"dir": "persona"})

    leader = load_agent_group_package(group)["leader"]

    rules = [section for section in leader.prompt_sections if section.name == "agent_group_leader_rules"]
    assert len(rules) == 1
    assert rules[0].content["en"] == "# Leader rules\n"


def test_group_instruction_and_shared_skills_survive_validated_loading(tmp_path: Path) -> None:
    group = _minimal_group(tmp_path)
    skill = _skill(group)
    _update_group(group, instruction="  Review together  ", skills=[{"dir": "./skills/review", "mode": "all"}])

    loaded = load_agent_group_package_bundle(group)

    assert loaded.instruction == "Review together"
    for template in loaded.templates.values():
        instruction = [section for section in template.prompt_sections if section.name == "agent_group_instruction"]
        assert len(instruction) == 1
        assert instruction[0].content == {"cn": "Review together", "en": "Review together"}
        assert [Path(item.dir) for item in template.skills] == [skill.resolve()]
