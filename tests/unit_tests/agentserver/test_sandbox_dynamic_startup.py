"""Startup and runtime switches must use the same registered bootstrap."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server as server, sandbox_config_rpc as rpc

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows startup paths")


@pytest.fixture
def startup(monkeypatch, tmp_path):
    from jiuwenswarm.common import config
    from jiuwenswarm.common.security_config_service import SecurityConfigService
    from jiuwenswarm.agents.harness.common import auto_harness
    from jiuwenswarm.server import sandbox_policy_render
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    runtime = {"enabled": False}
    policy = tmp_path / "policy.yaml"
    policy.write_text("version: 1", encoding="utf-8")
    setup_marker = tmp_path / "installed"
    started, ready = asyncio.Event(), asyncio.Event()
    setup_calls = []

    async def ensure(**kwargs):
        started.set()
        await ready.wait()
        if not setup_marker.exists():
            setup_calls.append("first setup")
            setup_marker.touch()
        return True

    monkeypatch.setattr(rpc, "_internal_bootstrap", None)
    monkeypatch.setattr(server, "get_sandbox_runtime", lambda: runtime)
    monkeypatch.setattr(config, "get_sandbox_runtime", lambda: runtime)
    monkeypatch.setattr(config, "update_sandbox_runtime", runtime.update)
    monkeypatch.setattr(server, "update_sandbox_runtime", runtime.update)
    monkeypatch.setattr(server, "get_sandbox_startup_mode_explicit", lambda: "internal")
    monkeypatch.setattr(server, "get_sandbox_endpoint", lambda: {"url": "http://127.0.0.1:8321"})
    monkeypatch.setattr(server, "ensure_portable_sandbox_policy_file", Mock())
    monkeypatch.setattr(server, "default_sandbox_policy_file", lambda: str(policy))
    monkeypatch.setattr(server, "resolve_sandbox_policy_path", lambda _: policy)
    monkeypatch.setattr(server, "update_sandbox_endpoint", Mock())
    monkeypatch.setattr(server, "_build_sandbox_env", AsyncMock(return_value={}))
    monkeypatch.setattr(auto_harness, "reset_harness_packages_state", Mock())
    monkeypatch.setattr(interface_deep, "ensure_persistent_checkpointer", AsyncMock())
    monkeypatch.setattr(sandbox_policy_render, "_ensure_copy_exists", lambda: policy)
    monkeypatch.setattr("jiuwenswarm.common.net_guard_config.render_saved_sandbox_urls", Mock())
    monkeypatch.setattr(SecurityConfigService, "is_initialized", lambda _: False)
    monkeypatch.setattr(server, "encode_agent_response_for_wire", lambda response, **_: response)
    send = AsyncMock()
    monkeypatch.setattr(server, "send_wire_payload", send)
    background_apply = Mock(side_effect=AssertionError("duplicate background apply"))
    monkeypatch.setattr(rpc, "_trigger_apply", background_apply)

    owner = server.AgentWebSocketServer.__new__(server.AgentWebSocketServer)
    owner._server = None
    owner._desktop_listenerless = False
    owner._startup_complete = None
    owner._startup_error = None
    owner._host, owner._port = "127.0.0.1", 18000
    owner._allocate_internal_jiuwenbox_port = lambda host, port: port
    owner._jiuwenbox_runner = SimpleNamespace(ensure_running=AsyncMock(side_effect=ensure))
    owner._restart_configured_sandboxes = AsyncMock()
    return SimpleNamespace(
        owner=owner, runtime=runtime, started=started, ready=ready, send=send,
        marker=setup_marker, setup_calls=setup_calls,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,installed,toggle", [
    pytest.param(False, False, False, id="1-disabled-first"),
    pytest.param(True, False, False, id="2-enabled-first"),
    pytest.param(True, True, False, id="3-enabled-subsequent"),
    pytest.param(False, True, False, id="4-disabled-subsequent"),
    pytest.param(False, False, True, id="5-enable-after-first-start"),
    pytest.param(False, True, True, id="6-enable-after-subsequent-start"),
])
async def test_six_startup_paths(startup, enabled, installed, toggle):
    s = startup
    s.runtime["enabled"] = enabled
    if installed:
        s.marker.touch()
    task = asyncio.create_task(s.owner.start(listen_tcp=False))
    if enabled:
        await asyncio.wait_for(s.started.wait(), 5)
        assert not task.done()
        assert not s.owner._startup_complete.is_set()
        s.ready.set()
    await task
    assert s.owner._startup_complete.is_set()
    assert rpc._internal_bootstrap == s.owner._bootstrap_internal_jiuwenbox
    if not enabled:
        s.owner._jiuwenbox_runner.ensure_running.assert_not_awaited()
    if toggle:
        request = AgentRequest(
            request_id="enable", channel_id="desktop", req_method=ReqMethod.SANDBOX_ENABLED_SET,
            params={"enabled": True},
        )
        task = asyncio.create_task(s.owner._handle_sandbox_config(None, request, asyncio.Lock()))
        await asyncio.wait_for(s.started.wait(), 5)
        assert not task.done()
        s.send.assert_not_awaited()
        s.ready.set()
        await task
        assert s.send.await_args.args[1].ok
    assert s.owner._jiuwenbox_runner.ensure_running.await_count == int(enabled or toggle)
    assert len(s.setup_calls) == int((enabled or toggle) and not installed)
    await s.owner._checkpointer_warmup_task


@pytest.mark.asyncio
async def test_dynamic_bootstrap_failure_is_returned_instead_of_success(startup):
    s = startup
    await s.owner.start(listen_tcp=False)
    s.owner._jiuwenbox_runner.ensure_running.side_effect = RuntimeError("UAC cancelled")
    request = AgentRequest(
        request_id="enable", channel_id="desktop", req_method=ReqMethod.SANDBOX_ENABLED_SET,
        params={"enabled": True},
    )
    await s.owner._handle_sandbox_config(None, request, asyncio.Lock())
    response = s.send.await_args.args[1]
    assert not response.ok
    assert "UAC cancelled" in response.payload["error"]
    await s.owner._checkpointer_warmup_task
