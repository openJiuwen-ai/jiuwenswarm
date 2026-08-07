# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Guards around command execution, from the 2026-08-02 Git OOM incident.

A single `git log` with a non-ASCII `--date=format:` reached roughly 8.5 GB
working set / 49 GB private memory per invocation on Git for Windows, and the
agent ran it nine times in two and a half minutes. Two things this pins:

  - the failures the tool reported were invisible to the execution guards,
    because several paths returned a bare string rather than a payload
  - terminating a command reached the shell wrapper and not the process doing
    the work, on the platform where the runaway allocation was happening
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.execution_guard.circuit_breaker_rail import (
    ToolResultErrorDetector,
)
from jiuwenswarm.agents.harness.common.tools import command_tools
from jiuwenswarm.agents.harness.common.tools.command_tools import _failure_payload


# ---------------------------------------------------------------------------
# Failures the guards can see
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "[ERROR]: command timed out after 300s.",
        "[ERROR]: command rejected for safety (blocked pattern).",
        "[ERROR]: command cannot be empty.",
    ],
)
def test_failure_payloads_are_visible_to_the_execution_guards(message: str) -> None:
    """Bare `[ERROR]:` strings read as neither success nor error.

    `ToolResultErrorDetector` could not classify them, so a command that timed
    out or was blocked counted toward no streak at all -- the loops the guards
    exist to catch were invisible to them.
    """
    payload = _failure_payload("git log -1", message)

    assert ToolResultErrorDetector.has_error(payload) is True
    assert ToolResultErrorDetector.has_explicit_success(payload) is False
    assert message in json.loads(payload)["stderr"]


def test_a_bare_error_string_is_now_visible_but_carries_no_exit_code() -> None:
    """Upstream #4294 taught the detector the bare ``[ERROR]:`` prefix.

    That closes the blind spot on its own, so this no longer pins a defect.
    What the structured payload still adds is the documented JSON contract:
    a bare string has no ``exit_code`` for anything downstream to key on.
    """
    assert ToolResultErrorDetector.has_error("[ERROR]: command timed out") is True
    bare = ToolResultErrorDetector._normalize("[ERROR]: command timed out")
    structured = json.loads(_failure_payload("x", "[ERROR]: command timed out"))
    assert "exit_code" not in bare
    assert structured["exit_code"] == -1


# ---------------------------------------------------------------------------
# Killing the tree, not just the child
# ---------------------------------------------------------------------------


def _process_is_dead(pid: int) -> bool:
    """True when *pid* is gone or only a zombie remains."""
    import psutil

    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shell")
async def test_a_grandchild_does_not_outlive_the_command() -> None:
    """The process doing the work is usually not the one we spawned.

    `mcp_exec_command` starts a shell, and the shell starts the real command, so
    the process that allocates is a grandchild. `terminate_shell_process` reaches
    the whole group via `killpg` on POSIX but falls back to `proc.terminate()` on
    Windows, which is the shell alone -- so on the platform the incident happened
    on, a timeout would have reported a killed command while the process holding
    the memory kept running.

    This pins the outcome that matters on every platform: once the tool has
    returned, nothing it started is still alive.
    """
    import psutil

    marker = Path(tempfile.gettempdir()) / f"jw-guard-{os.getpid()}.pid"
    marker.unlink(missing_ok=True)
    # Backgrounded inside the shell, so it is a grandchild that outlives its
    # parent unless something reaps the tree.
    script = (
        f"{sys.executable} -c \""
        "import os, time, pathlib; "
        f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); "
        "time.sleep(120)\" & sleep 120"
    )
    pid: int | None = None
    try:
        await command_tools.mcp_exec_command.invoke(
            {"command": script, "timeout_seconds": 2}
        )

        assert marker.exists(), "the grandchild never started; the test proves nothing"
        pid = int(marker.read_text().strip())

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not _process_is_dead(pid):
            await asyncio.sleep(0.05)

        assert _process_is_dead(pid), (
            f"grandchild {pid} survived the command that started it"
        )
    finally:
        if pid is not None:
            with contextlib.suppress(Exception):
                import psutil

                psutil.Process(pid).kill()
        marker.unlink(missing_ok=True)


def test_the_tree_reaper_does_not_rely_on_the_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Isolates the part of the fix that only matters off POSIX.

    The test above passes on Linux with or without this change, because `killpg`
    already reaps the group -- so it guards the outcome but cannot exercise the
    Windows path, where `terminate_shell_process` reaches the direct child alone.

    This neutralises the helper and checks that `_terminate_command_tree` reaps
    descendants on its own, which is what has to be true for a timeout or a cancel
    to contain anything on Windows rather than merely report it.
    """
    import psutil

    parent = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess, sys, time;"
         "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']);"
         "time.sleep(120)"],
    )
    try:
        deadline = time.monotonic() + 5
        children: list = []
        while time.monotonic() < deadline and not children:
            children = psutil.Process(parent.pid).children(recursive=True)
            time.sleep(0.05)
        assert children, "no descendant was created; the test proves nothing"
        child_pids = [c.pid for c in children]

        def dead(pid: int) -> bool:
            # A killed child whose parent has not reaped it is a zombie, and a
            # zombie's pid still "exists" -- so pid_exists alone would report a
            # successful kill as a survivor.
            try:
                return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                return True

        monkeypatch.setattr(command_tools, "terminate_shell_process", lambda _p: True)
        command_tools._terminate_command_tree(parent)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not all(map(dead, child_pids)):
            time.sleep(0.05)
        assert all(map(dead, child_pids)), (
            f"descendants {child_pids} survived with the group kill disabled"
        )
    finally:
        with contextlib.suppress(Exception):
            for child in psutil.Process(parent.pid).children(recursive=True):
                child.kill()
        parent.kill()
        parent.wait(timeout=5)


# ---------------------------------------------------------------------------
# The tool the incident actually used
# ---------------------------------------------------------------------------


def test_the_bash_path_reaps_descendants_too() -> None:
    """`bash`, not `mcp_exec_command`, is the tool the incident ran through.

    `bash_tool_safety` says so in its own module docstring, and the incident's
    recorded tool_call name is `bash`. Its teardown goes through agent-core's
    public ``OperationUtils.create_handler`` factory, whose default handler
    has the same shape as ``terminate_shell_process``: ``killpg`` on POSIX,
    ``self._process.kill()`` on Windows -- the shell wrapper alone.

    So the reaper has to be wired into that path as well, or the fix covers
    every shell tool except the one that mattered.
    """
    from openjiuwen.core.sys_operation.local.utils import OperationUtils

    from jiuwenswarm.agents.harness.common.tools import bash_tool_safety

    original_create = OperationUtils.create_handler
    try:
        bash_tool_safety.reset_installed_flag()
        bash_tool_safety.install_shell_tool_safety_hooks()

        assert getattr(OperationUtils.create_handler, "jiuwenswarm_safety_wrapped", False), (
            "bash's teardown factory is unpatched; the incident's tool is uncovered"
        )
        assert bash_tool_safety._safe_handler_cls is not None

        handler = OperationUtils.create_handler(SimpleNamespace(pid=None))
        assert isinstance(handler, bash_tool_safety._safe_handler_cls)

        # Order matters: collect before the parent dies, kill snapshot, then parent,
        # then retry.
        calls: list[str] = []

        import jiuwenswarm.agents.harness.common.tools.command_tools as ct

        real_collect = ct.collect_process_descendant_pids
        real_kill = ct.kill_process_pids
        ct.collect_process_descendant_pids = lambda pid: (calls.append("collect"), [])[1]
        ct.kill_process_pids = lambda pids: calls.append("kill")
        try:
            handler._kill_process_tree()  # pylint: disable=protected-access
        finally:
            ct.collect_process_descendant_pids = real_collect
            ct.kill_process_pids = real_kill

        assert calls == ["collect", "kill", "kill", "collect", "kill"], (
            "the bash path did not reap descendants"
        )
    finally:
        OperationUtils.create_handler = original_create
        bash_tool_safety.reset_installed_flag()
