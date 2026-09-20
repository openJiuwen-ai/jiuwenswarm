from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.tools import command_tools
from jiuwenswarm.agents.harness.common.tools.command_execution_context import (
    bind_command_execution,
    current_command_execution,
    reset_command_execution,
)


class _Shell:
    def __init__(self, label: str) -> None:
        self.label = label
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_cmd(self, command: str, **kwargs: object) -> object:
        self.calls.append((command, kwargs))
        return SimpleNamespace(
            code=0,
            message="ok",
            data=SimpleNamespace(
                exit_code=0,
                stdout=f"{self.label}-stdout",
                stderr="",
            ),
        )

    async def execute_cmd_background(self, command: str, **kwargs: object) -> object:
        self.calls.append((command, kwargs))
        return SimpleNamespace(
            code=0,
            message="ok",
            data=SimpleNamespace(pid=42),
        )


class _SysOperation:
    def __init__(self, label: str, *, extra_params: dict | None = None) -> None:
        self._shell = _Shell(label)
        self._run_config = SimpleNamespace(
            config=SimpleNamespace(
                launcher_config=SimpleNamespace(extra_params=extra_params or {})
            )
        )

    def shell(self) -> _Shell:
        return self._shell


async def _invoke(**kwargs: object) -> str:
    return await command_tools.mcp_exec_command._func(**kwargs)


@pytest.fixture(autouse=True)
def _bind_test_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(command_tools, "_context_cwd", lambda: tmp_path)
    monkeypatch.setattr(command_tools, "_context_project_root", lambda: tmp_path)


@pytest.mark.asyncio
async def test_bound_sandbox_foreground_never_uses_host_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        command_tools,
        "_run_command_sync",
        lambda *_args, **_kwargs: pytest.fail("host subprocess reached"),
    )
    sys_operation = _SysOperation("A")
    token = bind_command_execution(sys_operation, sandboxed=True)
    try:
        payload = json.loads(
            await _invoke(
                command="printf ok",
                timeout_seconds=17,
                workdir=".",
                max_output_chars=0,
                shell_type="bash",
                background=False,
            )
        )
    finally:
        reset_command_execution(token)

    assert payload["stdout"] == "A-stdout"
    assert payload["resolved_shell"] == "bash"
    assert sys_operation._shell.calls == [
        (
            "printf ok",
            {
                "cwd": str(tmp_path),
                "timeout": 17,
                "shell_type": "bash",
            },
        )
    ]
    assert current_command_execution() is None


@pytest.mark.asyncio
async def test_explicit_workspace_root_workdir_ignores_context_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    nested_cwd = tmp_path / "nested"
    nested_cwd.mkdir()
    monkeypatch.setattr(command_tools, "_context_cwd", lambda: nested_cwd)
    sys_operation = _SysOperation("A")
    token = bind_command_execution(sys_operation, sandboxed=True)
    try:
        await _invoke(command="pwd", workdir=str(tmp_path))
    finally:
        reset_command_execution(token)

    assert sys_operation._shell.calls == [
        (
            "pwd",
            {
                "cwd": str(tmp_path),
                "timeout": 300,
                "shell_type": "auto",
            },
        )
    ]


@pytest.mark.asyncio
async def test_bound_sandbox_background_never_uses_host_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        command_tools,
        "_run_command_background",
        lambda *_args, **_kwargs: pytest.fail("host subprocess reached"),
    )
    sys_operation = _SysOperation("A")
    token = bind_command_execution(sys_operation, sandboxed=True)
    try:
        payload = json.loads(await _invoke(command="server", background=True))
    finally:
        reset_command_execution(token)

    assert payload["pid"] == 42
    assert payload["status"] == "started"


@pytest.mark.asyncio
async def test_bound_sandbox_rejects_workdir_escape_before_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    sys_operation = _SysOperation("A")
    token = bind_command_execution(sys_operation, sandboxed=True)
    try:
        result = await _invoke(command="pwd", workdir="../outside")
    finally:
        reset_command_execution(token)

    assert result == "[ERROR]: workdir is outside project workspace."
    assert sys_operation._shell.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra_params",
    [
        {"fallback_on_failure": True},
        {"excluded_commands": ["pip*"]},
    ],
)
async def test_bound_sandbox_rejects_provider_host_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    extra_params: dict,
) -> None:
    sys_operation = _SysOperation("A", extra_params=extra_params)
    token = bind_command_execution(sys_operation, sandboxed=True)
    try:
        result = await _invoke(command="printf ok")
    finally:
        reset_command_execution(token)

    assert "failed closed" in result
    assert sys_operation._shell.calls == []


@pytest.mark.asyncio
async def test_concurrent_bindings_keep_sys_operations_isolated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    first = _SysOperation("A")
    second = _SysOperation("B")

    async def run(sys_operation: _SysOperation) -> str:
        token = bind_command_execution(sys_operation, sandboxed=True)
        try:
            await asyncio.sleep(0)
            return json.loads(await _invoke(command="printf ok"))["stdout"]
        finally:
            reset_command_execution(token)

    assert await asyncio.gather(run(first), run(second)) == ["A-stdout", "B-stdout"]
    assert current_command_execution() is None
