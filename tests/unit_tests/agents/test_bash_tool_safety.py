# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from types import SimpleNamespace

import psutil
import pytest

from jiuwenswarm.agents.harness.common.tools import bash_tool_safety, command_tools
from jiuwenswarm.agents.harness.common.tools.bash_tool_safety import (
    _pre_execute_shell_command,
    install_shell_tool_safety_hooks,
    reset_installed_flag,
)


@pytest.fixture(autouse=True)
def _reset_install_flag():
    reset_installed_flag()
    yield
    reset_installed_flag()


def test_pre_execute_blocks_pkill_on_jiuwenswarm_tui() -> None:
    err = _pre_execute_shell_command('pkill -f "jiuwenswarm-tui" 2>/dev/null')
    assert err is not None
    assert "rejected for safety" in err


def test_pre_execute_allows_unrelated_ps() -> None:
    err = _pre_execute_shell_command("ps aux | grep node | head -5")
    assert err is None


def test_install_wraps_bash_tool_invoke() -> None:
    from openjiuwen.harness.tools.shell.bash._tool import BashTool

    install_shell_tool_safety_hooks()
    assert getattr(BashTool.invoke, "jiuwenswarm_safety_wrapped", False)
    install_shell_tool_safety_hooks()
    assert getattr(BashTool.invoke, "jiuwenswarm_safety_wrapped", False)


def test_pre_execute_blocks_killing_python_by_name() -> None:
    err = _pre_execute_shell_command("taskkill /F /IM python.exe /T")
    assert err is not None
    assert "rejected for safety" in err


# ── Windows: shell 超时/中断回收整棵进程树 ────────────────────────


@pytest.fixture
def kill_paths(monkeypatch):
    """Stand-in originals for both openjiuwen kill paths, restored after the test."""
    from openjiuwen.core.sys_operation import shell_process_registry
    from openjiuwen.core.sys_operation.local.utils import AsyncProcessHandler

    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        AsyncProcessHandler,
        "_kill_process_tree",
        lambda self: calls.append(("kill_tree", self._process.pid)),
    )
    monkeypatch.setattr(
        shell_process_registry,
        "terminate_shell_process",
        lambda proc: calls.append(("terminate", proc.pid)) or True,
    )
    monkeypatch.setattr(
        command_tools, "terminate_shell_process", shell_process_registry.terminate_shell_process
    )
    return SimpleNamespace(
        calls=calls, handler_cls=AsyncProcessHandler, registry=shell_process_registry
    )


@pytest.fixture
def fake_process_tree(monkeypatch) -> list[int]:
    """Root pid 123 with descendants 124 and 125; returns the pids killed."""
    killed: list[int] = []

    class _Descendant:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def kill(self) -> None:
            killed.append(self.pid)

    class _Root:
        def __init__(self, pid: int) -> None:
            if pid != 123:
                raise psutil.NoSuchProcess(pid)

        @staticmethod
        def children(recursive: bool = False) -> list[_Descendant]:
            assert recursive
            return [_Descendant(124), _Descendant(125)]

    monkeypatch.setattr(psutil, "Process", _Root)
    return killed


def test_windows_timeout_kills_the_whole_tree(monkeypatch, kill_paths, fake_process_tree) -> None:
    monkeypatch.setattr(bash_tool_safety, "os", SimpleNamespace(name="nt"))
    install_shell_tool_safety_hooks()
    reset_installed_flag()
    install_shell_tool_safety_hooks()  # a second install must not wrap twice

    kill_paths.handler_cls._kill_process_tree(SimpleNamespace(_process=SimpleNamespace(pid=123)))

    assert fake_process_tree == [125, 124]
    assert kill_paths.calls == [("kill_tree", 123)]


def test_windows_interrupt_kills_the_whole_tree(monkeypatch, kill_paths, fake_process_tree) -> None:
    monkeypatch.setattr(bash_tool_safety, "os", SimpleNamespace(name="nt"))
    install_shell_tool_safety_hooks()

    assert command_tools.terminate_shell_process is kill_paths.registry.terminate_shell_process
    assert command_tools.terminate_shell_process(SimpleNamespace(pid=123, returncode=None)) is True
    assert fake_process_tree == [125, 124]

    # An exited process may have a recycled PID: leave its "descendants" alone.
    command_tools.terminate_shell_process(SimpleNamespace(pid=123, returncode=0))
    assert fake_process_tree == [125, 124]
    assert kill_paths.calls == [("terminate", 123), ("terminate", 123)]


def test_windows_timeout_skips_descendants_of_an_exited_process(
    monkeypatch, kill_paths, fake_process_tree
) -> None:
    monkeypatch.setattr(bash_tool_safety, "os", SimpleNamespace(name="nt"))
    install_shell_tool_safety_hooks()

    # returncode already observed: the PID may have been recycled, so the
    # "descendants" of 123 might belong to a stranger.
    kill_paths.handler_cls._kill_process_tree(
        SimpleNamespace(_process=SimpleNamespace(pid=123, returncode=0))
    )

    assert fake_process_tree == []
    assert kill_paths.calls == [("kill_tree", 123)]


def test_windows_interrupt_polls_before_trusting_returncode(
    monkeypatch, kill_paths, fake_process_tree
) -> None:
    monkeypatch.setattr(bash_tool_safety, "os", SimpleNamespace(name="nt"))
    install_shell_tool_safety_hooks()

    polled: list[int] = []

    class _PopenStyle:
        pid = 123
        returncode = None  # not observed yet...

        def poll(self):
            polled.append(self.pid)
            self.returncode = 0  # ...but polling reveals it already exited
            return self.returncode

    assert command_tools.terminate_shell_process(_PopenStyle()) is True

    assert polled == [123]
    assert fake_process_tree == []
    assert kill_paths.calls == [("terminate", 123)]


def test_posix_keeps_process_group_kill(monkeypatch, kill_paths) -> None:
    original = kill_paths.handler_cls._kill_process_tree
    monkeypatch.setattr(bash_tool_safety, "os", SimpleNamespace(name="posix"))

    install_shell_tool_safety_hooks()

    assert kill_paths.handler_cls._kill_process_tree is original
