import asyncio
import json
import sys
import time
from types import SimpleNamespace

import pytest

from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


class AgentWebSocketServerHarness(agent_ws_server_module.AgentWebSocketServer):
    async def handle_browser_runtime_restart_for_test(self, ws, request, send_lock):
        await self._handle_browser_runtime_restart(ws, request, send_lock)

    async def handle_command_add_dir_for_test(self, ws, request, send_lock):
        await self._handle_command_add_dir(ws, request, send_lock)

    async def handle_command_compact_for_test(self, ws, request, send_lock):
        await self._handle_command_compact(ws, request, send_lock)

    async def handle_command_diff_for_test(self, ws, request, send_lock):
        await self._handle_command_diff(ws, request, send_lock)

    async def handle_command_simplify_for_test(self, ws, request, send_lock):
        await self._handle_command_simplify(ws, request, send_lock)

    async def handle_command_model_for_test(self, ws, request, send_lock):
        await self._handle_command_model(ws, request, send_lock)

    async def handle_command_mcp_for_test(self, ws, request, send_lock):
        await self._handle_command_mcp(ws, request, send_lock)

    async def handle_command_resume_for_test(self, ws, request, send_lock):
        await self._handle_command_resume(ws, request, send_lock)

    async def handle_command_session_for_test(self, ws, request, send_lock):
        await self._handle_command_session(ws, request, send_lock)

    async def handle_permissions_config_for_test(self, ws, request, send_lock):
        await self._handle_permissions_config(ws, request, send_lock)

    async def handle_sandbox_config_for_test(self, ws, request, send_lock):
        await self._handle_sandbox_config(ws, request, send_lock)

    def get_agent_manager_for_test(self):
        return self._agent_manager


def fake_encode_agent_response_for_wire(resp, response_id):
    return {
        "response_id": response_id,
        "payload": resp.payload,
        "ok": resp.ok,
    }


@pytest.fixture
def server():
    return AgentWebSocketServerHarness()


@pytest.fixture
def fake_ws():
    return FakeWebSocket()


@pytest.fixture(autouse=True)
def patch_wire_encoder(monkeypatch):
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )


@pytest.mark.asyncio
async def test_browser_runtime_restart_resets_active_agent_runtimes(
    server,
    fake_ws,
    monkeypatch,
):
    from openjiuwen.harness.tools import browser_move

    async def fake_reset_active_browser_runtimes():
        return 2

    monkeypatch.setattr(
        browser_move,
        "reset_active_browser_runtimes",
        fake_reset_active_browser_runtimes,
        raising=False,
    )
    monkeypatch.setattr(
        browser_move,
        "restart_local_browser_runtime_server",
        lambda: {"status": "restarted"},
    )
    request = AgentRequest(
        request_id="req-browser-restart",
        channel_id="web",
        req_method=ReqMethod.BROWSER_RUNTIME_RESTART,
    )

    await server.handle_browser_runtime_restart_for_test(
        fake_ws,
        request,
        asyncio.Lock(),
    )

    assert fake_ws.sent == [
        {
            "response_id": "req-browser-restart",
            "payload": {
                "result": {"status": "restarted"},
                "reset_runtimes": 2,
            },
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_browser_runtime_restart_supports_sdk_without_runtime_reset():
    reset_runtimes = (
        await agent_ws_server_module._reset_active_browser_runtimes_if_available(
            SimpleNamespace()
        )
    )

    assert reset_runtimes == 0


@pytest.mark.asyncio
async def test_browser_runtime_restart_uses_identity_scoped_sdk_reset():
    calls = []

    async def reset_managed_browser_runtime(**kwargs):
        calls.append(kwargs)
        return 1

    reset_runtimes = await agent_ws_server_module._reset_requested_browser_runtime_if_available(
        SimpleNamespace(
            reset_managed_browser_runtime=reset_managed_browser_runtime,
        ),
        {
            "browser_key": "",
            "profile_name": "jiuwenclaw",
            "display_mode": "headed",
            "browser_binary": "C:\\Chrome\\chrome.exe",
        },
    )

    assert reset_runtimes == 1
    assert calls == [
        {
            "browser_key": "",
            "profile_name": "jiuwenclaw",
            "display_mode": "headed",
            "browser_binary": "C:\\Chrome\\chrome.exe",
        }
    ]


@pytest.mark.asyncio
async def test_handle_command_add_dir_returns_path_and_remember(
    server, fake_ws, monkeypatch
):
    persist_stub = {
        "ok": True,
        "normalized": "/tmp/demo",
        "path_pattern": "re:^/tmp/demo(?:$|/)",
        "shell_pattern": "re:.*/tmp/demo.*",
        "tiered_overrides": True,
    }
    monkeypatch.setattr(
        agent_ws_server_module,
        "persist_cli_trusted_directory",
        lambda _raw: persist_stub,
    )
    request = AgentRequest(
        request_id="req-add-dir",
        channel_id="tui",
        req_method=ReqMethod.COMMAND_ADD_DIR,
        params={"path": "/tmp/demo", "remember": True},
    )

    await server.handle_command_add_dir_for_test(fake_ws, request, asyncio.Lock())

    assert fake_ws.sent == [
        {
            "response_id": "req-add-dir",
            "payload": {
                "path": "/tmp/demo",
                "remember": True,
                "persist": persist_stub,
            },
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_command_add_dir_does_not_wait_for_agent_reload(
    server, fake_ws, monkeypatch
):
    persist_stub = {
        "ok": True,
        "normalized": "/tmp/demo",
    }
    monkeypatch.setattr(
        agent_ws_server_module,
        "persist_cli_trusted_directory",
        lambda _raw: persist_stub,
    )
    monkeypatch.setattr(agent_ws_server_module, "get_config", lambda: {})
    reload_started = asyncio.Event()

    async def _blocking_reload(_config, _env):
        reload_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        server.get_agent_manager(),
        "reload_agents_config",
        _blocking_reload,
    )
    request = AgentRequest(
        request_id="req-add-dir-no-reload-wait",
        channel_id="tui",
        req_method=ReqMethod.COMMAND_ADD_DIR,
        params={"path": "/tmp/demo", "remember": True},
    )

    await asyncio.wait_for(
        server.handle_command_add_dir_for_test(fake_ws, request, asyncio.Lock()),
        timeout=0.5,
    )

    assert not reload_started.is_set()
    assert fake_ws.sent == [
        {
            "response_id": "req-add-dir-no-reload-wait",
            "payload": {
                "path": "/tmp/demo",
                "remember": True,
                "persist": persist_stub,
            },
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_command_compact_returns_custom_instructions(server, fake_ws, monkeypatch):
    request = AgentRequest(
        request_id="req-compact",
        channel_id="tui",
        req_method=ReqMethod.COMMAND_COMPACT,
        params={"instructions": "focus on architecture"},
    )

    class MockAgent:
        async def compress_context(self, session_id, *, return_state=False):
            return {
                "result": "compressed",
                "stats": {
                    "raw_total_tokens": 1000,
                    "total_tokens": 300,
                },
            }

    mock_agent = MockAgent()

    async def mock_get_agent(channel_id, mode, project_dir=None, sub_mode=None):
        return mock_agent

    async def mock_send_push(msg):
        pass

    monkeypatch.setattr(
        server.get_agent_manager_for_test(),
        "get_agent",
        mock_get_agent,
    )
    monkeypatch.setattr(
        server,
        "send_push",
        mock_send_push,
    )

    await server.handle_command_compact_for_test(fake_ws, request, asyncio.Lock())

    assert fake_ws.sent == [
        {
            "response_id": "req-compact",
            "payload": {
                "result": "compressed",
                "stats": {
                    "raw_total_tokens": 1000,
                    "total_tokens": 300,
                },
            },
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_command_compact_pushes_current_compression_state_event(server, fake_ws, monkeypatch):
    request = AgentRequest(
        request_id="req-compact",
        channel_id="tui",
        session_id="session-1",
        req_method=ReqMethod.COMMAND_COMPACT,
        params={"mode": "agent.plan"},
    )

    class MockAgent:
        async def compress_context(self, session_id, *, return_state=False):
            return {
                "result": "compressed",
                "stats": {
                    "raw_total_tokens": 1000,
                    "total_tokens": 300,
                },
                "state": {
                    "status": "completed",
                    "phase": "active_compress",
                    "compact_summary": "manual compact summary",
                },
                "compact_summary": "manual compact summary",
            }

    pushed = []

    async def mock_get_agent(channel_id, mode, project_dir=None, sub_mode=None):
        return MockAgent()

    async def mock_send_push(msg):
        pushed.append(msg)

    monkeypatch.setattr(
        server.get_agent_manager_for_test(),
        "get_agent",
        mock_get_agent,
    )
    monkeypatch.setattr(server, "send_push", mock_send_push)

    await server.handle_command_compact_for_test(fake_ws, request, asyncio.Lock())

    compression_state_pushes = [
        item for item in pushed
        if item.get("payload", {}).get("event_type") == "context.compression_state"
    ]
    assert len(compression_state_pushes) == 1
    assert compression_state_pushes[0]["session_id"] == "session-1"
    assert compression_state_pushes[0]["payload"]["compact_summary"] == "manual compact summary"


@pytest.mark.asyncio
async def test_handle_command_diff_returns_summary_payload(server, fake_ws):
    request = AgentRequest(
        request_id="req-diff",
        channel_id="tui",
        req_method=ReqMethod.COMMAND_DIFF,
        params={},
    )

    await server.handle_command_diff_for_test(fake_ws, request, asyncio.Lock())

    assert fake_ws.sent == [
        {
            "response_id": "req-diff",
            "payload": {
                "type": "list",
                "turns": [],
            },
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_command_diff_includes_session_extra_history_roots(
    server, fake_ws, monkeypatch
):
    captured = {}

    class FakeDiffService:
        def get_turn_diffs(self, session_id, project_dir, **kwargs):
            captured["turns"] = {
                "session_id": session_id,
                "project_dir": project_dir,
                "kwargs": kwargs,
            }
            return []

        def get_git_diff(self, project_dir):
            captured["git_diff_project_dir"] = project_dir
            return None

    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_diff_service",
        lambda: FakeDiffService(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.git_diff_status.get_session_extra_history_roots",
        lambda session_id: [f"/history/{session_id}/worktrees"],
    )
    request = AgentRequest(
        request_id="req-diff-extra-roots",
        channel_id="tui",
        session_id="sess-1",
        req_method=ReqMethod.COMMAND_DIFF,
        params={"project_dir": "/repo"},
    )

    await server.handle_command_diff_for_test(fake_ws, request, asyncio.Lock())

    assert captured["turns"] == {
        "session_id": "sess-1",
        "project_dir": "/repo",
        "kwargs": {
            "extra_history_roots": ["/history/sess-1/worktrees"],
        },
    }
    assert captured["git_diff_project_dir"] == "/repo"
    assert fake_ws.sent == [
        {
            "response_id": "req-diff-extra-roots",
            "payload": {
                "type": "list",
                "turns": [],
            },
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_command_simplify_returns_prompt(server, fake_ws):
    """ /simplify 无 target 时返回包含三阶段审查结构的 prompt。"""
    request = AgentRequest(
        request_id="req-simplify",
        channel_id="tui",
        req_method=ReqMethod.COMMAND_SIMPLIFY,
        params={},
    )

    await server.handle_command_simplify_for_test(fake_ws, request, asyncio.Lock())

    assert len(fake_ws.sent) == 1
    msg = fake_ws.sent[0]
    assert msg["response_id"] == "req-simplify"
    assert msg["ok"] is True
    prompt = msg["payload"]["prompt"]
    # prompt should contain three-phase structure keywords (English)
    assert "Phase 1" in prompt
    assert "Phase 2" in prompt
    assert "Phase 3" in prompt
    # All three review dimensions should be present
    assert "Code Reuse Review" in prompt
    assert "Code Quality Review" in prompt
    assert "Efficiency Review" in prompt
    # Parallel sub-agent review should be optional, not mandatory
    assert "task_tool" in prompt or "Agent tool" in prompt
    assert "perform all three reviews yourself directly" in prompt
    # No Additional Focus section when no target is given
    assert "Additional Focus" not in prompt
    # Security is explicitly out of scope — must point to /security-review
    assert "security" in prompt.lower()
    assert "/security-review" in prompt


@pytest.mark.asyncio
async def test_handle_command_simplify_with_target_appends_focus(server, fake_ws):
    """/simplify 带 target 时将关注点追加到 prompt 末尾。"""
    request = AgentRequest(
        request_id="req-simplify-target",
        channel_id="tui",
        req_method=ReqMethod.COMMAND_SIMPLIFY,
        params={"target": "src/auth/ 模块的错误处理"},
    )

    await server.handle_command_simplify_for_test(fake_ws, request, asyncio.Lock())

    msg = fake_ws.sent[0]
    assert msg["ok"] is True
    prompt = msg["payload"]["prompt"]
    assert "Additional Focus" in prompt
    assert "src/auth/ 模块的错误处理" in prompt


@pytest.mark.asyncio
async def test_handle_command_model_no_action_shows_current(
    server, fake_ws, monkeypatch
):
    """No action → returns current model from os.environ and available list."""
    monkeypatch.setenv("MODEL_NAME", "test-model")
    request = AgentRequest(
        request_id="req-model",
        channel_id="tui",
        req_method=ReqMethod.COMMAND_MODEL,
        params={},
    )

    await server.handle_command_model_for_test(fake_ws, request, asyncio.Lock())

    assert fake_ws.sent == [
        {
            "response_id": "req-model",
            "payload": {
                "current": "test-model",
                "available": ["default-model"],
            },
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_command_model_add_model(server, fake_ws):
    """action=add_model → returns model_added confirmation."""
    request = AgentRequest(
        request_id="req-add",
        channel_id="cli",
        req_method=ReqMethod.COMMAND_MODEL,
        params={"action": "add_model", "target": "my-model", "config": {}},
    )

    await server.handle_command_model_for_test(fake_ws, request, asyncio.Lock())

    assert fake_ws.sent == [
        {
            "response_id": "req-add",
            "payload": {"type": "model_added", "name": "my-model"},
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_command_mcp_list(server, fake_ws, monkeypatch):
    monkeypatch.setattr(
        agent_ws_server_module,
        "get_mcp_servers",
        lambda: [{"name": "demo", "transport": "stdio", "enabled": True, "env": {"TOKEN": "abc"}}],
    )
    request = AgentRequest(
        request_id="req-mcp-list",
        channel_id="tui",
        req_method=ReqMethod.COMMAND_MCP,
        params={"action": "list"},
    )

    await server.handle_command_mcp_for_test(fake_ws, request, asyncio.Lock())

    assert fake_ws.sent == [
        {
            "response_id": "req-mcp-list",
            "payload": {
                "type": "list",
                "items": [{"name": "demo", "transport": "stdio", "enabled": True, "env": {"TOKEN": "***"}}],
            },
            "ok": True,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [8192, 8193, 9000])
async def test_handle_command_mcp_list_tools_url_size(server, fake_ws, monkeypatch, size):
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr
    from openjiuwen.harness.security.outbound import policy as host_exit

    # Exercise the real policy and logging; only the allowed connection is fake.
    monkeypatch.setattr(host_exit, "_state", host_exit.get_host_exit_state())
    host_exit.publish_host_exit_policy({"net_guard": {"enabled": True, "urls": {}}})
    base = "https://mcp.invalid/sse?pad="
    entry = {"name": "url-size-regression", "transport": "sse",
             "url": base + "a" * (size - len(base))}
    monkeypatch.setattr(agent_ws_server_module, "get_mcp_server_config", lambda name: entry)
    calls = []

    class FakeClient:
        async def connect(self):
            calls.append("connect")
            return True

        async def list_tools(self):
            return []

        async def disconnect(self):
            calls.append("disconnect")

    monkeypatch.setattr(ToolMgr, "_create_client", staticmethod(lambda config: FakeClient()))
    request = AgentRequest(
        request_id="req-mcp-url-size", channel_id="web", req_method=ReqMethod.COMMAND_MCP,
        params={"action": "list_tools", "name": entry["name"]},
    )
    started = time.monotonic()
    await server.handle_command_mcp_for_test(fake_ws, request, asyncio.Lock())
    assert time.monotonic() - started < 2.0
    assert calls == (["connect", "disconnect"] if size == 8192 else [])
    assert fake_ws.sent == [{
        "response_id": "req-mcp-url-size", "ok": True,
        "payload": {"type": "tools", "tools": [], "server_name": entry["name"]},
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["connect", "list_tools", "disconnect", "healthy"])
async def test_mcp_fetch_deadlines_and_lifecycle_owner(monkeypatch, stage):
    import anyio
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr
    from jiuwenswarm.common import mcp_call_timeout_patch as timeouts

    monkeypatch.setattr(timeouts, "DISCONNECT_TIMEOUT", 0.03)
    owner = asyncio.current_task()
    calls = []

    class FakeClient:
        async def connect(self):
            assert asyncio.current_task() is owner
            calls.append("connect")
            # A real transport keeps anyio scopes open across connect/list/
            # disconnect; a healthy fetch must close them in the same Task.
            self.scope = anyio.CancelScope()
            self.scope.__enter__()
            if stage == "connect":
                await asyncio.Event().wait()
            return True

        async def list_tools(self):
            assert asyncio.current_task() is owner
            calls.append("list_tools")
            if stage == "list_tools":
                await asyncio.Event().wait()
            return []

        async def disconnect(self):
            assert asyncio.current_task() is owner
            calls.append("disconnect")
            try:
                if stage == "disconnect":
                    await asyncio.Event().wait()
            finally:
                self.scope.__exit__(None, None, None)

    configs = []

    def create_client(config):
        configs.append(config)
        return FakeClient()

    monkeypatch.setattr(ToolMgr, "_create_client", staticmethod(create_client))
    entry = {"name": "deadline", "transport": "stdio", "command": "unused", "timeout_s": 0.03}
    started = time.monotonic()
    fetch = agent_ws_server_module.AgentWebSocketServer._fetch_mcp_tools_from_config
    if stage in {"connect", "list_tools"}:
        with pytest.raises(TimeoutError):
            await fetch(entry)
    else:
        assert await fetch(entry) == []
    assert time.monotonic() - started < 1.0
    assert calls[-1] == "disconnect"
    assert configs[0].params["timeout_s"] == 0.03


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_slow_mcp_discovery_honors_explicit_server_budget(monkeypatch, configured):
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr
    from jiuwenswarm.common import mcp_call_timeout_patch as timeouts

    monkeypatch.setattr(timeouts, "DISCOVERY_TIMEOUT", 0.01)
    calls = []
    configs = []

    class SlowClient:
        async def connect(self):
            calls.append("connect")
            await asyncio.sleep(0.03)
            return True

        async def list_tools(self):
            calls.append("list_tools")
            await asyncio.sleep(0.03)
            return []

        async def disconnect(self):
            calls.append("disconnect")

    def create_client(config):
        configs.append(config)
        return SlowClient()

    monkeypatch.setattr(ToolMgr, "_create_client", staticmethod(create_client))
    entry = {"name": "slow-healthy", "transport": "stdio", "command": "unused"}
    if configured:
        entry["timeout_s"] = 0.3
    fetch = agent_ws_server_module.AgentWebSocketServer._fetch_mcp_tools_from_config
    if configured:
        assert await fetch(entry) == []
        assert calls == ["connect", "list_tools", "disconnect"]
    else:
        with pytest.raises(TimeoutError):
            await fetch(entry)
        assert calls == ["connect", "disconnect"]
    assert configs[0].params["timeout_s"] == (0.3 if configured else 0.01)


@pytest.mark.asyncio
async def test_mcp_fetch_real_stdio_lifecycle():
    """A real SDK transport must survive the new deadlines and close cleanly."""
    script = (
        "from mcp.server.fastmcp import FastMCP\n"
        "server = FastMCP('discovery-regression')\n"
        "@server.tool()\n"
        "def echo(message: str) -> str:\n"
        "    return message\n"
        "server.run(transport='stdio')\n"
    )
    tools = await agent_ws_server_module.AgentWebSocketServer._fetch_mcp_tools_from_config({
        "name": "real-stdio", "transport": "stdio", "command": sys.executable,
        "args": ["-c", script], "timeout_s": 15,
    })
    assert [tool["name"] for tool in tools] == ["echo"]
    assert tools[0]["parameters"]["properties"]["message"]["type"] == "string"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["healthy", "connect_timeout", "list_timeout", "cancel"])
async def test_mcp_fetch_real_http_protocol_and_scopes(monkeypatch, outcome):
    from functools import partial
    import httpx
    from mcp.client import streamable_http
    from jiuwenswarm.common.mcp_call_timeout_patch import apply_mcp_call_timeout_patch
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import StreamableHttpClient

    apply_mcp_call_timeout_patch()

    methods = []
    stalled = outcome != "healthy"
    list_started = asyncio.Event()
    disconnects = []
    original_disconnect = StreamableHttpClient.disconnect

    async def slow_disconnect(client, **kwargs):
        disconnects.append(asyncio.current_task())
        if stalled:
            # Cleanup crosses the discovery deadline; it must finish once,
            # without cancellation from an overlapping wrapper deadline.
            await asyncio.sleep(0.08)
        return await original_disconnect(client, **kwargs)

    monkeypatch.setattr(StreamableHttpClient, "disconnect", slow_disconnect)

    async def respond(request):
        if request.method == "GET":
            return httpx.Response(405)
        if request.method == "DELETE":
            return httpx.Response(200)
        body = json.loads(request.content)
        method = body["method"]
        methods.append(method)
        if "id" not in body:
            return httpx.Response(202)
        if method == "initialize":
            if outcome == "connect_timeout":
                await asyncio.Event().wait()
            result = {"protocolVersion": body["params"]["protocolVersion"],
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "regression", "version": "1"}}
        else:
            assert method == "tools/list"
            list_started.set()
            if outcome in {"list_timeout", "cancel"}:
                await asyncio.Event().wait()
            result = {"tools": [{"name": "echo", "description": "Echo input",
                                 "inputSchema": {"type": "object"}}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    def client_factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(respond), **kwargs)

    # Keep the real MCP transport/session/task groups and replace only HTTP I/O.
    monkeypatch.setattr(streamable_http, "streamablehttp_client", partial(
        streamable_http.streamablehttp_client, httpx_client_factory=client_factory,
    ))
    entry = {"name": "real-http", "transport": "streamable-http",
             "url": "http://mcp.invalid/mcp", "timeout_s": 0.05 if outcome.endswith("timeout") else 1.0}
    fetch = agent_ws_server_module.AgentWebSocketServer._fetch_mcp_tools_from_config
    before = asyncio.all_tasks()
    started = time.monotonic()
    owner = asyncio.current_task()
    if outcome == "cancel":
        owner = asyncio.create_task(fetch(entry))
        await asyncio.wait_for(list_started.wait(), 1.0)
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
    elif stalled:
        with pytest.raises(TimeoutError):
            await fetch(entry)
    else:
        assert [tool["name"] for tool in await fetch(entry)] == ["echo"]
    if outcome != "connect_timeout":
        assert "tools/list" in methods
    assert disconnects == [owner]
    assert time.monotonic() - started < 2.0
    await asyncio.sleep(0)
    assert not [task for task in asyncio.all_tasks() - before if not task.done()]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,code", [
    (TimeoutError(), "MCP_TIMEOUT"), (ConnectionError("offline"), "MCP_CONNECTION_FAILED"),
    (RuntimeError("discovery failed"), "MCP_INTERNAL"), (None, None),
])
async def test_command_mcp_discovery_distinguishes_failure_from_empty(server, fake_ws, monkeypatch, failure, code):
    monkeypatch.setattr(agent_ws_server_module, "get_mcp_server_config", lambda _name: {"enabled": True})

    async def fetch(_entry):
        if failure is not None:
            raise failure
        return []

    monkeypatch.setattr(server, "_fetch_mcp_tools_from_config", fetch)
    request = AgentRequest(request_id="discovery-result", channel_id="web", req_method=ReqMethod.COMMAND_MCP,
                           params={"action": "list_tools", "name": "unused-discovery"})
    await server.handle_command_mcp_for_test(fake_ws, request, asyncio.Lock())
    response = fake_ws.sent[0]
    assert response["ok"] is (failure is None)
    if code:
        assert response["payload"]["code"] == code
    else:
        assert response["payload"]["tools"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,code", [
    (TimeoutError(), "MCP_TIMEOUT"), (ConnectionError("offline"), "MCP_CONNECTION_FAILED"),
    (RuntimeError("discovery failed"), "MCP_INTERNAL"), (None, None),
])
async def test_command_mcp_show_keeps_configuration_when_discovery_fails(server, fake_ws, monkeypatch, failure, code):
    from openjiuwen.core.runner import Runner

    entry = {"name": "offline-server", "enabled": True, "transport": "stdio", "command": "python"}
    monkeypatch.setattr(agent_ws_server_module, "get_mcp_server_config", lambda _name: entry)
    monkeypatch.setattr(Runner, "resource_mgr", SimpleNamespace(_resource_registry=None))

    async def fetch(_entry):
        if failure is not None:
            raise failure
        return []

    monkeypatch.setattr(server, "_fetch_mcp_tools_from_config", fetch)
    request = AgentRequest(request_id="show-result", channel_id="web", req_method=ReqMethod.COMMAND_MCP,
                           params={"action": "show", "name": entry["name"]})
    await server.handle_command_mcp_for_test(fake_ws, request, asyncio.Lock())
    response = fake_ws.sent[0]
    assert response["ok"] is True
    assert response["payload"]["type"] == "detail"
    item = response["payload"]["item"]
    assert {key: item[key] for key in entry} == entry
    assert item["tool_count"] == (None if failure is not None else 0)
    if code:
        assert item["tool_count_error"]["code"] == code
        assert item["tool_count_error"]["error"]
    else:
        assert "tool_count_error" not in item
    assert entry == {"name": "offline-server", "enabled": True, "transport": "stdio", "command": "python"}


@pytest.mark.asyncio
async def test_command_mcp_show_all_does_not_probe_servers(server, fake_ws, monkeypatch):
    from unittest.mock import AsyncMock

    entries = [
        {"name": "offline-server", "enabled": True},
        {"name": "healthy-server", "enabled": True},
        {"name": "disabled-server", "enabled": False},
    ]
    monkeypatch.setattr(agent_ws_server_module, "get_mcp_servers", lambda: entries)
    fetch = AsyncMock(side_effect=ConnectionError("offline"))
    monkeypatch.setattr(server, "_fetch_mcp_tools_from_config", fetch)
    request = AgentRequest(request_id="show-all", channel_id="web", req_method=ReqMethod.COMMAND_MCP,
                           params={"action": "show"})
    await server.handle_command_mcp_for_test(fake_ws, request, asyncio.Lock())
    assert fake_ws.sent == [{
        "response_id": "show-all", "ok": True,
        "payload": {"type": "list", "items": entries[:2]},
    }]
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_command_mcp_show_preserves_cancellation(server, fake_ws, monkeypatch):
    from openjiuwen.core.runner import Runner

    monkeypatch.setattr(agent_ws_server_module, "get_mcp_server_config", lambda _name: {"enabled": True})
    monkeypatch.setattr(Runner, "resource_mgr", SimpleNamespace(_resource_registry=None))

    async def fetch(_entry):
        raise asyncio.CancelledError()

    monkeypatch.setattr(server, "_fetch_mcp_tools_from_config", fetch)
    request = AgentRequest(request_id="show-cancel", channel_id="web", req_method=ReqMethod.COMMAND_MCP,
                           params={"action": "show", "name": "cancelled-server"})
    with pytest.raises(asyncio.CancelledError):
        await server.handle_command_mcp_for_test(fake_ws, request, asyncio.Lock())
    assert not fake_ws.sent


@pytest.mark.asyncio
async def test_handle_command_mcp_add_triggers_reload(server, fake_ws, monkeypatch):
    monkeypatch.setattr(
        agent_ws_server_module,
        "upsert_mcp_server_in_config",
        lambda payload: (payload, True),
    )
    monkeypatch.setattr(agent_ws_server_module, "get_config", lambda: {"mcp": {"servers": []}})

    # Mock pre-check so it does not attempt a real MCP connection.
    async def _pre_check_ok(_payload):
        return True, "pre-check ok"

    monkeypatch.setattr(
        agent_ws_server_module.AgentWebSocketServer,
        "_pre_check_mcp_server",
        staticmethod(_pre_check_ok),
    )

    called = {"reload": 0}

    async def _reload(_config, _env):
        called["reload"] += 1

    monkeypatch.setattr(server.get_agent_manager(), "reload_agents_config", _reload)
    request = AgentRequest(
        request_id="req-mcp-add",
        channel_id="tui",
        req_method=ReqMethod.COMMAND_MCP,
        params={
            "action": "add",
            "name": "demo",
            "transport": "stdio",
            "command": "python",
            "args": ["server.py"],
        },
    )

    await server.handle_command_mcp_for_test(fake_ws, request, asyncio.Lock())
    assert called["reload"] == 1
    assert fake_ws.sent == [
        {
            "response_id": "req-mcp-add",
            "payload": {"type": "added", "name": "demo", "applied": True},
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_command_mcp_enable_not_found(server, fake_ws, monkeypatch):
    def _raise_not_found(_name, _enabled):
        raise KeyError("MCP server 'demo' not found")

    monkeypatch.setattr(agent_ws_server_module, "set_mcp_server_enabled_in_config", _raise_not_found)
    request = AgentRequest(
        request_id="req-mcp-enable",
        channel_id="tui",
        req_method=ReqMethod.COMMAND_MCP,
        params={"action": "enable", "name": "demo"},
    )

    await server.handle_command_mcp_for_test(fake_ws, request, asyncio.Lock())
    assert fake_ws.sent == [
        {
            "response_id": "req-mcp-enable",
            "payload": {"error": "\"MCP server 'demo' not found\"", "code": "MCP_NOT_FOUND"},
            "ok": False,
        }
    ]


@pytest.mark.asyncio
async def test_handle_command_mcp_remove(server, fake_ws, monkeypatch):
    monkeypatch.setattr(
        agent_ws_server_module,
        "remove_mcp_server_in_config",
        lambda name: {"name": name, "enabled": True, "transport": "sse", "url": "http://127.0.0.1:9000/sse"},
    )
    monkeypatch.setattr(agent_ws_server_module, "get_config", lambda: {"mcp": {"servers": []}})

    async def _reload(_config, _env):
        return None

    monkeypatch.setattr(server.get_agent_manager(), "reload_agents_config", _reload)
    request = AgentRequest(
        request_id="req-mcp-remove",
        channel_id="tui",
        req_method=ReqMethod.COMMAND_MCP,
        params={"action": "remove", "name": "demo"},
    )

    await server.handle_command_mcp_for_test(fake_ws, request, asyncio.Lock())
    assert fake_ws.sent == [
        {
            "response_id": "req-mcp-remove",
            "payload": {
                "type": "removed",
                "name": "demo",
                "applied": True,
                "item": {"name": "demo", "enabled": True, "transport": "sse", "url": "http://127.0.0.1:9000/sse"},
            },
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_command_mcp_update(server, fake_ws, monkeypatch):
    monkeypatch.setattr(
        agent_ws_server_module,
        "get_mcp_server_config",
        lambda name: {"name": name, "enabled": True, "transport": "sse", "url": "http://127.0.0.1:9000/sse"},
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "upsert_mcp_server_in_config",
        lambda payload: (payload, False),
    )
    monkeypatch.setattr(agent_ws_server_module, "get_config", lambda: {"mcp": {"servers": []}})

    async def _reload(_config, _env):
        return None

    monkeypatch.setattr(server.get_agent_manager(), "reload_agents_config", _reload)
    request = AgentRequest(
        request_id="req-mcp-update",
        channel_id="tui",
        req_method=ReqMethod.COMMAND_MCP,
        params={"action": "update", "name": "demo", "enabled": False, "url": "http://127.0.0.1:9010/sse"},
    )

    await server.handle_command_mcp_for_test(fake_ws, request, asyncio.Lock())
    assert fake_ws.sent == [
        {
            "response_id": "req-mcp-update",
            "payload": {
                "type": "updated",
                "name": "demo",
                "applied": True,
                "item": {
                    "name": "demo",
                    "enabled": False,
                    "transport": "sse",
                    "url": "http://127.0.0.1:9010/sse",
                },
            },
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_command_mcp_minimal_flow_add_list_disable(server, fake_ws, monkeypatch):
    state = {"servers": []}

    def _upsert(payload):
        state["servers"] = [item for item in state["servers"] if item.get("name") != payload.get("name")]
        state["servers"].append(dict(payload))
        return payload, True

    def _get_servers():
        return [dict(item) for item in state["servers"]]

    def _set_enabled(name, enabled):
        for item in state["servers"]:
            if item.get("name") == name:
                item["enabled"] = bool(enabled)
                return dict(item)
        raise KeyError(f"MCP server '{name}' not found")

    monkeypatch.setattr(agent_ws_server_module, "upsert_mcp_server_in_config", _upsert)
    monkeypatch.setattr(agent_ws_server_module, "get_mcp_servers", _get_servers)
    monkeypatch.setattr(agent_ws_server_module, "set_mcp_server_enabled_in_config", _set_enabled)
    monkeypatch.setattr(agent_ws_server_module, "get_config", lambda: {"mcp": {"servers": _get_servers()}})

    async def _reload(_config, _env):
        return None

    monkeypatch.setattr(server.get_agent_manager(), "reload_agents_config", _reload)

    add_req = AgentRequest(
        request_id="req-flow-add",
        channel_id="tui",
        req_method=ReqMethod.COMMAND_MCP,
        params={
            "action": "add",
            "name": "flow-demo",
            "transport": "sse",
            "url": "http://127.0.0.1:9000/sse",
        },
    )
    await server.handle_command_mcp_for_test(fake_ws, add_req, asyncio.Lock())

    list_req = AgentRequest(
        request_id="req-flow-list",
        channel_id="tui",
        req_method=ReqMethod.COMMAND_MCP,
        params={"action": "list"},
    )
    await server.handle_command_mcp_for_test(fake_ws, list_req, asyncio.Lock())

    disable_req = AgentRequest(
        request_id="req-flow-disable",
        channel_id="tui",
        req_method=ReqMethod.COMMAND_MCP,
        params={"action": "disable", "name": "flow-demo"},
    )
    await server.handle_command_mcp_for_test(fake_ws, disable_req, asyncio.Lock())

    assert fake_ws.sent[0]["payload"]["type"] == "added"
    assert fake_ws.sent[1]["payload"]["items"][0]["name"] == "flow-demo"
    assert fake_ws.sent[2]["payload"]["type"] == "disabled"
    assert fake_ws.sent[2]["payload"]["item"]["enabled"] is False


@pytest.mark.asyncio
async def test_handle_command_resume_returns_mock_session(server, fake_ws):
    request = AgentRequest(
        request_id="req-resume",
        channel_id="tui",
        req_method=ReqMethod.COMMAND_RESUME,
        params={"query": "sess_123"},
    )

    await server.handle_command_resume_for_test(fake_ws, request, asyncio.Lock())

    assert fake_ws.sent == [
        {
            "response_id": "req-resume",
            "payload": {
                "session_id": "sess_123",
                "query": "sess_123",
                "resumed": True,
                "preview": "Mock resumed conversation",
            },
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_command_session_returns_remote_handoff(server, fake_ws):
    request = AgentRequest(
        request_id="req-session",
        channel_id="tui",
        session_id="sess_demo",
        req_method=ReqMethod.COMMAND_SESSION,
        params={},
    )

    await server.handle_command_session_for_test(fake_ws, request, asyncio.Lock())

    assert fake_ws.sent == [
        {
            "response_id": "req-session",
            "payload": {
                "session_id": "sess_demo",
                "remote_url": "https://example.com/session/sess_demo",
                "qr_text": "session:sess_demo",
            },
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_permissions_config_does_not_block_on_slow_reload(server, fake_ws, monkeypatch):
    """权限 RPC 必须 fire-and-forget 重载: 即使 reload 很慢, RPC 也应立即回包,
    不再 await reload(否则会触发 AgentServer request timed out)。"""
    import time as _time

    # dispatch 直接返回 ok, 不真写盘
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_config_rpc as _rpc_mod

    class _Resp:
        ok = True
        payload = {"ok": True}

    monkeypatch.setattr(_rpc_mod, "dispatch_permissions_config_request", lambda _req: _Resp())
    # dispatch 在 _handle_permissions_config 内部是延迟 import 取的符号, 需同时 patch 该符号
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc.dispatch_permissions_config_request",
        lambda _req: _Resp(),
        raising=True,
    )
    monkeypatch.setattr(agent_ws_server_module, "get_config", lambda: {})

    reload_calls = {"n": 0}

    async def _slow_reload(_config, _env):
        reload_calls["n"] += 1
        await asyncio.sleep(0.2)  # 模拟慢 reload

    monkeypatch.setattr(server.get_agent_manager(), "reload_agents_config", _slow_reload)

    request = AgentRequest(
        request_id="req-perm-update",
        channel_id="tui",
        session_id="sess_demo",
        req_method=ReqMethod.PERMISSIONS_TOOLS_UPDATE,
        params={"tool": "bash", "level": "deny"},
    )

    t0 = _time.monotonic()
    await server.handle_permissions_config_for_test(fake_ws, request, asyncio.Lock())
    elapsed = _time.monotonic() - t0

    # RPC 立即回包: 远小于 reload 的 0.2s
    assert fake_ws.sent and fake_ws.sent[0]["ok"] is True
    assert fake_ws.sent[0]["response_id"] == "req-perm-update"
    assert elapsed < 0.2, f"RPC 被 reload 阻塞了 {elapsed:.3f}s, 期望 fire-and-forget"

    # reload 在后台被调度: 等它跑完确认调用过一次
    await asyncio.sleep(0.3)
    assert reload_calls["n"] == 1, f"期望 reload 被调用 1 次, 实际 {reload_calls['n']}"


@pytest.mark.asyncio
async def test_handle_permissions_config_routes_managed_legacy_to_service(server, fake_ws, monkeypatch):
    from jiuwenswarm.common.security_config_service import SecurityConfigService
    from jiuwenswarm.server import security_config_legacy_rpc

    monkeypatch.setattr(SecurityConfigService, "is_initialized", lambda self: True)
    routed = []

    async def managed(method, params, **kwargs):
        routed.append((method, params, kwargs["service"]))
        return {"tools": {"bash": "deny"}, "saved": True, "applied": True}

    monkeypatch.setattr(security_config_legacy_rpc, "dispatch_managed_permissions_request", managed)
    request = AgentRequest(
        request_id="managed-tool-update", channel_id="tui", session_id="sess_demo",
        req_method=ReqMethod.PERMISSIONS_TOOLS_UPDATE,
        params={"tool": "bash", "level": "deny"},
    )
    await server.handle_permissions_config_for_test(fake_ws, request, asyncio.Lock())
    assert routed[0][:2] == (ReqMethod.PERMISSIONS_TOOLS_UPDATE, request.params)
    assert isinstance(routed[0][2], SecurityConfigService)
    assert fake_ws.sent[0]["ok"] is True
    assert fake_ws.sent[0]["payload"]["tools"] == {"bash": "deny"}


@pytest.mark.asyncio
async def test_handle_permissions_config_rechecks_mode_after_waiting_for_apply_lock(server, fake_ws, monkeypatch):
    from jiuwenswarm.common.security_config_service import SecurityConfigService
    from jiuwenswarm.server import security_config_legacy_rpc

    checks = iter([False, True])
    monkeypatch.setattr(SecurityConfigService, "is_initialized", lambda self: next(checks))
    called = []

    async def managed(method, params, **_kwargs):
        called.append(method)
        return {"tools": {"bash": "deny"}, "saved": True, "applied": True}

    monkeypatch.setattr(security_config_legacy_rpc, "dispatch_managed_permissions_request", managed)
    request = AgentRequest(
        request_id="initialization-race", channel_id="tui", session_id="sess_demo",
        req_method=ReqMethod.PERMISSIONS_TOOLS_UPDATE,
        params={"tool": "bash", "level": "deny"},
    )
    await server.handle_permissions_config_for_test(fake_ws, request, asyncio.Lock())
    assert called == [ReqMethod.PERMISSIONS_TOOLS_UPDATE]
    assert fake_ws.sent[0]["ok"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("method", [ReqMethod.PERMISSIONS_FILE_GUARD_GET, ReqMethod.PERMISSIONS_NET_GUARD_GET])
async def test_guard_reads_do_not_wait_for_sandbox_rebuild(server, fake_ws, monkeypatch, managed, method):
    from jiuwenswarm.common.schema.agent import AgentResponse
    from jiuwenswarm.common.security_config_service import SecurityConfigService
    from jiuwenswarm.server import security_config_legacy_rpc
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_config_rpc

    monkeypatch.setattr(SecurityConfigService, "is_initialized", lambda self: managed)
    response = AgentResponse(request_id="guard-read", channel_id="tui", ok=True, payload={"snapshot": True})
    monkeypatch.setattr(permissions_config_rpc, "dispatch_permissions_config_request", lambda _request: response)

    async def read_managed(*_args, **_kwargs):
        return {"snapshot": True}

    monkeypatch.setattr(security_config_legacy_rpc, "dispatch_managed_permissions_request", read_managed)
    lock = server._security_config_apply_lock = asyncio.Lock()
    await lock.acquire()
    try:
        request = AgentRequest(request_id="guard-read", channel_id="tui", req_method=method, params={})
        await asyncio.wait_for(server.handle_permissions_config_for_test(fake_ws, request, asyncio.Lock()), 1)
        assert fake_ws.sent[0]["ok"] is True
        assert fake_ws.sent[0]["payload"] == {"snapshot": True}
        assert lock.locked()
    finally:
        lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,params", [
    (ReqMethod.SECURITY_CONFIG_GET, {}),
    (ReqMethod.SECURITY_CONFIG_SANDBOX_GET, {}),
    (ReqMethod.SECURITY_BUILTIN_GET, {"module": "net_guard"}),
])
async def test_security_reads_return_applying_snapshot_without_waiting(server, fake_ws, monkeypatch, method, params):
    from jiuwenswarm.common.security_config_service import SecurityConfigService
    from jiuwenswarm.server import security_config_rpc

    monkeypatch.setattr(SecurityConfigService, "is_initialized", lambda self: True)
    snapshot = {"apply": {"status": "applying", "applied": False}}
    calls = []

    async def read_security(actual_method, actual_params, **_kwargs):
        calls.append((actual_method, actual_params))
        return snapshot

    monkeypatch.setattr(security_config_rpc, "dispatch_security_config_request", read_security)
    lock = server._security_config_apply_lock = asyncio.Lock()
    await lock.acquire()
    try:
        request = AgentRequest(request_id="security-read", channel_id="tui", req_method=method, params=params)
        await asyncio.wait_for(server.handle_permissions_config_for_test(fake_ws, request, asyncio.Lock()), 1)
        assert calls == [(method, params)]
        assert fake_ws.sent[0]["ok"] is True
        assert fake_ws.sent[0]["payload"] == snapshot
        assert lock.locked()
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_guard_writes_still_wait_for_sandbox_rebuild(server, fake_ws, monkeypatch):
    from jiuwenswarm.common.security_config_service import SecurityConfigService
    from jiuwenswarm.server import security_config_legacy_rpc

    monkeypatch.setattr(SecurityConfigService, "is_initialized", lambda self: True)
    edited = asyncio.Event()

    async def write_managed(*_args, **_kwargs):
        edited.set()
        return {"saved": True, "applied": True}

    monkeypatch.setattr(security_config_legacy_rpc, "dispatch_managed_permissions_request", write_managed)
    lock = server._security_config_apply_lock = asyncio.Lock()
    await lock.acquire()
    request = AgentRequest(
        request_id="guard-write", channel_id="tui", req_method=ReqMethod.PERMISSIONS_NET_GUARD_SET,
        params={"net_guard": {"defaults": "deny"}},
    )
    task = asyncio.create_task(server.handle_permissions_config_for_test(fake_ws, request, asyncio.Lock()))
    try:
        await asyncio.sleep(0)
        assert not edited.is_set()
        assert not task.done()
    finally:
        lock.release()
        await asyncio.wait_for(task, 1)
    assert edited.is_set()
    assert fake_ws.sent[0]["ok"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [
    ReqMethod.SANDBOX_ENABLED_GET, ReqMethod.SANDBOX_STARTUP_MODE_GET,
    ReqMethod.SANDBOX_LIMITS_GET, ReqMethod.SANDBOX_FILES_GET, ReqMethod.SANDBOX_NETWORK_GET,
])
async def test_managed_sandbox_reads_do_not_wait_for_apply(server, fake_ws, monkeypatch, method):
    from jiuwenswarm.common.security_config_service import SecurityConfigService
    from jiuwenswarm.server import security_config_rpc

    monkeypatch.setattr(SecurityConfigService, "is_initialized", lambda _: True)

    async def read(actual, _params, **_kwargs):
        assert actual == method
        return {"saved_snapshot": True}

    monkeypatch.setattr(security_config_rpc, "dispatch_managed_sandbox_request", read)
    lock = server._security_config_apply_lock = asyncio.Lock()
    await lock.acquire()
    try:
        request = AgentRequest(
            request_id="sandbox-read", channel_id="tui", req_method=method, params={},
        )
        await asyncio.wait_for(
            server.handle_sandbox_config_for_test(fake_ws, request, asyncio.Lock()), 1,
        )
        assert fake_ws.sent[0]["payload"] == {"saved_snapshot": True}
        assert lock.locked()
    finally:
        lock.release()
