# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Read the current executable binding without making permission decisions."""

from openjiuwen.core.runner import Runner


def matches_bound_method(method: object, *, expected_owner: object, expected_func: object) -> bool:
    """Compare both binding identities supplied by the trusted caller."""
    return (
        expected_owner is not None
        and expected_func is not None
        and getattr(method, "__self__", None) is expected_owner
        and getattr(method, "__func__", None) is expected_func
    )


def resolve_tool_binding(agent, tool_name: str, expected_type: type):
    """Return the exact registered implementation; callers own lookup failures."""
    card = agent.ability_manager.get(tool_name)
    resource = Runner.resource_mgr.get_tool(card.id, session=None) if card else None
    # Subclasses can replace execution; this is implementation identity, not assignability.
    exact_type = type(resource) is expected_type  # pylint: disable=huawei-unidiomatic-typecheck
    if exact_type and resource.card is card:
        return resource
    return None
