"""Verify the public create result leaves raw runtime diagnostics in the log."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import Response

from jiuwenbox.server.routes import sandbox
from jiuwenbox.server.runtime.errors import SandboxCreateError
from jiuwenbox.server.sandbox_manager import SandboxManager
from jiuwenbox.supervisor.network import NetworkSetupError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (SandboxCreateError("Failed to apply Supervisor cgroup limits", diagnostic="private"),
         "Failed to apply Supervisor cgroup limits"),
        (RuntimeError("private"), "Sandbox backend failed to create the sandbox"),
        (NetworkSetupError("Another app is currently holding the xtables lock. private"),
         "Sandbox network setup failed: xtables lock is busy"),
    ],
)
async def test_create_only_marks_public_error_text(monkeypatch, tmp_path, failure, expected):
    runtime = SimpleNamespace(create=AsyncMock(side_effect=failure))
    manager = SandboxManager(runtime=runtime, audit_logger=Mock(), state_dir=tmp_path)
    monkeypatch.setattr(manager.policy_engine, "write_sandbox_policy", lambda *_: tmp_path / "policy")
    monkeypatch.setattr(sandbox, "_mgr", lambda: manager)

    response = Response()
    result = await sandbox.create_sandbox(sandbox.CreateSandboxRequest(), response)

    assert result.phase == "error"
    assert result.error_message == expected
    assert "private" not in result.error_message
    assert response.headers["X-Jiuwenbox-Public-Error"] == "true"
