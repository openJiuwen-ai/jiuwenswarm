# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Bootstrap helpers for JiuwenSwarm team integrations."""

from __future__ import annotations

from jiuwenswarm.common.utils import get_user_workspace_dir


def configure_agent_teams_home() -> None:
    """Point openjiuwen.agent_teams at JiuwenSwarm's user workspace root."""
    from openjiuwen.agent_teams.paths import configure_openjiuwen_home

    configure_openjiuwen_home(get_user_workspace_dir())
