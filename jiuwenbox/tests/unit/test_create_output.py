"""Creation-time stdio survives API serialization and lifecycle restart."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from jiuwenbox.models.sandbox import SandboxPhase, SandboxRef
from jiuwenbox.server.routes import sandbox
from jiuwenbox.server.sandbox_manager import SandboxManager


@pytest.mark.asyncio
async def test_create_stdio_reaches_runtime_and_survives_stopped_lifecycle_restart(tmp_path, monkeypatch):
    runtime = SimpleNamespace(
        create=AsyncMock(return_value=123), is_running=AsyncMock(return_value=True), stop=AsyncMock(),
    )
    manager = SandboxManager(runtime=runtime, audit_logger=Mock(), state_dir=tmp_path)
    monkeypatch.setattr(manager.policy_engine, "write_sandbox_policy", lambda *_: tmp_path / "policy")
    monkeypatch.setattr(manager, "_resolve_sandbox_ip_address", AsyncMock(return_value=None))
    monkeypatch.setattr(sandbox, "_mgr", lambda: manager)
    app = FastAPI()
    app.include_router(sandbox.router, prefix="/api/v1")
    payload = {
        "sandbox_id": "logs-create",
        "stdout": str(tmp_path / "agent.out"),
        "stderr": str(tmp_path / "agent.err"),
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/api/v1/sandboxes", json=payload)
        assert response.status_code == 201, response.text
        assert (await client.post("/api/v1/sandboxes/logs-create/stop")).status_code == 200
        saved = SandboxRef.model_validate_json((tmp_path / "logs-create.json").read_bytes())
        assert saved.phase == SandboxPhase.STOPPED
        restarted = await client.post("/api/v1/sandboxes/logs-create/start")
        assert restarted.status_code == 200, restarted.text
    assert runtime.create.await_count == 2
    for key in ("stdout", "stderr"):
        assert response.json()[key] == getattr(saved, key) == restarted.json()[key] == payload[key]
        assert all(call.kwargs[key] == payload[key] for call in runtime.create.await_args_list)


@pytest.mark.parametrize("value", ["relative.out", "/tmp/nul\x00.out"])
def test_create_rejects_invalid_host_output_path(value):
    with pytest.raises(ValueError):
        sandbox.CreateSandboxRequest(stdout=value)


def test_create_output_defaults_match_empty_sandboxd_redirects():
    request = sandbox.CreateSandboxRequest()
    assert request.stdout == "" and request.stderr == ""
