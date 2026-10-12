# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Entry hooks and agent_manager import must not load DeepAgent or BashTool."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_JIUWEN = Path(__file__).resolve().parents[3]
_AGENT_CORE = _JIUWEN.parent / "agent-core"


def _run(code: str) -> None:
    env = os.environ.copy()
    env.pop("EXTENSION_DIRS", None)
    previous = env.get("PYTHONPATH", "")
    prefix = os.pathsep.join((str(_AGENT_CORE), str(_JIUWEN)))
    env["PYTHONPATH"] = prefix + (os.pathsep + previous if previous else "")
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_JIUWEN,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_shell_hook_does_not_import_bash_until_used() -> None:
    _run(
        """
import sys
from jiuwenswarm.agents.harness.common.tools.bash_tool_safety import (
    register_shell_safety_import_hook,
)
register_shell_safety_import_hook()
assert "openjiuwen.harness.tools.shell.bash._tool" not in sys.modules
assert "pdfplumber" not in sys.modules
from openjiuwen.harness.tools.shell.bash._tool import BashTool
assert getattr(BashTool.invoke, "jiuwenswarm_safety_wrapped", False)
"""
    )


def test_agent_manager_does_not_import_deep_agent() -> None:
    _run(
        """
import sys
import jiuwenswarm.server.runtime.agent_manager  # noqa: F401
blocked = (
    "openjiuwen.harness.deep_agent",
    "jiuwenswarm.agents.harness.team.team_manager",
)
for name in blocked:
    assert name not in sys.modules, name
"""
    )
