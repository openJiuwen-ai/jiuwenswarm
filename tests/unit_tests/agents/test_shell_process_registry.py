# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from subprocess import Popen

import pytest

from openjiuwen.core.sys_operation.shell_process_registry import (
    kill_shell_processes_for_session,
    kill_shell_processes_for_session_tree,
    register_shell_process,
    SHELL_PROCESS_REGISTRY,
)
from jiuwenswarm.agents.harness.common.tools import command_tools


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


def test_kill_sync_command_process(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    registered = threading.Event()
    kill_finished = threading.Event()

    def _register(session_id: str, proc: Popen[str]) -> None:
        register_shell_process(session_id, proc)
        registered.set()
        # Keep the command's cancellation loop from terminating the process first.
        kill_finished.wait()

    monkeypatch.setattr(command_tools, "register_shell_process", _register)
    monkeypatch.setenv("JW_START_NEW_SESSION", "true")
    with ThreadPoolExecutor(max_workers=1) as executor:
        command = executor.submit(
            command_tools._run_command_sync, "sleep 30", 5, tmp_path, "bash", session_id="sess_sync"
        )
        try:
            assert registered.wait(timeout=5), "The command process was not registered"
            assert kill_shell_processes_for_session("sess_sync") == 1
        finally:
            kill_finished.set()
        with pytest.raises(command_tools.CommandCancelled):
            command.result(timeout=5)


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
