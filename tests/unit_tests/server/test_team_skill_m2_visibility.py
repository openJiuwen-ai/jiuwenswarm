# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""M2 T-J-08: regular Skill + Swarm Skill both visible on TeamSkillUseRail (路1)."""
from __future__ import annotations

from pathlib import Path

import pytest

from openjiuwen.agent_teams.paths import configure_global_skills_dir
from openjiuwen.agent_teams.rails.team_skill_use_rail import create_team_skill_use_rail


def _write_skill(skills_root: Path, *, folder: str, kind: str | None) -> None:
    package = skills_root / folder
    package.mkdir(parents=True, exist_ok=True)
    kind_line = f"kind: {kind}\n" if kind else ""
    (package / "SKILL.md").write_text(
        f"---\nname: {folder}\n{kind_line}description: fixture\n---\n\n# {folder}\n",
        encoding="utf-8",
    )


async def _visible_names(rail) -> set[str]:
    await rail._prepare_skills()
    return {skill.name for skill in rail.skills}


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["leader", "teammate"])
async def test_tj08_open_library_lists_regular_and_swarm_skills(
    tmp_path: Path, role: str
) -> None:
    """Empty bootstrap_allow → ordinary + swarm-skill packages both visible."""
    skills = tmp_path / "skills"
    member_ws = tmp_path / "members" / role
    team_ws = tmp_path / "team"
    member_ws.mkdir(parents=True)
    team_ws.mkdir(parents=True)
    _write_skill(skills, folder="plain-note-taker", kind=None)
    _write_skill(skills, folder="swarm-equity-research", kind="swarm-skill")
    configure_global_skills_dir(skills)

    rail = create_team_skill_use_rail(
        member_name=role,
        team_name="dyn_tj08",
        member_visibility_path=member_ws / "skills-visibility.json",
        team_visibility_path=team_ws / "skills-visibility.json",
        skills_dir=[str(skills)],
        bootstrap_allow=[],
        skill_mode="all",
        include_tools=False,
    )
    listed = await _visible_names(rail)
    assert "plain-note-taker" in listed
    assert "swarm-equity-research" in listed


@pytest.mark.asyncio
async def test_tj08_nonempty_bootstrap_allow_would_narrow(tmp_path: Path) -> None:
    """Contrast: seeding allow=[one] shrinks the view — why dynamic open forces []."""
    skills = tmp_path / "skills"
    member_ws = tmp_path / "members" / "teammate"
    team_ws = tmp_path / "team"
    member_ws.mkdir(parents=True)
    team_ws.mkdir(parents=True)
    _write_skill(skills, folder="plain-note-taker", kind=None)
    _write_skill(skills, folder="swarm-equity-research", kind="swarm-skill")
    configure_global_skills_dir(skills)

    rail = create_team_skill_use_rail(
        member_name="teammate",
        team_name="dyn_tj08_narrow",
        member_visibility_path=member_ws / "skills-visibility.json",
        team_visibility_path=team_ws / "skills-visibility.json",
        skills_dir=[str(skills)],
        bootstrap_allow=["plain-note-taker"],
        skill_mode="all",
        include_tools=False,
    )
    listed = await _visible_names(rail)
    assert "plain-note-taker" in listed
    assert "swarm-equity-research" not in listed
