"""The configuration page must reach AgentServer sandbox RPCs through Web."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import WebHandlersBindParams, _register_web_handlers


class Channel:
    def __init__(self):
        self.methods = {}
        self.send_response = AsyncMock()

    def register_method(self, method, handler):
        self.methods[method] = handler

    def on_connect(self, handler):
        pass


def test_file_and_network_guard_reads_do_not_trigger_reload():
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc import (
        get_permissions_read_only_req_methods,
    )
    from jiuwenswarm.common.schema.message import ReqMethod

    methods = get_permissions_read_only_req_methods()
    assert ReqMethod.PERMISSIONS_FILE_GUARD_GET in methods
    assert ReqMethod.PERMISSIONS_NET_GUARD_GET in methods
    assert ReqMethod.PERMISSIONS_FILE_GUARD_UPDATE not in methods
    assert ReqMethod.PERMISSIONS_NET_GUARD_SET not in methods


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["sandbox.enabled.get", "sandbox.enabled.set", "sandbox.files.sync", "sandbox.restart"])
async def test_sandbox_web_rpc_requires_agent(method):
    channel = Channel()
    _register_web_handlers(WebHandlersBindParams(channel=channel))
    await channel.methods[method](None, "test", {}, None)
    assert channel.send_response.await_args.kwargs["code"] == "AGENT_NOT_READY"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["permissions.file_guard.get", "permissions.file_guard.update",
                                    "permissions.shell_guard.get", "permissions.shell_guard.update",
                                    "permissions.rules.create", "permissions.rules.update", "permissions.rules.delete",
                                    "sandbox.enabled.get", "sandbox.enabled.set", "sandbox.files.sync", "sandbox.restart"])
async def test_security_web_rpc_forwards_response_and_error(method):
    channel = Channel()
    response = SimpleNamespace(ok=True, payload={"status": "applied", "restarted": 1})
    client = SimpleNamespace(server_ready=True, send_request=AsyncMock(return_value=response))
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=client))
    await channel.methods[method](None, "test", {"enabled": True}, "session")
    assert client.send_request.await_count == 1
    forwarded = client.send_request.await_args.args[0]
    assert forwarded.method == method
    assert forwarded.params == {"enabled": True}
    assert forwarded.session_id == "session"
    assert channel.send_response.await_args.kwargs["payload"] == response.payload
    response.ok = False
    response.payload = {"error": "cannot apply rule", "code": "BAD_REQUEST"}
    await channel.methods[method](None, "test2", {}, None)
    assert channel.send_response.await_args.kwargs["ok"] is False
    assert channel.send_response.await_args.kwargs["error"] == "cannot apply rule"
