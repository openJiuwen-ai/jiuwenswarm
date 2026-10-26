# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the swarm member-skill provider.

Exercises the member-configured skill linking that the
``swarm.member_skill_toolkit`` provider performs, called directly on the provider
helpers (no customizer, no live ``DeepAgent``). These cover the behaviour the
legacy ``TeamManager.build_agent_customizer`` used to own before the provider
unification, now link-based to match the shared-skill-link runtime model.
"""

from __future__ import annotations

from pathlib import Path

from jiuwenswarm.agents.swarm.providers import skills


def _make_skill(parent: Path, name: str) -> None:
    """Create a minimal skill directory (with a ``SKILL.md``) under *parent*."""
    skill_dir = parent / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")


def test_link_member_configured_skills_links_only_selected(tmp_path):
    """Only the member's selected skills are linked into its skills directory."""
    global_skills = tmp_path / "global"
    for name in ("skill-a", "skill-b", "skill-c"):
        _make_skill(global_skills, name)

    member_skills = tmp_path / "member" / "skills"
    member_skills.mkdir(parents=True)

    skills._link_member_configured_skills(
        member_skills,
        ["skill-a", "skill-c"],
        global_skills,
    )

    # Selected skills become links resolving back to the global store (no copies).
    assert (member_skills / "skill-a").resolve() == (global_skills / "skill-a").resolve()
    assert (member_skills / "skill-c").resolve() == (global_skills / "skill-c").resolve()
    assert not (member_skills / "skill-b").exists()


def test_link_member_configured_skills_prunes_unselected_link(tmp_path):
    """Re-linking with a narrower selection prunes the dropped skill link."""
    global_skills = tmp_path / "global"
    for name in ("skill-a", "skill-b"):
        _make_skill(global_skills, name)

    member_skills = tmp_path / "member" / "skills"
    member_skills.mkdir(parents=True)

    skills._link_member_configured_skills(member_skills, ["skill-a", "skill-b"], global_skills)
    assert (member_skills / "skill-a").exists()
    assert (member_skills / "skill-b").exists()

    skills._link_member_configured_skills(member_skills, ["skill-a"], global_skills)
    assert (member_skills / "skill-a").exists()
    assert not (member_skills / "skill-b").exists()


def test_link_member_configured_skills_skips_missing_global_dir(tmp_path):
    """A missing global skills directory is a no-op rather than an error."""
    member_skills = tmp_path / "member" / "skills"
    member_skills.mkdir(parents=True)

    skills._link_member_configured_skills(
        member_skills,
        ["skill-a"],
        tmp_path / "does-not-exist",
    )

    assert list(member_skills.iterdir()) == []


def test_extract_skill_name_from_tool_result_prefers_nested_skill():
    """The skill name resolver reads nested ``skill`` then flat fallbacks."""
    assert skills._extract_skill_name_from_tool_result({"skill": {"name": "alpha"}}) == "alpha"
    assert skills._extract_skill_name_from_tool_result({"skill_name": "beta"}) == "beta"
    assert skills._extract_skill_name_from_tool_result({"name": "gamma"}) == "gamma"
    assert skills._extract_skill_name_from_tool_result({}) == ""
