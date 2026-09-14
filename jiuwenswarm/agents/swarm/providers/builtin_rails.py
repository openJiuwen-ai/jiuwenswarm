# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""No-factory class-rail declarations for swarm-owned team rails.

These rails take no construction params (a bare ``cls()``), so they are declared
with a class ``builder`` rather than a factory function. Only swarm-owned rail
classes live here; the generic openjiuwen rails (``sys_operation`` /
``task_planning`` / ``security`` / ``heartbeat`` / ...) are declared and
registered by openjiuwen itself and referenced by their bare names from
``config_specs``.

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
from jiuwenswarm.agents.harness.common.rails.response_prompt_rail import (
    ResponsePromptRail,
)
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)
from jiuwenswarm.agents.harness.common.rails.llm_retry_notify_rail import (
    TeamMemberNotifyingLLMRetryRail,
)

# No-parameter swarm-owned rail type names; namespaced under "swarm.".
RESPONSE_PROMPT = "swarm.response_prompt"
STREAM_EVENT = "swarm.stream_event"
AVATAR_PROMPT = "swarm.avatar_prompt"
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


@harness_element(
    kind=ElementKind.RAIL,
    name=TEAM_MEMBER_LLM_RETRY,
    description="Retries transient model-call failures for dispatched team members.",
)
def _build_team_member_llm_retry_rail(
    params: dict[str, Any],
    context: Any,
) -> TeamMemberNotifyingLLMRetryRail:
    del context
    retry_params = params if isinstance(params, dict) else {}
    rail_kwargs: dict[str, Any] = {
        "max_retries": retry_params.get("max_retries", 2),
        "repeat_min_pattern_chars": retry_params.get("repeat_min_pattern_chars", 2),
        "repeat_max_pattern_chars": retry_params.get("repeat_max_pattern_chars", 64),
        "repeat_min_count": retry_params.get("repeat_min_count", 6),
        "repeat_min_total_chars": retry_params.get("repeat_min_total_chars", 160),
        "repeat_window_chars": retry_params.get("repeat_window_chars", 1024),
        "single_char_repeat_count": retry_params.get("single_char_repeat_count", 100),
        "retry_transient_invoke_errors": retry_params.get("retry_transient_invoke_errors", True),
        "notify_user_on_retry": retry_params.get("notify_user_on_retry", True),
        "notify_user_on_exhausted": retry_params.get("notify_user_on_exhausted", True),
    }
    if "backoff_seconds" in retry_params:
        rail_kwargs["backoff_seconds"] = retry_params["backoff_seconds"]
    return TeamMemberNotifyingLLMRetryRail(**rail_kwargs)

__all__ = [
    "RESPONSE_PROMPT",
    "STREAM_EVENT",
    "AVATAR_PROMPT",
    "TEAM_MEMBER_LLM_RETRY",
]
