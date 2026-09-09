import asyncio
import json
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
        async def ensure_instance(self):
            # /compact 同 /btw：server 会先 ensure_instance 懒构建根 DeepAgent。
            return None

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
        async def ensure_instance(self):
            # /compact 同 /btw：server 会先 ensure_instance 懒构建根 DeepAgent。
            return None

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
async def test_handle_command_compact_attributes_team_work_to_live_leader(server, fake_ws, monkeypatch):
    from openjiuwen.harness import observability as harness_observability
    from jiuwenswarm.agents.harness import agent_observability
    from jiuwenswarm.agents.harness import team as team_package

    request = AgentRequest(
        request_id="req-team-compact",
        channel_id="web",
        session_id="session-team",
        req_method=ReqMethod.COMMAND_COMPACT,
        params={"mode": "team.work.normal"},
    )

    class MockAgent:
        async def ensure_instance(self):
            return None

        async def compress_context(self, session_id, *, return_state=False):
            return {"result": "noop", "stats": None}

    subject = SimpleNamespace(
        subject_id="team-member:session-team:demo:leader",
        display_name="Leader",
        kind="team_leader",
        parent_subject_id="",
        session_id="session-team",
    )
    leader = SimpleNamespace(observability_execution_subject=lambda session_id: subject)
    team_manager = SimpleNamespace(get_team_agent=lambda session_id: leader)
    captured = {}

    monkeypatch.setattr(
        server.get_agent_manager_for_test(),
        "get_agent_for_session_nowait",
        lambda channel_id, session_id: MockAgent(),
    )
    monkeypatch.setattr(team_package, "get_team_manager", lambda channel_id: team_manager)
    monkeypatch.setattr(agent_observability, "sync_agent_observability", lambda: None)
    monkeypatch.setattr(
        harness_observability,
        "open_agent_run_span",
        lambda **kwargs: captured.update(kwargs) or SimpleNamespace(),
    )
    monkeypatch.setattr(harness_observability, "close_agent_run_span", lambda *args, **kwargs: None)

    await server.handle_command_compact_for_test(fake_ws, request, asyncio.Lock())

    assert captured["execution_subject"] is subject
    assert captured["mode"] == "team.work.normal"


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
async def test_handle_command_mcp_add_triggers_reload(server, fake_ws, monkeypatch):
    monkeypatch.setattr(
        agent_ws_server_module,
        "upsert_mcp_server",
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

    monkeypatch.setattr(agent_ws_server_module, "set_mcp_server_enabled", _raise_not_found)
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
        "remove_mcp_server",
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
        "upsert_mcp_server",
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
async def test_handle_command_mcp_add_http_auth_rejected(server, fake_ws, monkeypatch):
    """HTTP add with rejected auth (401) must not persist config.yaml."""
    upsert_calls = []
    monkeypatch.setattr(
        agent_ws_server_module,
        "upsert_mcp_server",
        lambda payload: (upsert_calls.append(payload), (payload, True))[1],
    )
    monkeypatch.setattr(agent_ws_server_module, "get_config", lambda: {"mcp": {"servers": []}})

    async def _pre_check_fail(_payload):
        return False, "github (streamable-http) pre-check failed: auth rejected (HTTP 401)"

    monkeypatch.setattr(
        agent_ws_server_module.AgentWebSocketServer,
        "_pre_check_mcp_http_auth",
        staticmethod(_pre_check_fail),
    )

    called = {"reload": 0}

    async def _reload(_config, _env):
        called["reload"] += 1

    monkeypatch.setattr(server.get_agent_manager(), "reload_agents_config", _reload)
    request = AgentRequest(
        request_id="req-mcp-add-http-401",
        channel_id="web",
        req_method=ReqMethod.COMMAND_MCP,
        params={
            "action": "add",
            "name": "github",
            "transport": "streamable-http",
            "url": "https://api.githubcopilot.com/mcp",
            "headers": {"Authorization": "Bearer bad_token"},
            "timeout_s": 30,
        },
    )

    await server.handle_command_mcp_for_test(fake_ws, request, asyncio.Lock())
    assert upsert_calls == [], "config.yaml must not be written when pre-check fails"
    assert called["reload"] == 0, "reload must not run when pre-check fails"
    assert fake_ws.sent == [
        {
            "response_id": "req-mcp-add-http-401",
            "payload": {
                "type": "add_failed",
                "name": "github",
                "error": "github (streamable-http) pre-check failed: auth rejected (HTTP 401)",
            },
            "ok": False,
        }
    ]


@pytest.mark.asyncio
async def test_handle_command_mcp_add_http_timeout(server, fake_ws, monkeypatch):
    """HTTP add against a non-responding server must not persist config.yaml."""
    upsert_calls = []
    monkeypatch.setattr(
        agent_ws_server_module,
        "upsert_mcp_server",
        lambda payload: (upsert_calls.append(payload), (payload, True))[1],
    )
    monkeypatch.setattr(agent_ws_server_module, "get_config", lambda: {"mcp": {"servers": []}})

    async def _pre_check_timeout(_payload):
        return False, "stuck (streamable-http) pre-check failed: timed out after 10s (server not responding): TimeoutException"

    monkeypatch.setattr(
        agent_ws_server_module.AgentWebSocketServer,
        "_pre_check_mcp_http_auth",
        staticmethod(_pre_check_timeout),
    )

    monkeypatch.setattr(
        server.get_agent_manager(), "reload_agents_config", lambda _c, _e: None
    )
    request = AgentRequest(
        request_id="req-mcp-add-http-timeout",
        channel_id="web",
        req_method=ReqMethod.COMMAND_MCP,
        params={
            "action": "add",
            "name": "stuck",
            "transport": "http",
            "url": "http://10.255.255.1/mcp",
        },
    )

    await server.handle_command_mcp_for_test(fake_ws, request, asyncio.Lock())
    assert upsert_calls == []
    assert fake_ws.sent[0]["ok"] is False
    assert "timed out" in fake_ws.sent[0]["payload"]["error"]


@pytest.mark.asyncio
async def test_handle_command_mcp_add_http_passed(server, fake_ws, monkeypatch):
    """HTTP add that passes pre-check persists config and triggers reload."""
    upsert_calls = []
    monkeypatch.setattr(
        agent_ws_server_module,
        "upsert_mcp_server",
        lambda payload: (upsert_calls.append(payload), (payload, True))[1],
    )
    monkeypatch.setattr(agent_ws_server_module, "get_config", lambda: {"mcp": {"servers": []}})

    async def _pre_check_ok(_payload):
        return True, "github (streamable-http) pre-check passed (http 200)"

    monkeypatch.setattr(
        agent_ws_server_module.AgentWebSocketServer,
        "_pre_check_mcp_http_auth",
        staticmethod(_pre_check_ok),
    )

    called = {"reload": 0}

    async def _reload(_config, _env):
        called["reload"] += 1

    monkeypatch.setattr(server.get_agent_manager(), "reload_agents_config", _reload)
    request = AgentRequest(
        request_id="req-mcp-add-http-ok",
        channel_id="web",
        req_method=ReqMethod.COMMAND_MCP,
        params={
            "action": "add",
            "name": "github",
            "transport": "streamable-http",
            "url": "https://api.githubcopilot.com/mcp",
            "headers": {"Authorization": "Bearer good_token"},
        },
    )

    await server.handle_command_mcp_for_test(fake_ws, request, asyncio.Lock())
    assert len(upsert_calls) == 1, "config.yaml must be written when pre-check passes"
    assert called["reload"] == 1
    assert fake_ws.sent == [
        {
            "response_id": "req-mcp-add-http-ok",
            "payload": {"type": "added", "name": "github", "applied": True},
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_command_mcp_add_stdio_command_not_found(server, fake_ws, monkeypatch):
    """stdio add with a non-existent command must be rejected at config time."""
    upsert_calls = []
    monkeypatch.setattr(
        agent_ws_server_module,
        "upsert_mcp_server",
        lambda payload: (upsert_calls.append(payload), (payload, True))[1],
    )
    monkeypatch.setattr(agent_ws_server_module, "get_config", lambda: {"mcp": {"servers": []}})

    # Do NOT mock _pre_check_mcp_server — exercise the real static check
    # (shutil.which returns None for a clearly-bogus command).
    monkeypatch.setattr(
        server.get_agent_manager(), "reload_agents_config", lambda _c, _e: None
    )
    request = AgentRequest(
        request_id="req-mcp-add-stdio-badcmd",
        channel_id="tui",
        req_method=ReqMethod.COMMAND_MCP,
        params={
            "action": "add",
            "name": "broken",
            "transport": "stdio",
            "command": "nonexistent_cmd_xyz_jws",
            "args": [],
        },
    )

    await server.handle_command_mcp_for_test(fake_ws, request, asyncio.Lock())
    assert upsert_calls == [], "config.yaml must not be written when command is missing"
    assert fake_ws.sent[0]["ok"] is False
    assert fake_ws.sent[0]["payload"]["type"] == "add_failed"
    assert "command not found" in fake_ws.sent[0]["payload"]["error"]


@pytest.mark.asyncio
async def test_handle_command_mcp_update_http_auth_rejected(server, fake_ws, monkeypatch):
    """HTTP update with rejected auth (401) must not overwrite config.yaml."""
    monkeypatch.setattr(
        agent_ws_server_module,
        "get_mcp_server_config",
        lambda name: {
            "name": name,
            "enabled": True,
            "transport": "streamable-http",
            "url": "https://api.githubcopilot.com/mcp",
            "headers": {"Authorization": "Bearer old_token"},
        },
    )
    upsert_calls = []
    monkeypatch.setattr(
        agent_ws_server_module,
        "upsert_mcp_server",
        lambda payload: (upsert_calls.append(payload), (payload, False))[1],
    )
    monkeypatch.setattr(agent_ws_server_module, "get_config", lambda: {"mcp": {"servers": []}})

    async def _pre_check_fail(_payload):
        return False, "github (streamable-http) pre-check failed: auth rejected (HTTP 401)"

    monkeypatch.setattr(
        agent_ws_server_module.AgentWebSocketServer,
        "_pre_check_mcp_http_auth",
        staticmethod(_pre_check_fail),
    )

    monkeypatch.setattr(
        server.get_agent_manager(), "reload_agents_config", lambda _c, _e: None
    )
    request = AgentRequest(
        request_id="req-mcp-update-http-401",
        channel_id="web",
        req_method=ReqMethod.COMMAND_MCP,
        params={
            "action": "update",
            "name": "github",
            "headers": {"Authorization": "Bearer bad_token"},
        },
    )

    await server.handle_command_mcp_for_test(fake_ws, request, asyncio.Lock())
    assert upsert_calls == [], "config.yaml must not be overwritten when pre-check fails"
    assert fake_ws.sent == [
        {
            "response_id": "req-mcp-update-http-401",
            "payload": {
                "type": "update_failed",
                "name": "github",
                "error": "github (streamable-http) pre-check failed: auth rejected (HTTP 401)",
            },
            "ok": False,
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

    monkeypatch.setattr(agent_ws_server_module, "upsert_mcp_server", _upsert)
    monkeypatch.setattr(agent_ws_server_module, "get_mcp_servers", _get_servers)
    monkeypatch.setattr(agent_ws_server_module, "set_mcp_server_enabled", _set_enabled)
    monkeypatch.setattr(agent_ws_server_module, "get_config", lambda: {"mcp": {"servers": _get_servers()}})

    # This flow test exercises add→list→disable, not real connectivity. Mock
    # the HTTP pre-check to pass so a mock SSE endpoint (502) doesn't abort add.
    async def _pre_check_ok(_payload):
        return True, "pre-check ok (mock)"

    monkeypatch.setattr(
        agent_ws_server_module.AgentWebSocketServer,
        "_pre_check_mcp_http_auth",
        staticmethod(_pre_check_ok),
    )

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
