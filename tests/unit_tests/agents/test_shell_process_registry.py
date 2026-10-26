# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from openjiuwen.core.sys_operation.shell_process_registry import (
    kill_shell_processes_for_session,
    kill_shell_processes_for_session_tree,
    register_shell_process,
    SHELL_PROCESS_REGISTRY,
)
from jiuwenswarm.agents.harness.common.tools.command_tools import (
    CommandCancelled,
    _run_command_sync,
)


@pytest.mark.asyncio
async def test_kill_asyncio_shell_process() -> None:
    proc = await asyncio.create_subprocess_exec(
        "sleep",
        "30",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    SHELL_PROCESS_REGISTRY.register("sess_async", proc)
    await asyncio.sleep(0.05)
    killed = kill_shell_processes_for_session("sess_async")
    assert killed == 1
    await asyncio.wait_for(proc.wait(), timeout=3)
    assert proc.returncode is not None


def test_kill_sync_command_process() -> None:
    def _run() -> None:
        try:
            _run_command_sync("sleep 30", 60, Path("."), "bash", session_id="sess_sync")
        except CommandCancelled:
            return

    thread = threading.Thread(target=_run)
    thread.start()
    time.sleep(0.2)
    killed = kill_shell_processes_for_session("sess_sync")
    assert killed == 1
    thread.join(timeout=5)
    assert not thread.is_alive()


@pytest.mark.asyncio
async def test_kill_shell_processes_for_session_tree() -> None:
    main_proc = await asyncio.create_subprocess_exec(
        "sleep",
        "30",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    sub_proc = await asyncio.create_subprocess_exec(
        "sleep",
        "30",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    SHELL_PROCESS_REGISTRY.register("sess_main", main_proc)
    SHELL_PROCESS_REGISTRY.register("sess_main_sub_explore", sub_proc)
    await asyncio.sleep(0.05)

    killed = kill_shell_processes_for_session_tree("sess_main")
    assert killed == 2
    await asyncio.wait_for(main_proc.wait(), timeout=3)
    await asyncio.wait_for(sub_proc.wait(), timeout=3)
