# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
import subprocess
import sys

import pytest

from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.models.sandbox import ExecResult, SandboxPhase, SandboxRef
from jiuwenbox.server.limits import MAX_TEXT_RESPONSE_BYTES, SandboxLimitError, SandboxLimits
from jiuwenbox.server.runtime.base import RuntimeFileOpResult
from jiuwenbox.server.sandbox_manager import SandboxExecRequest, SandboxManager


class _NoIpcRuntime:
    """File IPC unsupported; exec runs the fallback script with this Python."""

    def __init__(self) -> None:
        self.exec_calls = 0

    async def read_file(self, sandbox_id, sandbox_path):
        return RuntimeFileOpResult(ok=False, error="unsupported")

    async def exec(self, sandbox_id, request):
        self.exec_calls += 1
        command = list(request.command)
        if command[0] == "python3":
            command[0] = sys.executable
        proc = subprocess.run(command, capture_output=True, check=False)
        return ExecResult(
            exit_code=proc.returncode,
            stdout=proc.stdout.decode("ascii", errors="replace"),
            stderr=proc.stderr.decode("utf-8", errors="replace"),
        )


def _manager(limits: SandboxLimits) -> tuple[SandboxManager, _NoIpcRuntime]:
    manager = SandboxManager.__new__(SandboxManager)
    manager._lock = asyncio.Lock()
    manager._sandboxes = {"sb": SandboxRef(id="sb", phase=SandboxPhase.READY)}
    policy = SecurityPolicy()
    manager._policies = {"sb": policy}
    manager.policy = policy
    runtime = _NoIpcRuntime()
    manager.runtime = runtime
    manager.audit = type("Audit", (), {"log": staticmethod(lambda *args, **kwargs: None)})()
    manager.limits = limits
    return manager, runtime


def test_fallback_download_is_not_capped_by_exec_output_limit(tmp_path):
    # Base64 of 4 MiB is ~5.3 MiB, above the generic 4 MiB exec output cap.
    payload = bytes(range(256)) * (4 * 1024 * 4)
    assert len(payload) * 4 // 3 > MAX_TEXT_RESPONSE_BYTES
    target = tmp_path / "big.bin"
    target.write_bytes(payload)
    manager, runtime = _manager(SandboxLimits(max_file_mb=5))

    content = asyncio.run(manager.download_file_from_sandbox("sb", str(target)))

    assert content == payload
    assert runtime.exec_calls == 1


def test_fallback_download_over_file_limit_uses_download_error(tmp_path):
    target = tmp_path / "big.bin"
    target.write_bytes(b"x" * (1024 * 1024 + 1))
    manager, _ = _manager(SandboxLimits(max_file_mb=1))

    with pytest.raises(SandboxLimitError) as exc:
        asyncio.run(manager.download_file_from_sandbox("sb", str(target)))

    assert exc.value.status_code == 413
    assert exc.value.code == "SANDBOX_DOWNLOAD_TOO_LARGE"


def test_fallback_output_overflow_is_reported_as_download_limit():
    manager, _ = _manager(SandboxLimits(max_file_mb=1))

    async def oversized_exec(sandbox_id, request):
        return ExecResult(exit_code=0, stdout="A" * (manager.limits.download_fallback_output_bytes + 1))

    manager.runtime.exec = oversized_exec
    with pytest.raises(SandboxLimitError) as exc:
        asyncio.run(manager._download_via_exec_fallback("sb", "/tmp/file.bin"))

    assert exc.value.code == "SANDBOX_DOWNLOAD_TOO_LARGE"


def test_http_exec_keeps_generic_output_limit():
    manager, _ = _manager(SandboxLimits())

    async def oversized_exec(sandbox_id, request):
        return ExecResult(exit_code=0, stdout="x" * (MAX_TEXT_RESPONSE_BYTES + 1))

    manager.runtime.exec = oversized_exec
    with pytest.raises(SandboxLimitError) as exc:
        asyncio.run(manager.exec_in_sandbox("sb", SandboxExecRequest(command=["cat"])))

    assert exc.value.code == "SANDBOX_EXEC_OUTPUT_TOO_LARGE"
