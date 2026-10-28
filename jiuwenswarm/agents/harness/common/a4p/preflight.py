# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Conservative authorization preflight against the installed session rail."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from weakref import WeakValueDictionary

logger = logging.getLogger(__name__)

_SESSION_RAILS: WeakValueDictionary[str, Any] = WeakValueDictionary()
_PATTERN_CHARS = frozenset("*?[")


def register_preflight_rail(session_id: str, rail: Any) -> None:
    """Keep a non-owning reference to the manual rail used for execution."""
    _SESSION_RAILS[session_id] = rail


def unregister_preflight_rail(session_id: str) -> None:
    """Discard a previous rail when the session's permission mode changes."""
    _SESSION_RAILS.pop(session_id, None)


@dataclass(frozen=True)
class _ActionInvocation:
    tool_name: str
    tool_args: dict[str, Any]
    ctx: Any = None
    tool_call: Any = None


def is_concrete_action(action: dict[str, Any]) -> bool:
    """Never evaluate an authorization pattern as a literal invocation."""
    def contains_pattern(value: Any) -> bool:
        if isinstance(value, str):
            return any(char in value for char in _PATTERN_CHARS)
        if isinstance(value, dict):
            return any(contains_pattern(item) for item in value.values())
        if isinstance(value, list):
            return any(contains_pattern(item) for item in value)
        return False

    return not contains_pattern(action["params"])


async def evaluate_action(session_id: str, action: dict[str, Any]) -> str:
    """Return allow/ask/deny, or unknown when reliable preflight is unavailable."""
    rail = _SESSION_RAILS.get(session_id)
    if rail is None or not is_concrete_action(action):
        return "unknown"
    try:
        from jiuwenswarm.agents.harness.common.rails.permissions.policy_eval import (
            OpenJiuwenPolicyEvaluator,
        )

        snapshot = rail.get_permission_snapshot(session_id=session_id)
        evaluator = OpenJiuwenPolicyEvaluator(
            rail,
            permission_config_getter=lambda: snapshot,
        )
        result = await evaluator.evaluate(
            _ActionInvocation(tool_name=action["name"], tool_args=action["params"]),
            scene_approval_is_final=True,
        )
        # A failed refresh must not silently fall back to a different policy.
        if result.source == "fail_closed":
            return "unknown"
        return result.level
    except Exception:
        logger.warning("A4P permission preflight unavailable", exc_info=True)
        return "unknown"
