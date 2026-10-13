# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio

import pytest

from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.models.sandbox import ExecResult, SandboxPhase, SandboxRef
from jiuwenbox.server.limits import SandboxLimits
from jiuwenbox.server.sandbox_manager import (
    CodePolicyDenialError,
    SandboxBackgroundExecRequest,
    SandboxExecRequest,
    SandboxManager,
)


class _Runtime:
    def __init__(self) -> None:
        self.exec_calls = 0

    async def exec(self, sandbox_id, request):
        self.exec_calls += 1
        return ExecResult(exit_code=0)

    async def list_background_jobs(self, sandbox_id):
        return []

    async def exec_background(self, sandbox_id, request):
        raise AssertionError("background exec should be denied before runtime")


def _manager() -> tuple[SandboxManager, _Runtime]:
    manager = SandboxManager.__new__(SandboxManager)
    manager._lock = asyncio.Lock()
    manager._sandboxes = {"sb": SandboxRef(id="sb", phase=SandboxPhase.READY)}
    policy = SecurityPolicy()
    policy.windows.filesystem.deny_read = ["D:/secret"]
    policy.windows.filesystem.deny_write = ["D:/secret"]
    manager._policies = {"sb": policy}
    manager.policy = policy
    runtime = _Runtime()
    manager.runtime = runtime
    manager.audit = type("Audit", (), {"log": staticmethod(lambda *args, **kwargs: None)})()
    manager.limits = SandboxLimits()
    return manager, runtime


def test_exec_workdir_on_denied_path_is_rejected():
    manager, runtime = _manager()

    async def run():
        with pytest.raises(CodePolicyDenialError):
            await manager.exec_in_sandbox(
                "sb",
                SandboxExecRequest(command=["ls"], workdir="D:/secret"),
            )
        with pytest.raises(CodePolicyDenialError):
            await manager.exec_background_in_sandbox(
                "sb",
                SandboxBackgroundExecRequest(command=["ls"], workdir="D:/secret/child"),
            )

    asyncio.run(run())
    assert runtime.exec_calls == 0


def test_exec_without_workdir_skips_path_policy():
    manager, runtime = _manager()

    async def run():
        result = await manager.exec_in_sandbox(
            "sb",
            SandboxExecRequest(command=["ls"]),
        )
        assert result.exit_code == 0

    asyncio.run(run())
    assert runtime.exec_calls == 1
