# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression guard for ``TaskExecutionRail.inherit_to_subagents = False``.

TaskExecutionRail binds parent-specific state in ``init`` (``self._deep_agent
= agent``) and resolves todo.json against that agent's workspace. When a
general-purpose subagent inherits the SAME instance by reference, its
``_ensure_initialized`` re-runs ``init`` against the child, rebinding
``_deep_agent`` to the subagent. The parent then resolves todo.json under the
child's empty ``sub_agents/.../todo/`` directory, reads an empty list, and
stops emitting ``task.start``/``task.complete`` for every later stage (e.g.
PPT stage4+ missing from history.json after a general-purpose subagent ran).

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

import pytest

ROOT = Path(__file__).resolve().parents[4]
RAIL_PATH = (
    ROOT
    / "jiuwenswarm"
    / "agents"
    / "harness"
    / "common"
    / "rails"
    / "task_execution_rail.py"
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
    assigns = _find_class_assignments(source, "TaskExecutionRail")

    assert "inherit_to_subagents" in assigns, (
        "TaskExecutionRail must declare inherit_to_subagents = False; "
        "removing the flag silently re-exposes the subagent-rebind bug "
        "(child init rebinds _deep_agent → parent todo.json loads empty → "
        "task.start stops firing for later stages)"
    )
    value = assigns["inherit_to_subagents"]
    assert ast.dump(value) == ast.dump(
        ast.parse("False", mode="eval").body
    ), "TaskExecutionRail.inherit_to_subagents must be the literal False"
