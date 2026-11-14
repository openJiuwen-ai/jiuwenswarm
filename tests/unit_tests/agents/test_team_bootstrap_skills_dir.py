# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""M2: configure_global_skills_dir pins the shared TeamSkillUse library."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from jiuwenswarm.agents.harness.team.bootstrap import configure_agent_teams_home
from jiuwenswarm.agents.swarm.config_specs import _team_skill_use_rail_spec
from jiuwenswarm.common.local_env_config import ENV_CONFIG_DICT
from jiuwenswarm.common.utils import (
    JIUWENSWARM_SHARED_SKILLS_DIRS_ENV,
    get_agent_skills_dir,
    primary_team_skill_library_dir,
    resolve_team_skill_library_dirs,
)


def test_configure_agent_teams_home_pins_primary_library_dir(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    workspace = tmp_path / "workspace"
    skills.mkdir()
    workspace.mkdir()

    home = MagicMock()
    global_skills = MagicMock()
    fake_paths = SimpleNamespace(
        configure_openjiuwen_home=home,
        configure_global_skills_dir=global_skills,
    )

    with patch(
        "jiuwenswarm.agents.harness.team.bootstrap.get_user_workspace_dir",
        return_value=workspace,
    ), patch(
        "jiuwenswarm.common.utils.primary_team_skill_library_dir",
        return_value=skills,
    ), patch(
        "openjiuwen.agent_teams.paths",
        fake_paths,
    ):
        configure_agent_teams_home()

    home.assert_called_once_with(workspace)
    global_skills.assert_called_once_with(skills)


def test_configure_agent_teams_home_requires_global_skills_pin(
    tmp_path: Path,
) -> None:
    skills = tmp_path / "skills"
    workspace = tmp_path / "workspace"
    skills.mkdir()
    workspace.mkdir()

    home = MagicMock()
    fake_paths = SimpleNamespace(configure_openjiuwen_home=home)

    with patch(
        "jiuwenswarm.agents.harness.team.bootstrap.get_user_workspace_dir",
        return_value=workspace,
    ), patch(
        "jiuwenswarm.common.utils.primary_team_skill_library_dir",
        return_value=skills,
    ), patch(
        "openjiuwen.agent_teams.paths",
        fake_paths,
    ), pytest.raises(ImportError, match="configure_global_skills_dir"):
        configure_agent_teams_home()

    home.assert_called_once_with(workspace)


def test_primary_team_skill_library_dir_prefers_last_shared(tmp_path: Path) -> None:
    official = tmp_path / "office-claw-skills"
    user = tmp_path / "user-skills"
    official.mkdir()
    user.mkdir()
    ENV_CONFIG_DICT[JIUWENSWARM_SHARED_SKILLS_DIRS_ENV] = (
        f"{official}{__import__('os').pathsep}{user}"
    )
    try:
        dirs = resolve_team_skill_library_dirs()
        assert dirs == [official.resolve(), user.resolve()]
        assert primary_team_skill_library_dir() == user.resolve()
        assert primary_team_skill_library_dir() != get_agent_skills_dir()
    finally:
        ENV_CONFIG_DICT.pop(JIUWENSWARM_SHARED_SKILLS_DIRS_ENV, None)


def test_team_skill_use_rail_spec_declares_full_library_list(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    ENV_CONFIG_DICT[JIUWENSWARM_SHARED_SKILLS_DIRS_ENV] = str(shared)
    try:
        rail = _team_skill_use_rail_spec({})
        assert rail.params["skills_dir"] == [str(shared.resolve())]
    finally:
        ENV_CONFIG_DICT.pop(JIUWENSWARM_SHARED_SKILLS_DIRS_ENV, None)


def test_import_team_manager_does_not_raise() -> None:
    """Smoke: team_manager import succeeds with pinned TeamSkillUse agent-core."""
    import jiuwenswarm.agents.harness.team.team_manager  # noqa: F401
