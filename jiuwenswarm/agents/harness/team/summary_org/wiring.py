# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Install JiuwenSwarm's lazy Summary Team launcher into agent-core."""

from __future__ import annotations

import logging
from typing import Any

_ADAPTERS_FLAG = "_jiuwen_summary_org_adapter_ready"
_PROGRESS_PUBLISHER_FLAG = "_jiuwen_organization_progress_publisher_ready"
_RUNNER_TEAM_RUNTIME_ATTR = "_team_runtime_manager"
logger = logging.getLogger(__name__)


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
    else:
        logger.warning("Summary adapter installer was not registered: organization runtime unavailable")
        return
    _install_organization_progress_publisher(org_runtime)


def _install_organization_progress_publisher(org_runtime: Any) -> None:
    """Register factual Organization milestones without launching a Summary Team."""
    if org_runtime is None or getattr(org_runtime, _PROGRESS_PUBLISHER_FLAG, False):
        return
    setter = getattr(org_runtime, "set_organization_progress_publisher", None)
    if not callable(setter):
        logger.warning("Organization progress publisher was not registered: runtime lacks setter")
        return

    from jiuwenswarm.agents.harness.team.expert_org.launcher import (
        JiuwenExpertTeamLauncher,
        _resolve_launch_channel_id,
    )

    runtime = getattr(org_runtime, "_team_runtime_manager", None)
    relay = JiuwenExpertTeamLauncher(runtime_manager=runtime, organization_runtime=org_runtime)

    async def publish_progress(session_id: str, root_team_id: str, progress: dict[str, Any]) -> None:
        """Push a factual Root Leader milestone without exposing model reasoning."""
        phase = str(progress.get("phase") or "organization_progress")
        await relay._push_expert_payload(
            {
                "event_type": "org.progress",
                **progress,
                "role": "leader",
                "team_id": root_team_id,
                "team_name": root_team_id,
            },
            team_id=root_team_id,
            session_id=session_id,
            channel_id=_resolve_launch_channel_id(None, session_id),
            round_id=f"org-progress-{phase}",
            frame_sequence=0,
            source="org_root_progress",
        )

    setter(publish_progress)
    setattr(org_runtime, _PROGRESS_PUBLISHER_FLAG, True)


def install_summary_org_adapter(org_runtime: Any) -> None:
    """Inject the Summary Team launcher once when an execution first requests it."""
    if org_runtime is None or getattr(org_runtime, _ADAPTERS_FLAG, False):
        return
    setter = getattr(org_runtime, "set_summary_team_launcher", None)
    if not callable(setter):
        return
    from jiuwenswarm.agents.harness.team.summary_org.launcher import JiuwenSummaryTeamLauncher
    from jiuwenswarm.agents.harness.team.expert_org.launcher import JiuwenExpertTeamLauncher

    runtime = getattr(org_runtime, "_team_runtime_manager", None)
    setter(JiuwenSummaryTeamLauncher(runtime_manager=runtime))
    relay = JiuwenExpertTeamLauncher(runtime_manager=runtime, organization_runtime=org_runtime)

    async def run_summary_turn(team_id: str, session_id: str, inputs: object) -> bool:
        """Relay only Summary Team background frames to their Web session."""
        return await relay.run_organization_turn(
            team_id, session_id, inputs, source="org_summary_background"
        )

    org_runtime.set_summary_turn_runner(run_summary_turn)
    _install_organization_progress_publisher(org_runtime)
    setattr(org_runtime, _ADAPTERS_FLAG, True)


__all__ = ["install_summary_org_adapter", "register_summary_adapter_installer"]
