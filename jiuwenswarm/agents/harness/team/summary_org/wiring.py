# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Install JiuwenSwarm's lazy Summary Team launcher into agent-core."""

from __future__ import annotations

from typing import Any

_ADAPTERS_FLAG = "_jiuwen_summary_org_adapter_ready"
_RUNNER_TEAM_RUNTIME_ATTR = "_team_runtime_manager"


def register_summary_adapter_installer() -> None:
    """Register the lazy installer without building a Summary Team during bootstrap."""
    from openjiuwen.agent_teams.runtime import TeamRuntimeManager
    from openjiuwen.core.runner.runner import GLOBAL_RUNNER

    manager = vars(GLOBAL_RUNNER).get(_RUNNER_TEAM_RUNTIME_ATTR)
    if manager is None:
        manager = TeamRuntimeManager()
        setattr(GLOBAL_RUNNER, _RUNNER_TEAM_RUNTIME_ATTR, manager)
    org_runtime = getattr(manager, "organization_runtime_manager", None)
    installer = getattr(org_runtime, "set_summary_adapter_installer", None)
    if callable(installer):
        installer(install_summary_org_adapter)


def install_summary_org_adapter(org_runtime: Any) -> None:
    """Inject the Summary Team launcher once when an execution first requests it."""
    if org_runtime is None or getattr(org_runtime, _ADAPTERS_FLAG, False):
        return
    setter = getattr(org_runtime, "set_summary_team_launcher", None)
    if not callable(setter):
        return
    from jiuwenswarm.agents.harness.team.summary_org.launcher import JiuwenSummaryTeamLauncher

    setter(JiuwenSummaryTeamLauncher(runtime_manager=getattr(org_runtime, "_team_runtime_manager", None)))
    setattr(org_runtime, _ADAPTERS_FLAG, True)


__all__ = ["install_summary_org_adapter", "register_summary_adapter_installer"]
