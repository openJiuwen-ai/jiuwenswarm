# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the legacy Skill view migration.

Skills used to be materialized as a ``skills/`` directory of links inside every
team and member workspace. :func:`migrate_team_skill_views` reads those
directories once, turns them into ``skills-visibility.json`` allow lists and
removes them.

The migrated allow list describes what a workspace was *actually* allowed to
see, so it must beat the default/config seed written during team assembly. These
tests pin that down as a *rank* guarantee rather than an ordering one: the
config seed is deliberately written **before** the migration here, which is the
exact sequence a future refactor could introduce, and the migrated allow list
must still win. A regression would not raise anything — it would silently widen
the workspace's Skill view, which is why the guard lives in a test.
"""

import json
import logging
import os
import shutil

import pytest
from openjiuwen.agent_teams.paths import (
    configure_openjiuwen_home,
    get_agent_teams_home,
    member_skill_visibility_path,
    reset_openjiuwen_home,
    team_skill_visibility_path,
)
from openjiuwen.agent_teams.skill.visibility import (
    AUTHORITY_EXPLICIT,
    AUTHORITY_MIGRATION,
    AUTHORITY_SEED,
    SCOPE_MEMBER,
    SCOPE_TEAM,
    bootstrap_skill_visibility,
    compose_skill_visibility,
    read_skill_visibility,
    set_skill_visibility,
)

from jiuwenswarm.common.utils import migrate_team_skill_views

test_logger = logging.getLogger("tests.skill_view_migration")

TEAM_NAME = "demo_team"
MEMBER_NAME = "reviewer"


@pytest.fixture
def openjiuwen_home(tmp_path):
    """Point the agent-teams layout at a throwaway home for one test."""
    configure_openjiuwen_home(tmp_path / "openjiuwen")
    try:
        yield tmp_path / "openjiuwen"
    finally:
        reset_openjiuwen_home()


def _make_library(tmp_path, names: list[str]):
    """Create the single physical Skill library holding ``names``."""
    library = tmp_path / "library"
    for name in names:
        skill_dir = library / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    return library


def _make_legacy_view(view_dir, library, names: list[str]) -> None:
    """Materialize a legacy view directory as sandbox-style library copies.

    Copies rather than symlinks: they are the shape sandboxed runtimes produced,
    they are removable by the migration (identical ``SKILL.md`` bytes) and they
    need no symlink privilege on Windows.
    """
    view_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copytree(library / name, view_dir / name, dirs_exist_ok=True)


def _member_view_dir(member_name: str = MEMBER_NAME):
    """Return the legacy view directory of one member workspace."""
    return get_agent_teams_home() / TEAM_NAME / "workspaces" / f"{member_name}_workspace" / "skills"


def _team_view_dir():
    """Return the legacy view directory of the team workspace."""
    return get_agent_teams_home() / TEAM_NAME / "team-workspace" / "skills"


def _seed_like_bootstrap(library_names: list[str]) -> None:
    """Write the startup seeds, as if they had landed before migration.

    The member seed comes from ``config.agents.<role>.skills`` at member
    assembly; the team seed is the unrestricted default that team workspace
    initialization writes. Both are the widest values the startup path can
    produce, so if either one wins the workspace ends up with *more* Skills than
    it had before the refactor.
    """
    bootstrap_skill_visibility(
        member_skill_visibility_path(TEAM_NAME, MEMBER_NAME),
        scope=SCOPE_MEMBER,
        entity_id=MEMBER_NAME,
        allow=library_names,
        bootstrapped_from="config:agents.teammate.skills",
    )
    bootstrap_skill_visibility(
        team_skill_visibility_path(TEAM_NAME),
        scope=SCOPE_TEAM,
        entity_id=TEAM_NAME,
        allow=None,
        bootstrapped_from="team_workspace:initialize",
    )


def test_migration_wins_over_a_config_seed_written_first(tmp_path, openjiuwen_home):
    """A config seed that landed first must not widen the migrated allow list."""
    library = _make_library(tmp_path, ["skill-a", "skill-b", "skill-c"])
    _make_legacy_view(_member_view_dir(), library, ["skill-a"])
    _make_legacy_view(_team_view_dir(), library, ["skill-a"])

    _seed_like_bootstrap(["skill-a", "skill-b", "skill-c"])
    migrated = migrate_team_skill_views(library_dir=library)

    member = read_skill_visibility(
        member_skill_visibility_path(TEAM_NAME, MEMBER_NAME),
        scope=SCOPE_MEMBER,
        entity_id=MEMBER_NAME,
    )
    team = read_skill_visibility(
        team_skill_visibility_path(TEAM_NAME),
        scope=SCOPE_TEAM,
        entity_id=TEAM_NAME,
    )
    enabled, disabled = compose_skill_visibility(member, team, None)

    test_logger.info("migrated=%d member.allow=%s team.allow=%s", migrated, member.allow, team.allow)
    assert migrated == 2
    assert member.allow == ["skill-a"]
    assert member.authority == AUTHORITY_MIGRATION
    assert member.bootstrapped_from == "migration:symlinks"
    assert team.allow == ["skill-a"]
    assert team.authority == AUTHORITY_MIGRATION
    assert enabled == {"skill-a"}
    assert disabled == set()


def test_migration_result_is_independent_of_the_seed_order(tmp_path, openjiuwen_home):
    """Seeding after the migration yields the same document as seeding before."""
    library = _make_library(tmp_path, ["skill-a", "skill-b", "skill-c"])
    _make_legacy_view(_member_view_dir(), library, ["skill-a"])
    _make_legacy_view(_team_view_dir(), library, ["skill-a"])

    migrate_team_skill_views(library_dir=library)
    _seed_like_bootstrap(["skill-a", "skill-b", "skill-c"])

    member = read_skill_visibility(
        member_skill_visibility_path(TEAM_NAME, MEMBER_NAME),
        scope=SCOPE_MEMBER,
        entity_id=MEMBER_NAME,
    )
    team = read_skill_visibility(
        team_skill_visibility_path(TEAM_NAME),
        scope=SCOPE_TEAM,
        entity_id=TEAM_NAME,
    )

    test_logger.info("member.allow=%s team.allow=%s", member.allow, team.allow)
    assert member.allow == ["skill-a"]
    assert team.allow == ["skill-a"]
    assert member.authority == AUTHORITY_MIGRATION


def test_migration_never_overrides_an_explicit_authorization(tmp_path, openjiuwen_home):
    """An operator grant/revocation outranks anything the migration derives."""
    library = _make_library(tmp_path, ["skill-a", "skill-b", "skill-c"])
    _make_legacy_view(_member_view_dir(), library, ["skill-a", "skill-b"])
    member_path = member_skill_visibility_path(TEAM_NAME, MEMBER_NAME)
    set_skill_visibility(
        member_path,
        scope=SCOPE_MEMBER,
        entity_id=MEMBER_NAME,
        allow=["skill-c"],
        deny=["skill-a"],
    )

    migrate_team_skill_views(library_dir=library)

    member = read_skill_visibility(member_path, scope=SCOPE_MEMBER, entity_id=MEMBER_NAME)
    test_logger.info("member.allow=%s member.deny=%s", member.allow, member.deny)
    assert member.allow == ["skill-c"]
    assert member.deny == ["skill-a"]
    assert member.authority == AUTHORITY_EXPLICIT


def test_migration_keeps_a_deny_list_when_it_reseeds(tmp_path, openjiuwen_home):
    """Reseeding replaces the allow list only; a revocation always survives."""
    library = _make_library(tmp_path, ["skill-a", "skill-b"])
    _make_legacy_view(_member_view_dir(), library, ["skill-a"])
    member_path = member_skill_visibility_path(TEAM_NAME, MEMBER_NAME)
    bootstrap_skill_visibility(
        member_path,
        scope=SCOPE_MEMBER,
        entity_id=MEMBER_NAME,
        allow=["skill-a", "skill-b"],
        bootstrapped_from="config:agents.teammate.skills",
    )
    document = json.loads(member_path.read_text(encoding="utf-8"))
    document["deny"] = ["skill-b"]
    member_path.write_text(json.dumps(document), encoding="utf-8")

    migrate_team_skill_views(library_dir=library)

    member = read_skill_visibility(member_path, scope=SCOPE_MEMBER, entity_id=MEMBER_NAME)
    test_logger.info("member.allow=%s member.deny=%s", member.allow, member.deny)
    assert member.allow == ["skill-a"]
    assert member.deny == ["skill-b"]


def test_migration_records_a_full_view_as_inherit_everything(tmp_path, openjiuwen_home):
    """A view covering the library becomes an empty allow list, not a snapshot."""
    library = _make_library(tmp_path, ["skill-a", "skill-b"])
    _make_legacy_view(_member_view_dir(), library, ["skill-a", "skill-b"])

    migrate_team_skill_views(library_dir=library)

    member = read_skill_visibility(
        member_skill_visibility_path(TEAM_NAME, MEMBER_NAME),
        scope=SCOPE_MEMBER,
        entity_id=MEMBER_NAME,
    )
    test_logger.info("member.allow=%s unrestricted=%s", member.allow, member.is_unrestricted)
    assert member.allow == []
    assert member.is_unrestricted is True


def test_migration_is_idempotent_and_removes_the_legacy_view(tmp_path, openjiuwen_home):
    """A second run rewrites nothing: the document already holds the top rank."""
    library = _make_library(tmp_path, ["skill-a", "skill-b"])
    view_dir = _member_view_dir()
    _make_legacy_view(view_dir, library, ["skill-a"])

    first = migrate_team_skill_views(library_dir=library)
    member_path = member_skill_visibility_path(TEAM_NAME, MEMBER_NAME)
    stamp = member_path.stat().st_mtime_ns
    second = migrate_team_skill_views(library_dir=library)

    test_logger.info("migrated first=%d second=%d", first, second)
    assert first == 1
    assert second == 0
    assert not view_dir.exists()
    assert member_path.stat().st_mtime_ns == stamp
    assert read_skill_visibility(member_path, scope=SCOPE_MEMBER, entity_id=MEMBER_NAME).allow == ["skill-a"]


def test_empty_scaffold_dir_is_removed_but_not_reported_as_a_migration(tmp_path, openjiuwen_home):
    """An empty ``skills/`` scaffold grants nothing, so it seeds and counts nothing.

    Older workspace schemas created an empty ``skills/`` directory (with a
    ``.workspace`` marker) in every member workspace. Counting it as a migrated
    legacy view made the startup migration report work it never did, and seeding
    from it would have stamped a migration-rank document over a workspace that
    never had a view at all.
    """
    library = _make_library(tmp_path, ["skill-a"])
    view_dir = _member_view_dir()
    view_dir.mkdir(parents=True, exist_ok=True)
    (view_dir / ".workspace").write_text("", encoding="utf-8")

    migrated = migrate_team_skill_views(library_dir=library)

    test_logger.info("migrated=%d scaffold_exists=%s", migrated, view_dir.exists())
    assert migrated == 0
    assert not view_dir.exists()
    assert not member_skill_visibility_path(TEAM_NAME, MEMBER_NAME).exists()


def test_scaffold_dir_with_a_user_owned_entry_is_left_alone(tmp_path, openjiuwen_home):
    """A hand-made directory inside ``skills/`` is never deleted by the cleanup."""
    library = _make_library(tmp_path, ["skill-a"])
    view_dir = _member_view_dir()
    mine = view_dir / "_scratch"
    mine.mkdir(parents=True, exist_ok=True)
    (mine / "notes.md").write_text("mine", encoding="utf-8")

    migrated = migrate_team_skill_views(library_dir=library)

    test_logger.info("migrated=%d kept=%s", migrated, mine.exists())
    assert migrated == 0
    assert (mine / "notes.md").read_text(encoding="utf-8") == "mine"


def test_user_owned_skill_dir_survives_next_to_removable_view_entries(tmp_path, openjiuwen_home):
    """A real Skill somebody authored in ``skills/`` is kept, links around it go.

    This is the judgement the legacy link remover made ("never delete a real
    member directory") and it has to survive the move to metadata: a directory
    whose ``SKILL.md`` differs from the library is the user's own work, not a
    materialized view of a library entry, so deleting it would lose data. The
    view directory therefore stays too, and the workspace is still credited with
    a migration because it really did carry a legacy view.

    Its name is *not* seeded into the allow list: the library holds no Skill of
    that name, so the entry could never resolve, and keeping it would turn an
    otherwise unrestricted view into a permanent restriction.
    """
    library = _make_library(tmp_path, ["skill-a", "skill-b"])
    view_dir = _member_view_dir()
    _make_legacy_view(view_dir, library, ["skill-a"])
    mine = view_dir / "handwritten"
    mine.mkdir(parents=True, exist_ok=True)
    (mine / "SKILL.md").write_text("---\nname: handwritten\n---\nmine\n", encoding="utf-8")

    migrated = migrate_team_skill_views(library_dir=library)

    member = read_skill_visibility(
        member_skill_visibility_path(TEAM_NAME, MEMBER_NAME),
        scope=SCOPE_MEMBER,
        entity_id=MEMBER_NAME,
    )
    test_logger.info("migrated=%d allow=%s kept=%s", migrated, member.allow, mine.exists())
    assert migrated == 1
    assert (mine / "SKILL.md").read_text(encoding="utf-8").endswith("mine\n")
    assert view_dir.is_dir()
    assert not (view_dir / "skill-a").exists()
    assert member.allow == ["skill-a"]


def test_view_names_absent_from_the_library_are_not_seeded(tmp_path, openjiuwen_home):
    """A view entry the library no longer holds must not enter the allow list.

    Such a name can never resolve to a Skill, but it would make the difference
    between an unrestricted document and one restricted forever, permanently
    narrowing what the workspace may see.
    """
    library = _make_library(tmp_path, ["skill-a", "skill-b"])
    view_dir = _member_view_dir()
    _make_legacy_view(view_dir, library, ["skill-a"])
    (view_dir / "was-uninstalled").mkdir(parents=True, exist_ok=True)

    migrate_team_skill_views(library_dir=library)

    member = read_skill_visibility(
        member_skill_visibility_path(TEAM_NAME, MEMBER_NAME),
        scope=SCOPE_MEMBER,
        entity_id=MEMBER_NAME,
    )
    test_logger.info("member.allow=%s", member.allow)
    assert member.allow == ["skill-a"]


def test_view_of_only_unknown_names_seeds_an_unrestricted_document(tmp_path, openjiuwen_home):
    """Nothing in the view exists in the library, so there is no grant to keep.

    The document is seeded unrestricted, exactly like an empty view: recording
    the unknown names instead would leave the workspace able to resolve no
    Skill at all.
    """
    library = _make_library(tmp_path, ["skill-a"])
    view_dir = _member_view_dir()
    view_dir.mkdir(parents=True, exist_ok=True)
    (view_dir / "replica").mkdir(parents=True, exist_ok=True)

    migrate_team_skill_views(library_dir=library)

    member = read_skill_visibility(
        member_skill_visibility_path(TEAM_NAME, MEMBER_NAME),
        scope=SCOPE_MEMBER,
        entity_id=MEMBER_NAME,
    )
    test_logger.info("member.allow=%s unrestricted=%s", member.allow, member.is_unrestricted)
    assert member.allow == []
    assert member.is_unrestricted is True


def test_symlinked_view_entries_are_unlinked_without_touching_the_library(tmp_path, openjiuwen_home):
    """Unlinking a view entry must never follow through to the library Skill."""
    library = _make_library(tmp_path, ["skill-a"])
    view_dir = _member_view_dir()
    view_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(library / "skill-a", view_dir / "skill-a", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # Windows without developer mode
        pytest.skip(f"symlink unavailable: {exc}")

    migrated = migrate_team_skill_views(library_dir=library)

    test_logger.info("migrated=%d library intact=%s", migrated, (library / "skill-a" / "SKILL.md").is_file())
    assert migrated == 1
    assert not view_dir.exists()
    assert (library / "skill-a" / "SKILL.md").is_file()


def test_config_seed_alone_still_wins_over_a_later_config_seed(tmp_path, openjiuwen_home):
    """Equal-rank seeds keep first-writer-wins: config never reverts itself."""
    member_path = tmp_path / "reviewer_workspace" / "skills-visibility.json"
    first = bootstrap_skill_visibility(
        member_path,
        scope=SCOPE_MEMBER,
        entity_id=MEMBER_NAME,
        allow=["skill-a"],
        bootstrapped_from="config:agents.teammate.skills",
    )
    second = bootstrap_skill_visibility(
        member_path,
        scope=SCOPE_MEMBER,
        entity_id=MEMBER_NAME,
        allow=["skill-a", "skill-b"],
        bootstrapped_from="config:agents.teammate.skills",
    )

    test_logger.info("first=%s second=%s", first.allow, second.allow)
    assert second.allow == ["skill-a"]
    assert second.authority == AUTHORITY_SEED
