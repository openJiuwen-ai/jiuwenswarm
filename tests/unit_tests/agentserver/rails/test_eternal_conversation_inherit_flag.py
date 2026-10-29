# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression guard for ``EternalConversationRail.inherit_to_subagents = False``.

EternalConversationRail is a stateful session-level Rail: ``init`` binds
``_agent`` / ``system_prompt_builder`` and ``configure_runtime`` binds
``_session_id`` / ``_coordinator``. A general-purpose subagent inherits parent
rails by reference (``factory._inject_general_purpose_subagent``); the child's
``init_rail`` would then rebind the shared instance, pollute the session
evidence chain with subagent tasks and silently break the parent's memory
section injection. ``inherit_to_subagents = False`` opts the rail out at
factory injection, matching TaskExecutionRail / ProgressiveToolRail /
CodingArtifactPostProcessRail.

This test locks the swarm-side half: the flag value as declared in source.
It reads the module via ``ast`` — NOT importing it — so it runs in CI
gates where the openjiuwen dependency may be unavailable.

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
    / "eternal_conversation"
    / "rail.py"
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
    """Parse in a child interpreter; return ({attr_name: dump}, literal-False dump)."""
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
    assigns, false_dump = _find_class_assignments("EternalConversationRail")

    assert "inherit_to_subagents" in assigns, (
        "EternalConversationRail must declare inherit_to_subagents = False; "
        "removing the flag lets factory._inject_general_purpose_subagent copy "
        "the rail by reference into general-purpose subagents, whose "
        "init_rail rebinds _agent/_builder and pollutes the session "
        "evidence chain (rail/base.py inheritance trap)"
    )
    assert assigns["inherit_to_subagents"] == false_dump, (
        "EternalConversationRail.inherit_to_subagents must be the literal False"
    )
