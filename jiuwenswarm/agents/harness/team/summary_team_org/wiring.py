# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Install the host SummaryTeamFactory into OrganizationRuntime."""

from __future__ import annotations

from typing import Any

_FACTORY_FLAG = "_jiuwen_summary_team_factory_ready"
_RUNNER_TEAM_RUNTIME_ATTR = "_team_runtime_manager"


def register_summary_factory_installer() -> None:
    """Register the lazy factory installer on ``GLOBAL_RUNNER`` org runtime.

    Call once at process/team bootstrap. Does not build the factory; the
    factory is constructed on first summary-task event that needs it.
    """
    from openjiuwen.agent_teams.runtime import TeamRuntimeManager
    from openjiuwen.core.runner.runner import GLOBAL_RUNNER

    manager = vars(GLOBAL_RUNNER).get(_RUNNER_TEAM_RUNTIME_ATTR)
    if manager is None:
        manager = TeamRuntimeManager()
        setattr(GLOBAL_RUNNER, _RUNNER_TEAM_RUNTIME_ATTR, manager)
    org_runtime = getattr(manager, "organization_runtime_manager", None)
    set_installer = getattr(org_runtime, "set_summary_team_factory_installer", None)
    if callable(set_installer):
        set_installer(install_summary_factory)


def install_summary_factory(org_runtime: Any) -> None:
    """Idempotently inject the host SummaryTeamFactory into OrganizationRuntimeManager.

    Intended as ``set_summary_team_factory_installer(install_summary_factory)`` so
    the factory is constructed on first summary-task event, not at
    TeamRuntimeManager setup.
    """
    if org_runtime is None:
        return
    if getattr(org_runtime, _FACTORY_FLAG, False):
        return

    set_factory = getattr(org_runtime, "set_summary_team_factory", None)
    if not callable(set_factory):
        # Older openjiuwen without the summary-team org contract.
        return

    from jiuwenswarm.agents.harness.team.summary_team_org.factory import (
        JiuwenSummaryTeamFactory,
    )

    team_runtime_manager = getattr(org_runtime, "_team_runtime_manager", None)
    set_factory(JiuwenSummaryTeamFactory(runtime_manager=team_runtime_manager))
    setattr(org_runtime, _FACTORY_FLAG, True)


__all__ = ["install_summary_factory", "register_summary_factory_installer"]
