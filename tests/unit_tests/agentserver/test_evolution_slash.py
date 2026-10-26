# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for rail-independent evolution slash handling."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server.runtime.agent_adapter.evolution_slash import (
    EvolutionSlashContext,
    handle_evolution_slash_command,
)


def _write_skill(tmp_path, name: str, *, kind: str | None = None) -> str:
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True)
    if kind is None:
        content = f"# {name}\n"
    else:
        content = f"---\nname: {name}\nkind: {kind}\n---\n# {name}\n"
    skill_dir.joinpath("SKILL.md").write_text(content, encoding="utf-8")
    return str(skills_dir)


def _evolution_log_json(skill_name: str, marker: str, *, version: str = "v1.0.0") -> str:
    return json.dumps(
        {
            "skill_id": skill_name,
            "version": version,
            "updated_at": marker,
            "entries": [],
        },
        ensure_ascii=False,
    )


def _write_rollback_pair(
    tmp_path,
    skill_name: str,
    version: str,
    *,
    skill_content: str,
    log_marker: str,
) -> None:
    archive = tmp_path / "skills" / skill_name / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    archive.joinpath(f"SKILL.{version}.md").write_text(skill_content, encoding="utf-8")
    archive.joinpath(f"evolutions.{version}.json").write_text(
        _evolution_log_json(skill_name, log_marker),
        encoding="utf-8",
    )


def _skill_archive_version(path) -> str:
    return path.name[len("SKILL."):-len(".md")]


def _evolution_archive_version(path) -> str:
    return path.name[len("evolutions."):-len(".json")]


def _archived_skill_versions_with_content(archive, excluded_name: str, content: str) -> set[str]:
    versions: set[str] = set()
    for path in archive.glob("SKILL.v*.md"):
        if path.name == excluded_name:
            continue
        if path.read_text(encoding="utf-8") != content:
            continue
        versions.add(_skill_archive_version(path))
    return versions


def _archived_log_versions_for_initialized_log(archive, excluded_name: str, skill_id: str) -> set[str]:
    versions: set[str] = set()
    for path in archive.glob("evolutions.v*.json"):
        if path.name == excluded_name:
            continue
        log = json.loads(path.read_text(encoding="utf-8"))
        if log.get("skill_id") != skill_id:
            continue
        if log.get("entries") != []:
            continue
        versions.add(_evolution_archive_version(path))
    return versions


@pytest.mark.anyio
async def test_agent_plan_evolve_uses_actual_swarm_skill_kind(tmp_path):
    skills_dir = _write_skill(tmp_path, "research-team", kind="swarm-skill")

    result = await handle_evolution_slash_command(
        "/evolve research-team improve review flow",
        EvolutionSlashContext(
            mode="agent.plan",
            session_id="sess-agent-plan",
            skills_dir=skills_dir,
            evolution_enabled=True,
        ),
    )

    assert result is not None
    assert result["result_type"] == "followup"
    assert 'subject={"kind": "swarm-skill", "name": "research-team"}' in result["followup_prompt"]


@pytest.mark.anyio
async def test_agent_plan_evolve_defaults_untyped_skill_to_skill_kind(tmp_path):
    skills_dir = _write_skill(tmp_path, "regular-skill")

    result = await handle_evolution_slash_command(
        "/evolve regular-skill improve retry flow",
        EvolutionSlashContext(
            mode="agent.plan",
            session_id="sess-agent-plan",
            skills_dir=skills_dir,
            evolution_enabled=True,
        ),
    )

    assert result is not None
    assert result["result_type"] == "followup"
    assert 'subject={"kind": "skill", "name": "regular-skill"}' in result["followup_prompt"]


@pytest.mark.anyio
async def test_agent_plan_evolve_allows_missing_user_intent(tmp_path):
    skills_dir = _write_skill(tmp_path, "regular-skill")

    result = await handle_evolution_slash_command(
        "/evolve regular-skill",
        EvolutionSlashContext(
            mode="agent.plan",
            session_id="sess-agent-plan",
            skills_dir=skills_dir,
            evolution_enabled=True,
        ),
    )

    assert result is not None
    assert result["result_type"] == "followup"
    assert result["action"] == "run_evolve_followup"
    assert result["skill_name"] == "regular-skill"
    assert 'user_intent=""' in result["followup_prompt"]


@pytest.mark.anyio
async def test_team_evolve_allows_missing_user_intent(tmp_path):
    skills_dir = _write_skill(tmp_path, "regular-skill")

    result = await handle_evolution_slash_command(
        "/evolve regular-skill",
        EvolutionSlashContext(
            mode="team",
            session_id="sess-team",
            skills_dir=skills_dir,
            evolution_enabled=True,
        ),
    )

    assert result is not None
    assert result["result_type"] == "followup"
    assert result["action"] == "run_evolve_followup"
    assert result["skill_name"] == "regular-skill"
    assert 'user_intent=""' in result["followup_prompt"]


@pytest.mark.anyio
async def test_agent_plan_evolve_simplify_no_records_returns_answer(tmp_path, monkeypatch):
    skills_dir = _write_skill(tmp_path, "research-team", kind="swarm-skill")
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.evolution_slash.ExperienceQueryService.list_experiences",
        AsyncMock(return_value={"items": [], "has_more": False}),
    )

    result = await handle_evolution_slash_command(
        "/evolve_simplify research-team",
        EvolutionSlashContext(
            mode="agent.plan",
            session_id="sess-agent-plan",
            skills_dir=skills_dir,
            evolution_enabled=True,
        ),
    )

    assert result is not None
    assert result["result_type"] == "answer"
    assert result["output"] == "Skill 'research-team' 暂无演进经验，无需整理。"


@pytest.mark.anyio
async def test_agent_plan_evolve_rebuild_returns_rebuild_request(tmp_path):
    skills_dir = _write_skill(tmp_path, "research-team", kind="swarm-skill")

    result = await handle_evolution_slash_command(
        "/evolve_rebuild research-team optimize",
        EvolutionSlashContext(
            mode="agent.plan",
            session_id="sess-agent-plan",
            skills_dir=skills_dir,
            evolution_enabled=True,
        ),
    )

    assert result is not None
    assert result["result_type"] == "rebuild_request"
    assert result["action"] == "run_rebuild_inline"
    assert result["skill_name"] == "research-team"
    assert result["user_intent"] == "optimize"


@pytest.mark.anyio
async def test_agent_plan_evolve_rebuild_requires_skill_name(tmp_path):
    skills_dir = _write_skill(tmp_path, "regular-skill")

    result = await handle_evolution_slash_command(
        "/evolve_rebuild",
        EvolutionSlashContext(
            mode="agent.plan",
            session_id="sess-agent-plan",
            skills_dir=skills_dir,
            evolution_enabled=True,
        ),
    )

    assert result is not None
    assert result["result_type"] == "error"
    assert "/evolve_rebuild" in result["output"]


@pytest.mark.anyio
async def test_agent_plan_evolve_rebuild_validate_does_not_archive(tmp_path):
    skills_dir = _write_skill(tmp_path, "regular-skill")
    skill_dir = tmp_path / "skills" / "regular-skill"
    skill_dir.joinpath("evolutions.json").write_text(
        _evolution_log_json("regular-skill", "before-clear"),
        encoding="utf-8",
    )

    context = EvolutionSlashContext(
        mode="agent.plan",
        session_id="sess-agent-plan",
        skills_dir=skills_dir,
        evolution_enabled=True,
    )

    result = await handle_evolution_slash_command(
        "/evolve_rebuild regular-skill",
        context,
    )

    assert result is not None
    assert result["result_type"] == "rebuild_request"
    assert result["action"] == "run_rebuild_inline"
    assert result["skill_name"] == "regular-skill"
    assert result.get("user_intent") is None
    archive = skill_dir / "archive"
    assert not archive.exists() or not list(archive.glob("SKILL.v*.md"))
    current_log = json.loads(skill_dir.joinpath("evolutions.json").read_text(encoding="utf-8"))
    assert current_log["updated_at"] == "before-clear"


@pytest.mark.anyio
async def test_agent_plan_evolve_rollback_lists_paired_short_versions(tmp_path):
    skills_dir = _write_skill(tmp_path, "regular-skill")
    _write_rollback_pair(
        tmp_path,
        "regular-skill",
        "v1.0.1",
        skill_content="# archived\n",
        log_marker="target",
    )
    archive = tmp_path / "skills" / "regular-skill" / "archive"
    # Timestamp-style archives are no longer treated as valid SemVer pairs.
    archive.joinpath("SKILL.v20260622T101500.md").write_text("# unpaired\n", encoding="utf-8")

    result = await handle_evolution_slash_command(
        "/evolve_rollback regular-skill",
        EvolutionSlashContext(
            mode="agent.plan",
            session_id="sess-agent-plan",
            skills_dir=skills_dir,
            evolution_enabled=True,
        ),
    )

    assert result is not None
    assert result["result_type"] == "answer"
    assert "`v1.0.1`" in result["output"]
    assert "v20260622T101500" not in result["output"]
    assert "SKILL.v1.0.1.md" not in result["output"]
    assert "evolutions.v1.0.1.json" not in result["output"]


@pytest.mark.anyio
async def test_agent_plan_evolve_rollback_restores_pair_and_archives_current(tmp_path):
    skills_dir = _write_skill(tmp_path, "regular-skill")
    skill_dir = tmp_path / "skills" / "regular-skill"
    skill_dir.joinpath("SKILL.md").write_text(
        "---\nname: regular-skill\nversion: v1.2.0\n---\n# current\n",
        encoding="utf-8",
    )
    skill_dir.joinpath("evolutions.json").write_text(
        _evolution_log_json("regular-skill", "current", version="v1.2.0"),
        encoding="utf-8",
    )
    _write_rollback_pair(
        tmp_path,
        "regular-skill",
        "v1.0.0",
        skill_content="# archived\n",
        log_marker="target",
    )

    result = await handle_evolution_slash_command(
        "/evolve_rollback regular-skill v1.0.0",
        EvolutionSlashContext(
            mode="agent.plan",
            session_id="sess-agent-plan",
            skills_dir=skills_dir,
            evolution_enabled=True,
        ),
    )

    assert result is not None
    assert result["result_type"] == "answer"
    assert "v1.0.0" in result["output"]
    assert skill_dir.joinpath("SKILL.md").read_text(encoding="utf-8") == "# archived\n"
    restored_log = json.loads(skill_dir.joinpath("evolutions.json").read_text(encoding="utf-8"))
    # Live evolutions are cleared on rollback; archived evo contents are never restored.
    assert restored_log["entries"] == []
    assert restored_log["skill_id"] == "regular-skill"

    archive = skill_dir / "archive"
    assert not archive.joinpath("SKILL.v1.0.0.md").exists()
    assert not archive.joinpath("evolutions.v1.0.0.json").exists()
    current_skill_versions = _archived_skill_versions_with_content(
        archive,
        "SKILL.v1.0.0.md",
        "---\nname: regular-skill\nversion: v1.2.0\n---\n# current\n",
    )
    current_log_versions = _archived_log_versions_for_initialized_log(
        archive,
        "evolutions.v1.0.0.json",
        "regular-skill",
    )
    assert len(current_skill_versions) == 1
    assert current_skill_versions == current_log_versions
    current_version = next(iter(current_skill_versions))
    archived_current = json.loads(
        archive.joinpath(f"evolutions.{current_version}.json").read_text(encoding="utf-8")
    )
    assert archived_current["entries"] == []


@pytest.mark.anyio
async def test_agent_plan_evolve_rollback_initializes_missing_current_evolution_log(tmp_path):
    skills_dir = _write_skill(tmp_path, "regular-skill")
    skill_dir = tmp_path / "skills" / "regular-skill"
    skill_dir.joinpath("SKILL.md").write_text(
        "---\nname: regular-skill\nversion: v1.2.0\n---\n# current\n",
        encoding="utf-8",
    )
    _write_rollback_pair(
        tmp_path,
        "regular-skill",
        "v1.0.0",
        skill_content="# archived\n",
        log_marker="target",
    )

    result = await handle_evolution_slash_command(
        "/evolve_rollback regular-skill v1.0.0",
        EvolutionSlashContext(
            mode="agent.plan",
            session_id="sess-agent-plan",
            skills_dir=skills_dir,
            evolution_enabled=True,
        ),
    )

    assert result is not None
    assert result["result_type"] == "answer"
    assert skill_dir.joinpath("SKILL.md").read_text(encoding="utf-8") == "# archived\n"
    restored_log = json.loads(skill_dir.joinpath("evolutions.json").read_text(encoding="utf-8"))
    assert restored_log["entries"] == []
    assert restored_log["skill_id"] == "regular-skill"

    archive = skill_dir / "archive"
    current_skill_versions = _archived_skill_versions_with_content(
        archive,
        "SKILL.v1.0.0.md",
        "---\nname: regular-skill\nversion: v1.2.0\n---\n# current\n",
    )
    current_log_versions = _archived_log_versions_for_initialized_log(
        archive,
        "evolutions.v1.0.0.json",
        "regular-skill",
    )
    assert len(current_skill_versions) == 1
    assert current_skill_versions == current_log_versions
