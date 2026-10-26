# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression guard for ``ProgressiveToolRail.inherit_to_subagents = False``.

ProgressiveToolRail binds ``_deep_agent`` / ``_runtime_agent`` in ``init`` and
refreshes the deferred-tool cache from that agent's ability_manager. A
general-purpose subagent inherits parent rails by reference
(``factory._inject_general_purpose_subagent``). The child's ``init`` then
rebinds the shared instance and overwrites the cache with the child's
smaller tool set (no ``subagent_wait``). Parent ``tools_search`` /
``invoke_tool`` for deferred subagent tools then miss while the child is
still running.

``inherit_to_subagents = False`` opts the rail out at factory injection.
This test locks the swarm-side half: the flag value as declared in source.
It reads the module via ``ast`` — NOT importing it — so it runs in CI
gates where the openjiuwen dependency may be unavailable.

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
    / "progressive_tool_rail.py"
)


def _find_class_assignments(source: str, class_name: str) -> dict[str, ast.AST]:
    """Return ``{attr_name: value_node}`` for top-level assigns in a class."""
    tree = ast.parse(source, filename=str(RAIL_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
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
    assigns = _find_class_assignments(source, "ProgressiveToolRail")

    assert "inherit_to_subagents" in assigns, (
        "ProgressiveToolRail must declare inherit_to_subagents = False; "
        "removing the flag silently re-exposes the subagent-rebind bug "
        "(child init rebinds _deep_agent → deferred cache loses "
        "subagent_wait → parent invoke_tool reports 未注册)"
    )
    value = assigns["inherit_to_subagents"]
    assert ast.dump(value) == ast.dump(
        ast.parse("False", mode="eval").body
    ), "ProgressiveToolRail.inherit_to_subagents must be the literal False"
