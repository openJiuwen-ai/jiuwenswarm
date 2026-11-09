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

The ``ast`` work runs in a throwaway interpreter. On the CI runners
(Python 3.11.5, pytest-xdist) an in-process ``ast.parse`` of this rail
file intermittently dies with ``SystemError: AST constructor recursion
depth mismatch (before=141, after=157/158)`` even though the file's
brace nesting depth is 3 — the AST constructor's recursion counter is
process-wide and can be left corrupted by earlier parses in the same
worker, so the failure depends on test order, not on this file. A fresh
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
    / "progressive_tool_rail.py"
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
    assigns, false_dump = _find_class_assignments("ProgressiveToolRail")

    assert "inherit_to_subagents" in assigns, (
        "ProgressiveToolRail must declare inherit_to_subagents = False; "
        "removing the flag silently re-exposes the subagent-rebind bug "
        "(child init rebinds _deep_agent → deferred cache loses "
        "subagent_wait → parent invoke_tool reports 未注册)"
    )
    assert assigns["inherit_to_subagents"] == false_dump, (
        "ProgressiveToolRail.inherit_to_subagents must be the literal False"
    )
