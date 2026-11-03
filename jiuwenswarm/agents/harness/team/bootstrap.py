# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Bootstrap helpers for JiuwenSwarm team integrations."""

from __future__ import annotations

import logging

from jiuwenswarm.common.utils import get_user_workspace_dir

logger = logging.getLogger(__name__)


def configure_agent_teams_home() -> None:
    """Point openjiuwen.agent_teams at JiuwenSwarm's user workspace root.

    Also pins the single shared Skill library to the platform skills directory
    so ``core.team.skill_use`` (TeamSkillUseRail) scans the same store Hub
    installs into — not a second copy under ``~/.openjiuwen/workspace/skills``.

    Soft-degrades when the installed agent-core lacks ``configure_global_skills_dir``
    (older ``dev-stable`` pins): home is still configured; only the global skills
    override is skipped with a warning.
    """
    import openjiuwen.agent_teams.paths as paths_mod

    from jiuwenswarm.common.utils import get_agent_skills_dir

    configure_home = getattr(paths_mod, "configure_openjiuwen_home", None)
    if configure_home is None:
        raise ImportError(
            "openjiuwen.agent_teams.paths.configure_openjiuwen_home is required"
        )
    configure_home(get_user_workspace_dir())

    configure_skills = getattr(paths_mod, "configure_global_skills_dir", None)
    if configure_skills is None:
        logger.warning(
            "[TeamBootstrap] configure_global_skills_dir missing in agent-core; "
            "skipping global skills dir pin (upgrade openjiuwen for TeamSkillUse)"
        )
        return
    configure_skills(get_agent_skills_dir())
