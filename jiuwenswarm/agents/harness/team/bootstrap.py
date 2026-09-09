# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Bootstrap helpers for JiuwenSwarm team integrations."""

from __future__ import annotations

from jiuwenswarm.common.utils import get_user_workspace_dir


def configure_agent_teams_home() -> None:
    """Point openjiuwen.agent_teams at JiuwenSwarm's user workspace root."""
    from openjiuwen.agent_teams.paths import configure_openjiuwen_home

    configure_openjiuwen_home(get_user_workspace_dir())
    # Register installers only; Catalog/Launcher/Factory stay lazy until first
    # org tool use or first summary-task event.
    from jiuwenswarm.agents.harness.team.expert_org.wiring import (
        register_expert_adapter_installer,
    )
    from jiuwenswarm.agents.harness.team.summary_team_org.wiring import (
        register_summary_factory_installer,
    )

    register_expert_adapter_installer()
    register_summary_factory_installer()
