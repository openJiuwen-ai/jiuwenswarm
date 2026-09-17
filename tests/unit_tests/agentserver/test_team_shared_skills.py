# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for team Skill visibility metadata and the library reload rail.

Skills live in exactly one physical library (``get_agent_skills_dir()``); a
team owns no mirrored ``skills/`` directory. What a team or a member may see is
decided by a ``skills-visibility.json`` document at the corresponding workspace
root, so these tests assert on metadata content rather than on symlinks.
"""

# pylint: disable=protected-access

import inspect
import logging

import pytest
from openjiuwen.agent_evolving.checkpointing import EvolutionStore
from openjiuwen.agent_evolving.checkpointing.types import EvolutionPatch, EvolutionRecord, EvolutionTarget
from openjiuwen.agent_teams.paths import SKILL_VISIBILITY_FILENAME
from openjiuwen.core.single_agent.rail.base import ToolCallInputs
from openjiuwen.agent_teams.skill import (
    SCOPE_MEMBER,
    SCOPE_TEAM,
    SkillVisibility,
    bootstrap_skill_visibility,
    compose_skill_visibility,
    read_skill_visibility,
    set_skill_visibility,
)

from jiuwenswarm.agents.harness.team.rails.team_skill_library_reload_rail import (
    TeamSkillLibraryReloadRail,
)
from jiuwenswarm.agents.harness.team import team_manager as team_manager_module
from jiuwenswarm.agents.harness.team.team_manager import TeamManager
from jiuwenswarm.agents.swarm import assembly
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

test_logger = logging.getLogger("tests.team_shared_skills")


def _make_skill(library_dir, name: str) -> None:
    """Create a minimal Skill directory inside the single physical library."""
    skill_dir = library_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_team_evolution_is_visible_and_editable_from_global_skill_manager(tmp_path):
    """A Skill evolved from the team side is the same entity the global manager edits."""
    global_workspace = tmp_path / "agent" / "workspace"
    global_skills_dir = global_workspace / "skills"
    _make_skill(global_skills_dir, "shared-skill")
    (global_skills_dir / "shared-skill" / "SKILL.md").write_text(
        "---\nname: shared-skill\nkind: swarm-skill\n---\n\n# Shared Skill\n",
        encoding="utf-8",
    )

    store = EvolutionStore(str(global_skills_dir))
    record = EvolutionRecord(
        id="ev_shared",
        source="execution_success",
        timestamp="2026-08-04T00:00:00+00:00",
        context="team run",
        change=EvolutionPatch(
            section="Instructions",
            action="append",
            content="original experience",
            target=EvolutionTarget.BODY,
        ),
    )
    await store.append_record("shared-skill", record, subject_kind="swarm-skill")

    manager = SkillManager(workspace_dir=str(global_workspace))
    listed = await manager.handle_skills_list({})
    skill = next(item for item in listed["skills"] if item["name"] == "shared-skill")
    assert skill["has_evolutions"] is True

    evolution = await manager.handle_skills_evolution_get({"name": "shared-skill"})
    assert [entry["id"] for entry in evolution["entries"]] == ["ev_shared"]
    evolution["entries"][0]["change"]["content"] = "edited experience"

    saved = await manager.handle_skills_evolution_save(
        {"name": "shared-skill", "entries": evolution["entries"]}
    )
    assert saved["success"] is True
    reloaded = await manager.handle_skills_evolution_get({"name": "shared-skill"})
    assert reloaded["entries"][0]["change"]["content"] == "edited experience"


def test_platform_declares_no_team_scope_skill_visibility_seeder():
    """The team document has exactly one seeder, and it does not live here.

    ``TeamWorkspaceManager.initialize`` seeds the team-scope
    ``skills-visibility.json``; the platform side only resolves the path and
    hands it to the rails. A second writer would be harmless only while every
    writer agrees on an empty allow-list — the moment one of them seeds a real
    grant, the outcome would depend on call order. This guard fails as soon as
    a seeder is added back to team assembly or to the team lifecycle manager.
    """
    for module in (assembly, team_manager_module):
        source = inspect.getsource(module)
        test_logger.info("checking %s for a team-scope seeder", module.__name__)
        assert "bootstrap_skill_visibility" not in source

    assert not hasattr(TeamManager, "ensure_team_skill_visibility_initialized")
    assert not hasattr(TeamManager, "ensure_team_skill_visibility_ready_for_session")


def test_missing_team_visibility_document_imposes_no_restriction(tmp_path):
    """A team without a seeded document constrains nothing, so no seeding is required."""
    metadata_path = tmp_path / "team_workspace" / SKILL_VISIBILITY_FILENAME

    team = read_skill_visibility(metadata_path, scope=SCOPE_TEAM, entity_id="demo_team")
    test_logger.info("missing team document reads back as: allow=%s deny=%s", team.allow, team.deny)
    assert team.allow == []
    assert team.deny == []

    member = SkillVisibility(scope=SCOPE_MEMBER, id="teammate")
    enabled, disabled = compose_skill_visibility(member, team, [])
    assert enabled == set()
    assert disabled == set()


def test_newly_installed_skill_is_visible_without_touching_metadata(tmp_path):
    """A new library entry needs no metadata edit: an empty allow-list inherits it."""
    team_workspace = tmp_path / "team_workspace"
    bootstrap_skill_visibility(
        team_workspace / SKILL_VISIBILITY_FILENAME,
        scope=SCOPE_TEAM,
        entity_id="demo_team",
        allow=None,
        bootstrapped_from="team_workspace:initialize",
    )

    team = read_skill_visibility(
        team_workspace / SKILL_VISIBILITY_FILENAME,
        scope=SCOPE_TEAM,
        entity_id="demo_team",
    )
    member = SkillVisibility(scope=SCOPE_MEMBER, id="teammate")
    enabled, disabled = compose_skill_visibility(member, team, [])

    # Nothing is enumerated, so nothing can go stale when "skill-new" lands later.
    assert enabled == set()
    assert disabled == set()


@pytest.mark.asyncio
async def test_team_skill_library_reload_rail_reloads_after_global_skill_write(tmp_path, monkeypatch):
    """The after-tool rail reloads Skill views only when writes touch the library."""
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.rails.team_skill_library_reload_rail.get_cwd",
        lambda: str(tmp_path),
    )
    global_skills_dir = tmp_path / "global_skills"
    _make_skill(global_skills_dir, "skill-a")
    skill_md = global_skills_dir / "skill-a" / "SKILL.md"
    reload_calls = []

    rail = TeamSkillLibraryReloadRail(
        global_skills_dir=global_skills_dir,
        reload_skill_views=lambda: reload_calls.append("reload"),
    )
    ctx = type(
        "_Ctx",
        (),
        {
            "inputs": ToolCallInputs(
                tool_name="write_file",
                tool_args={"file_path": str(skill_md.relative_to(tmp_path))},
            ),
            "agent": None,
        },
    )()

    await rail.after_tool_call(ctx)

    assert reload_calls == ["reload"]


@pytest.mark.asyncio
async def test_team_skill_library_reload_rail_ignores_writes_outside_library(tmp_path, monkeypatch):
    """Writes outside the single library must not trigger a Skill view reload."""
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.rails.team_skill_library_reload_rail.get_cwd",
        lambda: str(tmp_path),
    )
    global_skills_dir = tmp_path / "global_skills"
    _make_skill(global_skills_dir, "skill-a")
    outside_file = tmp_path / "notes" / "todo.md"
    outside_file.parent.mkdir(parents=True)
    outside_file.write_text("unrelated\n", encoding="utf-8")
    reload_calls = []

    rail = TeamSkillLibraryReloadRail(
        global_skills_dir=global_skills_dir,
        reload_skill_views=lambda: reload_calls.append("reload"),
    )
    ctx = type(
        "_Ctx",
        (),
        {
            "inputs": ToolCallInputs(
                tool_name="write_file",
                tool_args={"file_path": str(outside_file)},
            ),
            "agent": None,
        },
    )()

    await rail.after_tool_call(ctx)

    assert reload_calls == []


def test_bootstrap_skill_visibility_seeds_member_allow_only_once(tmp_path):
    """Config seeds a member document once; later config edits never overwrite it."""
    member_path = tmp_path / "reviewer_workspace" / SKILL_VISIBILITY_FILENAME

    first = bootstrap_skill_visibility(
        member_path,
        scope=SCOPE_MEMBER,
        entity_id="reviewer",
        allow=["skill-a"],
        bootstrapped_from="config:agents.teammate.skills",
    )
    assert first.allow == ["skill-a"]

    second = bootstrap_skill_visibility(
        member_path,
        scope=SCOPE_MEMBER,
        entity_id="reviewer",
        allow=["skill-b"],
        bootstrapped_from="config:agents.teammate.skills",
    )

    test_logger.info("member allow after second bootstrap: %s", second.allow)
    assert second.allow == ["skill-a"]
    assert second.bootstrapped_from == "config:agents.teammate.skills"


def test_compose_skill_visibility_unions_allow_and_prefers_deny(tmp_path):
    """Member and team allow-lists union; any deny wins over every allow."""
    member_path = tmp_path / "reviewer_workspace" / SKILL_VISIBILITY_FILENAME
    team_path = tmp_path / "team_workspace" / SKILL_VISIBILITY_FILENAME
    set_skill_visibility(
        member_path,
        scope=SCOPE_MEMBER,
        entity_id="reviewer",
        allow=["skill-a", "skill-shared"],
        deny=["skill-x"],
    )
    set_skill_visibility(
        team_path,
        scope=SCOPE_TEAM,
        entity_id="demo_team",
        allow=["skill-b", "skill-shared"],
        deny=["skill-y"],
    )

    member = read_skill_visibility(member_path, scope=SCOPE_MEMBER, entity_id="reviewer")
    team = read_skill_visibility(team_path, scope=SCOPE_TEAM, entity_id="demo_team")
    enabled, disabled = compose_skill_visibility(member, team, ["skill-z"])

    test_logger.info("composed enabled=%s disabled=%s", sorted(enabled), sorted(disabled))
    assert enabled == {"skill-a", "skill-b", "skill-shared"}
    assert disabled == {"skill-x", "skill-y", "skill-z"}


def test_compose_skill_visibility_empty_allow_means_inherit_everything():
    """An empty allow-list is passed through, so later library additions stay visible."""
    member = SkillVisibility(scope=SCOPE_MEMBER, id="reviewer")
    team = SkillVisibility(scope=SCOPE_TEAM, id="demo_team")

    enabled, disabled = compose_skill_visibility(member, team, None)

    assert enabled == set()
    assert disabled == set()


def test_compose_skill_visibility_without_team_uses_member_only():
    """A member outside any team composes from its own document alone."""
    member = SkillVisibility(scope=SCOPE_MEMBER, id="solo", allow=["skill-a"], deny=["skill-b"])

    enabled, disabled = compose_skill_visibility(member, None, ["skill-c"])

    assert enabled == {"skill-a"}
    assert disabled == {"skill-b", "skill-c"}


def test_read_skill_visibility_degrades_to_permissive_on_corrupt_document(tmp_path):
    """A corrupt document must never leave an agent with zero Skills."""
    metadata_path = tmp_path / SKILL_VISIBILITY_FILENAME
    metadata_path.write_text("{not json", encoding="utf-8")

    visibility = read_skill_visibility(metadata_path, scope=SCOPE_MEMBER, entity_id="reviewer")

    test_logger.info("degraded visibility: allow=%s deny=%s", visibility.allow, visibility.deny)
    assert visibility.allow == []
    assert visibility.deny == []
    assert visibility.is_unrestricted is True
