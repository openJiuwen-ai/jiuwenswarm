# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""M2: configure_global_skills_dir pins the shared TeamSkillUse library."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from jiuwenswarm.agents.harness.team.bootstrap import configure_agent_teams_home


def test_configure_agent_teams_home_pins_global_skills_dir(tmp_path: Path) -> None:
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
        "jiuwenswarm.common.utils.get_agent_skills_dir",
        return_value=skills,
    ), patch(
        "openjiuwen.agent_teams.paths",
        fake_paths,
    ):
        configure_agent_teams_home()

    home.assert_called_once_with(workspace)
    global_skills.assert_called_once_with(skills)


def test_configure_agent_teams_home_soft_skips_missing_global_skills(
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
        "jiuwenswarm.common.utils.get_agent_skills_dir",
        return_value=skills,
    ), patch(
        "openjiuwen.agent_teams.paths",
        fake_paths,
    ):
        configure_agent_teams_home()

    home.assert_called_once_with(workspace)


def test_import_team_manager_does_not_raise() -> None:
    """Smoke: module-level bootstrap must not ImportError on older agent-core."""
    import jiuwenswarm.agents.harness.team.team_manager  # noqa: F401
