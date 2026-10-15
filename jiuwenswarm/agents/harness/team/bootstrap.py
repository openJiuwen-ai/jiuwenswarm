# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Bootstrap helpers for JiuwenSwarm team integrations."""

from __future__ import annotations

from jiuwenswarm.common.utils import (
    configure_skill_library,
    get_user_workspace_dir,
    migrate_team_skill_views,
)


def prepare_team_storage() -> None:
    """Prepare Team paths and legacy visibility before a Team consumer reads.

    Re-evaluate paths on every entry so tests, named instances and configuration
    changes cannot retain another workspace's state. This installs no Team
    runtime or organization adapters and is also used by visibility RPCs.
    """
    from openjiuwen.agent_teams.paths import configure_openjiuwen_home

    configure_openjiuwen_home(get_user_workspace_dir())
    configure_skill_library()
    migrate_team_skill_views()


def configure_agent_teams_home() -> None:
    """Activate Team storage and organization integrations on Team use."""
    prepare_team_storage()
    # Register installer only; Catalog/Launcher stay lazy until first org tool use.
    from jiuwenswarm.agents.harness.team.expert_org.wiring import (
        register_expert_adapter_installer,
    )
    from jiuwenswarm.agents.harness.team.summary_org.wiring import (
        register_summary_adapter_installer,
    )

    register_expert_adapter_installer()
    register_summary_adapter_installer()
