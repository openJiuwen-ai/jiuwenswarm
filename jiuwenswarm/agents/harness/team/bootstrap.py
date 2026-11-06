# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Bootstrap helpers for JiuwenSwarm team integrations."""

from __future__ import annotations

import logging

from jiuwenswarm.common.utils import get_user_workspace_dir

logger = logging.getLogger(__name__)


def configure_agent_teams_home() -> None:
    """Point openjiuwen.agent_teams at JiuwenSwarm's user workspace root.

    Also pins agent-core's single ``global_skills_dir`` to the platform primary
    library (SHARED tip / user skills when present, else workspace skills) so
    identity completion and write-policy rails do not fall back to
    ``~/.openjiuwen/workspace/skills``. Team scan roots themselves are the full
    :func:`~jiuwenswarm.common.utils.resolve_team_skill_library_dirs` list,
    declared on ``core.team.skill_use`` — same as DeepAdapter.

    Soft-degrades when the installed agent-core lacks ``configure_global_skills_dir``
    (older ``dev-stable`` pins): home is still configured; only the global skills
    override is skipped with a warning.
    """
    import openjiuwen.agent_teams.paths as paths_mod

    from jiuwenswarm.common.utils import primary_team_skill_library_dir

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
    configure_skills(primary_team_skill_library_dir())
