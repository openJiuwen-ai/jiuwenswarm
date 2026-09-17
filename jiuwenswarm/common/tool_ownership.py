# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Single source of truth for how a tool instance is registered process-wide.

``Runner.resource_mgr`` is a process-global registry, so every tool needs an
answer to one question: is this instance owned by one agent, or shared by all of
them? openjiuwen answers it with ``ToolCard.stateless`` (see
``AbilityManager.add_ability``), and this module keeps the JiuWenSwarm hosts on
exactly that judgement instead of a parallel one:

- ``stateless`` False (the default): agent-owned. The id is qualified with the
  owner so concurrent agents never overwrite each other's instance, and
  ``AbilityManager.teardown_tools`` can reclaim it on cleanup.
- ``stateless`` True: shared. The bare id is kept and a repeated registration is
  an idempotent no-op, so the first registered instance is the one everybody
  runs.

Hosts that register tools before a DeepAgent exists (``_get_tool_cards`` runs
ahead of ``create_deep_agent``) have no AbilityManager to route through yet;
:func:`register_tool` applies the same contract so the two paths cannot drift.
"""

from __future__ import annotations

from typing import Any

from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.runner import Runner

# Ownership is read off ``ToolCard.stateless``; a card that predates the flag
# (or a test double standing in for one) is treated as agent-owned, the safe
# default — a shared instance registered per agent costs an extra object, while
# an agent-owned instance registered as shared pins one session's state process-wide.
_DEFAULT_STATELESS = False


def mark_stateless(tools: list[Any]) -> list[Any]:
    """Declare tool instances as shared across agents.

    "Stateless" here is openjiuwen's operational definition — shared across
    agents under the bare card id — not a claim that the instance holds no
    fields. A toolkit bound to a process-wide manager qualifies: what matters is
    that one instance serves every agent and must not be id-qualified per agent
    (which would corrupt the shared card) nor torn down with any single one.

    Args:
        tools: Tool instances to flag.

    Returns:
        The same list, so callers can wrap a build expression inline.
    """
    for tool in tools:
        card = getattr(tool, "card", None)
        if card is not None:
            card.stateless = True
    return tools


def qualify_tool_id(card: ToolCard, owner_id: str) -> str:
    """Return the agent-qualified registration id for an owned tool.

    Derived from ``card.name`` rather than ``card.id`` so re-registering an
    already-qualified card is idempotent.

    Args:
        card: Card of the tool being registered.
        owner_id: Id of the agent owning this instance.

    Returns:
        The qualified id, ``"{name}_{owner_id}"``.
    """
    return f"{card.name}_{owner_id}"


def register_tool(tool: Any, owner_id: str | None) -> None:
    """Register one tool instance under the ownership its card declares.

    Args:
        tool: Tool instance to register.
        owner_id: Owner of this instance, or None when the caller has no agent
            scope to attribute it to. A missing owner degrades to the shared
            contract, matching ``AbilityManager`` with no owner id set.
    """
    card = tool.card
    if getattr(card, "stateless", _DEFAULT_STATELESS) or not owner_id:
        Runner.resource_mgr.add_tool(tool, skip_if_exists=True)
        return
    card.id = qualify_tool_id(card, owner_id)
    Runner.resource_mgr.add_tool(tool, refresh=True)


def unregister_tool(tool: Any) -> None:
    """Drop an owned tool's registration, leaving shared instances in place.

    Mirrors ``AbilityManager.remove_ability``: a shared instance may still back
    other agents, so only the owning-agent registration is removable.

    Args:
        tool: Tool instance whose registration should be dropped.
    """
    card = tool.card
    if getattr(card, "stateless", _DEFAULT_STATELESS):
        return
    Runner.resource_mgr.remove_tool(card.id)


__all__ = [
    "mark_stateless",
    "qualify_tool_id",
    "register_tool",
    "unregister_tool",
]
