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

The ``ast`` work runs in a throwaway interpreter. On the CI runners
(Python 3.11.5, pytest-xdist) an in-process ``ast.parse`` of rail files
intermittently dies with ``SystemError: AST constructor recursion depth
mismatch`` — the AST constructor's recursion counter is process-wide and
can be left corrupted by earlier parses in the same worker. A fresh
interpreter starts with clean state and cannot inherit that corruption.
"""

from __future__ import annotations

import json
import subprocess
import sys
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

# Child interpreter script: read and parse the rail file, dump the top-level
# assignments of the requested class, and emit the reference dump of the
# literal False. The file is read (and only read) inside the child so the
# source never crosses a locale-dependent stdio pipe.
_CHILD_SCRIPT = """\
import ast, json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    source = fh.read()
tree = ast.parse(source, filename=sys.argv[1])
assigns = {}
for node in ast.walk(tree):
    if isinstance(node, ast.ClassDef) and node.name == sys.argv[2]:
        for stmt in node.body:
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
            ):
                assigns[stmt.targets[0].id] = ast.dump(stmt.value)
        break
false_dump = ast.dump(ast.parse("False", mode="eval").body)
print(json.dumps({"assigns": assigns, "false_dump": false_dump}))
"""


def _find_class_assignments(class_name: str) -> tuple[dict[str, str], str]:
    """Parse in a child interpreter; return ({attr_name: dump}, literal-False dump).

    See the module docstring: an in-process ``ast.parse`` is unreliable on
    CPython 3.11 CI workers once earlier tests corrupted its AST recursion
    counter. The child only needs stdlib, so this stays valid in CI gates
    without the openjiuwen dependency.
    """
    proc = subprocess.run(
        [sys.executable, "-I", "-B", "-c", _CHILD_SCRIPT, str(RAIL_PATH), class_name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AssertionError(
            f"child ast.parse of {RAIL_PATH} failed (rc={proc.returncode}):\n{proc.stderr}"
        )
    payload = json.loads(proc.stdout)
    return payload["assigns"], payload["false_dump"]


def test_inherit_to_subagents_is_false():
    """Flag must stay False — guards against accidental deletion/flipping."""
    assert RAIL_PATH.is_file(), f"rail source not found: {RAIL_PATH}"
    assigns, false_dump = _find_class_assignments("ConcurrentSafeTaskPlanningRail")

    assert "inherit_to_subagents" in assigns, (
        "ConcurrentSafeTaskPlanningRail must declare inherit_to_subagents = False; "
        "removing the flag silently re-exposes the subagent-rebind bug "
        "(child init rebinds _state_agent/self.tools → parent before_invoke resets "
        "the wrong instance's _created_in_invoke → todo_create rejected every "
        "round → task lists accumulate and leak across rounds)"
    )
    assert assigns["inherit_to_subagents"] == false_dump, (
        "ConcurrentSafeTaskPlanningRail.inherit_to_subagents must be the literal False"
    )
