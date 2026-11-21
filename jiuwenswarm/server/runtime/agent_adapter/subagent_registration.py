# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compatibility planning for default sub-agent registration.

The provider builders remain responsible for constructing ``SubAgentConfig``
objects.  This module only decides whether the already-built objects use the
agent-core event bridge or the legacy static path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from openjiuwen.core.runner import Runner
from openjiuwen.harness.schema.config import SubAgentConfig


@dataclass(frozen=True)
class SubagentRegistrationPlan:
    """The static and event subsets for one parent DeepAgent."""

    mode: Literal["legacy", "event"]
    static_subagents: list[Any] | None
    event_subagents: list[SubAgentConfig]
    extension_bus: Any = None


def resolve_subagent_registration_plan(
    subagents: list[Any] | None,
    *,
    event_names: set[str] | frozenset[str],
) -> SubagentRegistrationPlan:
    """Split default configs when the complete event bridge is available.

    Capability detection happens before any event-specific keyword is passed to
    agent-core, so an older local checkout remains on the existing path without
    importing or constructing unsupported event payloads.
    """
    configured = list(subagents) if subagents is not None else None
    try:
        from jiuwenswarm.agents.swarm.registry import (
            supports_subagent_registration_event,
        )

        extension_bus = Runner.get_extension_bus()
        supported = supports_subagent_registration_event()
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
        extension_bus = None
        supported = False

    if not supported or extension_bus is None:
        return SubagentRegistrationPlan("legacy", configured, [])

    if configured is None:
        return SubagentRegistrationPlan("event", None, [], extension_bus)

    event_subagents = []
    for item in configured:
        if not isinstance(item, SubAgentConfig):
            continue
        if item.agent_card.name in event_names:
            event_subagents.append(item)
    # Keep event mode selected even when this startup has no event-managed
    # defaults.  A later turn must still use the same event-capable adapter
    # for ``ops.agent.register`` instead of silently reverting to legacy mode.

    event_ids = {id(item) for item in event_subagents}
    static_subagents = [item for item in configured if id(item) not in event_ids]
    return SubagentRegistrationPlan(
        "event",
        static_subagents,
        event_subagents,
        extension_bus,
    )


__all__ = ["SubagentRegistrationPlan", "resolve_subagent_registration_plan"]
