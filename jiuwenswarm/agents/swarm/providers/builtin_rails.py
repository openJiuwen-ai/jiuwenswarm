# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Built-in rail declarations for swarm-owned team rails.

Parameterless rails use a class ``builder``; parameterized rails use a factory
function. Only swarm-owned rails live here; the generic openjiuwen rails
(``task_planning`` / ``security`` / ``heartbeat`` / ...) are declared and
registered by openjiuwen itself and referenced by their bare names from
``config_specs``. Team members mount ``swarm.team_accel_sys`` instead of
``core.sys_operation``.

Declarations run at import time (the module-level ``harness_element`` calls
populate the catalog). ``registry`` imports this module so the declarations are
present before ``register_from_catalog`` runs.
"""

from __future__ import annotations

from typing import Any

from openjiuwen.agent_teams.harness.manifest import (
    ConstructionInput,
    ElementKind,
    context_field,
    harness_element,
)

from jiuwenswarm.agents.harness.common.rails.avatar_rail import AvatarPromptRail
from jiuwenswarm.agents.harness.common.rails.llm_retry_notify_rail import (
    TeamMemberNotifyingLLMRetryRail,
)
from jiuwenswarm.agents.harness.common.rails.response_prompt_rail import (
    ResponsePromptRail,
)
from jiuwenswarm.agents.harness.team.accel.accel_prompt_rail import AccelPromptRail
from jiuwenswarm.agents.harness.team.accel.accel_sys_operation_rail import (
    AccelSysOperationRail,
)
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)
from jiuwenswarm.agents.harness.common.rails.workspace_quota_rail import (
    WorkspaceQuotaRail,
)

# Swarm-owned rail type names; namespaced under "swarm.".
RESPONSE_PROMPT = "swarm.response_prompt"
TEAM_ACCEL_PROMPT = "swarm.team_accel_prompt"
TEAM_ACCEL_SYS = "swarm.team_accel_sys"
STREAM_EVENT = "swarm.stream_event"
AVATAR_PROMPT = "swarm.avatar_prompt"
WORKSPACE_QUOTA = "swarm.workspace_quota"
TEAM_MEMBER_LLM_RETRY = "swarm.team_member_llm_retry"


class ResponsePromptInput(ConstructionInput):
    """Construction inputs for the response-format prompt rail."""

    channel: str = context_field(
        attr="channel",
        default="default",
        description="Resolved channel key.",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=RESPONSE_PROMPT,
    description="Appends the response-format prompt segment before each model call.",
)
def _build_response_prompt_rail(
    params: dict[str, Any],
    context: Any,
) -> ResponsePromptRail:
    inp = ResponsePromptInput.resolve(params, context)
    rail = ResponsePromptRail()
    rail.set_channel(inp.channel)
    return rail


class TeamAccelPromptInput(ConstructionInput):
    """Construction inputs for the team accel prompt rail."""

    role: str = context_field(
        attr="role",
        default="teammate",
        description="Member role (leader vs teammate) for cost-discipline copy.",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=TEAM_ACCEL_PROMPT,
    description=(
        "Injects team writing rules, short-message collaboration, and "
        "role-split cost discipline."
    ),
)
def _build_team_accel_prompt_rail(
    params: dict[str, Any],
    context: Any,
) -> AccelPromptRail:
    inp = TeamAccelPromptInput.resolve(params, context)
    return AccelPromptRail(role=inp.role)
harness_element(
    kind=ElementKind.RAIL,
    name=TEAM_ACCEL_SYS,
    description="Filesystem rail with flash read/edit and the stock shell toolset.",
    builder=AccelSysOperationRail,
)
harness_element(
    kind=ElementKind.RAIL,
    name=STREAM_EVENT,
    description="Emits JiuSwarm streaming events across the member's lifecycle.",
    builder=JiuSwarmStreamEventRail,
)
harness_element(
    kind=ElementKind.RAIL,
    name=AVATAR_PROMPT,
    description="Injects per-request digital-avatar prompt sections.",
    builder=AvatarPromptRail,
)
harness_element(
    kind=ElementKind.RAIL,
    name=WORKSPACE_QUOTA,
    description="Blocks bash/exec when workspace quota is exhausted.",
    builder=WorkspaceQuotaRail,
)


@harness_element(
    kind=ElementKind.RAIL,
    name=TEAM_MEMBER_LLM_RETRY,
    description="Retries transient model-call failures for team members with user notifications.",
)
def _build_team_member_llm_retry_rail(
    params: dict[str, Any],
    context: Any,
) -> TeamMemberNotifyingLLMRetryRail:
    del context
    return TeamMemberNotifyingLLMRetryRail(**params)


__all__ = [
    "RESPONSE_PROMPT",
    "TEAM_ACCEL_PROMPT",
    "TEAM_ACCEL_SYS",
    "STREAM_EVENT",
    "AVATAR_PROMPT",
    "WORKSPACE_QUOTA",
    "TEAM_MEMBER_LLM_RETRY",
]
