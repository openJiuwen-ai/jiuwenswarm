# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Install ExpertGroupCatalog / ExpertTeamLauncher into OrganizationRuntime."""

from __future__ import annotations

from typing import Any

_ADAPTERS_FLAG = "_jiuwen_expert_org_adapters_ready"
_RUNNER_TEAM_RUNTIME_ATTR = "_team_runtime_manager"


def register_expert_adapter_installer() -> None:
    """Register the lazy adapter installer on ``GLOBAL_RUNNER`` org runtime.

    Call once at process/team bootstrap. Does not build Catalog/Launcher; those
    are constructed on first ``list_expert_groups`` / ``create_and_invite``.
    """
    from openjiuwen.agent_teams.runtime import TeamRuntimeManager
    from openjiuwen.core.runner.runner import GLOBAL_RUNNER

    manager = vars(GLOBAL_RUNNER).get(_RUNNER_TEAM_RUNTIME_ATTR)
    if manager is None:
        manager = TeamRuntimeManager()
        setattr(GLOBAL_RUNNER, _RUNNER_TEAM_RUNTIME_ATTR, manager)
    org_runtime = getattr(manager, "organization_runtime_manager", None)
    set_installer = getattr(org_runtime, "set_expert_adapter_installer", None)
    if callable(set_installer):
        set_installer(install_expert_org_adapters)


def install_expert_org_adapters(org_runtime: Any) -> None:
    """Idempotently inject host Catalog + Launcher into OrganizationRuntimeManager.

    Intended as ``set_expert_adapter_installer(install_expert_org_adapters)`` so
    adapters are constructed on first ``list_expert_groups`` /
    ``create_and_invite_expert_team``, not at TeamRuntimeManager setup.
    """
    if org_runtime is None:
        return
    if getattr(org_runtime, _ADAPTERS_FLAG, False):
        return

    set_catalog = getattr(org_runtime, "set_expert_group_catalog", None)
    set_launcher = getattr(org_runtime, "set_expert_team_launcher", None)
    if not callable(set_catalog) or not callable(set_launcher):
        # Older openjiuwen without expert-group org contracts.
        return

    from jiuwenswarm.agents.harness.team.expert_org.catalog import JiuwenExpertGroupCatalog
    from jiuwenswarm.agents.harness.team.expert_org.launcher import JiuwenExpertTeamLauncher

    team_runtime_manager = getattr(org_runtime, "_team_runtime_manager", None)
    launcher = JiuwenExpertTeamLauncher(
        runtime_manager=team_runtime_manager,
        organization_runtime=org_runtime,
    )
    set_catalog(JiuwenExpertGroupCatalog())
    set_launcher(launcher)
    launcher.ensure_turn_runner_installed()
    setattr(org_runtime, _ADAPTERS_FLAG, True)


__all__ = ["install_expert_org_adapters", "register_expert_adapter_installer"]
