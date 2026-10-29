# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression guard for ``ConcurrentSafeTaskPlanningRail.inherit_to_subagents = False``.

ConcurrentSafeTaskPlanningRail binds agent-specific state in ``init``
(``self._state_agent = agent``) and rebinds ``self.tools`` to the initializing
agent's todo tool instances. When a general-purpose subagent inherits the SAME
instance by reference, its ``_ensure_initialized`` re-runs ``init`` against the
child, rebinding ``self.tools`` / ``_state_agent`` to the subagent. The
parent's ``TaskPlanningRail.before_invoke`` then resets the
``_created_in_invoke`` marker on the WRONG tool instance, so the parent's
in-flight ``todo_create`` stays "already rebuilt" for every later round: each
new task list is rejected (or appended via ``todo_modify`` instead) and stale
task lists accumulate and leak across rounds in the UI.

``inherit_to_subagents = False`` opts the rail out at
``factory._inject_general_purpose_subagent`` (openjiuwen side, tested in
agent-core). This test locks the swarm-side half: the flag value as declared
in source. It reads the module via ``ast`` — NOT importing it — so it runs
in CI gates where the openjiuwen dependency may be unavailable.

Removing the flag (or flipping to True) silently re-exposes the rebind bug.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
RAIL_PATH = (
    ROOT
    / "jiuwenswarm"
    / "agents"
    / "harness"
    / "common"
    / "rails"
    / "concurrent_safe_rails.py"
)


def _find_class_assignments(source: str, class_name: str) -> dict[str, ast.AST]:
    """Return ``{attr_name: value_node}`` for top-level assigns in a class.

    Walks the module AST so the file is never imported (which would pull the
    openjiuwen dependency chain — unavailable in some CI gate sandboxes).
    """
    tree = ast.parse(source, filename=str(RAIL_PATH))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == class_name
        ):
            assigns: dict[str, ast.AST] = {}
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                ):
                    assigns[stmt.targets[0].id] = stmt.value
            return assigns
    return {}


def test_inherit_to_subagents_is_false():
    """Flag must stay False — guards against accidental deletion/flipping."""
    assert RAIL_PATH.is_file(), f"rail source not found: {RAIL_PATH}"
    source = RAIL_PATH.read_text(encoding="utf-8")
    assigns = _find_class_assignments(source, "ConcurrentSafeTaskPlanningRail")

    assert "inherit_to_subagents" in assigns, (
        "ConcurrentSafeTaskPlanningRail must declare inherit_to_subagents = False; "
        "removing the flag silently re-exposes the subagent-rebind bug "
        "(child init rebinds _state_agent/self.tools → parent before_invoke resets "
        "the wrong instance's _created_in_invoke → todo_create rejected every "
        "round → task lists accumulate and leak across rounds)"
    )
    value = assigns["inherit_to_subagents"]
    assert ast.dump(value) == ast.dump(
        ast.parse("False", mode="eval").body
    ), "ConcurrentSafeTaskPlanningRail.inherit_to_subagents must be the literal False"
