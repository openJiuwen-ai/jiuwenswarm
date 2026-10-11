# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""第 1 层：请求上下文、回包口、agents / 扩展 / hooks / harness 查表。"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

# 本机虚拟环境里的 openjiuwen 还没有这个模块。测试只需要配置模块能被导入。
_KV_CACHE_MOD = "openjiuwen.core.kv_cache"
if _KV_CACHE_MOD not in sys.modules:
    try:
        __import__(_KV_CACHE_MOD)
    except ModuleNotFoundError:
        _stub = types.ModuleType(_KV_CACHE_MOD)
        _stub.KVCacheAffinityConfig = type("KVCacheAffinityConfig", (), {})
        sys.modules[_KV_CACHE_MOD] = _stub

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.context import AgentServerServices, build_ws_request_context
from jiuwenswarm.server.dispatch import HANDLERS, try_dispatch
from jiuwenswarm.server.handlers import agents as agents_handlers
from jiuwenswarm.server.handlers import extensions as extensions_handlers
from jiuwenswarm.server.handlers import ops as ops_handlers
from jiuwenswarm.server.handlers import permissions as permissions_handlers


@dataclass
class _ListedAgent:
    name: str


class _Sink:
    def __init__(self) -> None:
        self.responses: list[AgentResponse] = []

    async def send_response(self, response: AgentResponse) -> None:
        self.responses.append(response)


def _request(method: ReqMethod, params: dict | None = None) -> AgentRequest:
    return AgentRequest(
        request_id="req-1",
        channel_id="web",
        req_method=method,
        params=params or {},
    )


def _ctx(method: ReqMethod, params: dict | None = None, services: object | None = None):
    sink = _Sink()
    ctx = SimpleNamespace(
        request=_request(method, params),
        sink=sink,
        connection_id="1",
        services=services or SimpleNamespace(),
    )
    return ctx, sink


class TestDispatchTable:
    def test_agents_methods_are_registered(self) -> None:
        expected = {
            ReqMethod.AGENTS_LIST,
            ReqMethod.AGENTS_GET,
            ReqMethod.AGENTS_CREATE,
            ReqMethod.AGENTS_UPDATE,
            ReqMethod.AGENTS_DELETE,
            ReqMethod.AGENTS_ENABLE,
            ReqMethod.AGENTS_DISABLE,
            ReqMethod.AGENTS_TOOLS_LIST,
        }
        assert expected <= set(HANDLERS)

    def test_extension_hook_harness_methods_are_registered(self) -> None:
        expected = {
            ReqMethod.EXTENSIONS_LIST,
            ReqMethod.EXTENSIONS_IMPORT,
            ReqMethod.EXTENSIONS_DELETE,
            ReqMethod.EXTENSIONS_TOGGLE,
            ReqMethod.HOOKS_LIST,
            ReqMethod.HARNESS_PACKAGES_GET,
            ReqMethod.HARNESS_PACKAGES_SCAN,
            ReqMethod.HARNESS_PACKAGES_ACTIVATE,
            ReqMethod.HARNESS_PACKAGES_DEACTIVATE,
            ReqMethod.HARNESS_PACKAGES_DELETE,
        }
        assert expected <= set(HANDLERS)
        assert HANDLERS[ReqMethod.EXTENSIONS_LIST] is extensions_handlers.handle_extensions_list
        assert HANDLERS[ReqMethod.HOOKS_LIST] is extensions_handlers.handle_hooks_list
        assert HANDLERS[ReqMethod.HARNESS_PACKAGES_DELETE] is extensions_handlers.handle_harness_packages_delete

    def test_permission_and_ops_methods_are_registered(self) -> None:
        permission_methods = {
            ReqMethod.PERMISSIONS_TOOLS_GET,
            ReqMethod.PERMISSIONS_TOOLS_SET,
            ReqMethod.PERMISSIONS_TOOLS_UPDATE,
            ReqMethod.PERMISSIONS_TOOLS_DELETE,
            ReqMethod.PERMISSIONS_RULES_GET,
            ReqMethod.PERMISSIONS_RULES_CREATE,
            ReqMethod.PERMISSIONS_RULES_UPDATE,
            ReqMethod.PERMISSIONS_RULES_DELETE,
            ReqMethod.PERMISSIONS_APPROVAL_OVERRIDES_GET,
            ReqMethod.PERMISSIONS_APPROVAL_OVERRIDES_DELETE,
        }
        for method in permission_methods:
            assert HANDLERS[method] is permissions_handlers.handle_permissions_config
        assert HANDLERS[ReqMethod.PROACTIVE_TICK] is ops_handlers.handle_proactive_tick
        assert HANDLERS[ReqMethod.PROACTIVE_FEEDBACK] is ops_handlers.handle_proactive_feedback
        assert HANDLERS[ReqMethod.BROWSER_RUNTIME_RESTART] is ops_handlers.handle_browser_runtime_restart
        assert HANDLERS[ReqMethod.CONFIG_CACHE_CLEAR] is ops_handlers.handle_config_cache_clear
        assert HANDLERS[ReqMethod.AGENT_RELOAD_CONFIG] is ops_handlers.handle_agent_reload_config
        assert HANDLERS[ReqMethod.AGENT_PREWARM_SYNC] is ops_handlers.handle_agent_prewarm_sync
        assert HANDLERS[ReqMethod.AUTH_CREDENTIALS_UPDATE] is ops_handlers.handle_auth_credentials_update

    def test_generic_commands_are_registered(self) -> None:
        from jiuwenswarm.server.handlers import commands as command_handlers

        registered = {
            ReqMethod.COMMAND_WORKFLOWS: command_handlers.handle_command_workflows,
            ReqMethod.COMMAND_ADD_DIR: command_handlers.handle_command_add_dir,
            ReqMethod.COMMAND_CHROME: command_handlers.handle_command_chrome,
            ReqMethod.COMMAND_COMPACT: command_handlers.handle_command_compact,
            ReqMethod.COMMAND_COMPACT_PARTIAL: command_handlers.handle_command_compact_partial,
            ReqMethod.COMMAND_CONTEXT: command_handlers.handle_command_context,
            ReqMethod.COMMAND_RECAP: command_handlers.handle_command_recap,
            ReqMethod.COMMAND_BTW: command_handlers.handle_command_btw,
            ReqMethod.COMMAND_DIFF: command_handlers.handle_command_diff,
            ReqMethod.COMMAND_SIMPLIFY: command_handlers.handle_command_simplify,
            ReqMethod.COMMAND_MODEL: command_handlers.handle_command_model,
            ReqMethod.COMMAND_RESUME: command_handlers.handle_command_resume,
            ReqMethod.COMMAND_SESSION: command_handlers.handle_command_session,
            ReqMethod.COMMAND_STATUS: command_handlers.handle_command_status,
        }
        for method, handler in registered.items():
            assert HANDLERS[method] is handler

    def test_sandbox_command_is_registered(self) -> None:
        from jiuwenswarm.server.handlers.sandbox import handle_command_sandbox

        assert HANDLERS[ReqMethod.COMMAND_SANDBOX] is handle_command_sandbox

    def test_schedule_and_issue_are_registered(self) -> None:
        from jiuwenswarm.server.handlers.schedule import handle_schedule_request

        registered = {
            ReqMethod.SCHEDULE_CHECK_CONFIG,
            ReqMethod.SCHEDULE_UPDATE_CONFIG,
            ReqMethod.SCHEDULE_CREATE,
            ReqMethod.SCHEDULE_RUN,
            ReqMethod.SCHEDULE_LIST,
            ReqMethod.SCHEDULE_STATUS,
            ReqMethod.SCHEDULE_LOGS,
            ReqMethod.SCHEDULE_CANCEL,
            ReqMethod.SCHEDULE_DELETE,
            ReqMethod.ISSUE_WATCH_ONCE,
            ReqMethod.ISSUE_STATE_LIST,
            ReqMethod.ISSUE_DELETE,
            ReqMethod.ISSUE_MATRIX,
        }
        for method in registered:
            assert HANDLERS[method] is handle_schedule_request

    def test_session_history_and_rewind_are_registered(self) -> None:
        from jiuwenswarm.server.handlers.session import (
            handle_history_append_record,
            handle_history_get,
            handle_session_input_intent,
            handle_session_list,
            handle_session_message_continue_queued,
            handle_session_plan_status,
            handle_session_rebind_project,
            handle_session_rename,
            handle_session_rewind,
            handle_session_rewind_context,
            handle_session_switch,
        )

        registered = {
            ReqMethod.SESSION_LIST: handle_session_list,
            ReqMethod.SESSION_RENAME: handle_session_rename,
            ReqMethod.SESSION_REBIND_PROJECT: handle_session_rebind_project,
            ReqMethod.SESSION_SWITCH: handle_session_switch,
            ReqMethod.SESSION_PLAN_STATUS: handle_session_plan_status,
            ReqMethod.SESSION_INPUT_INTENT: handle_session_input_intent,
            ReqMethod.SESSION_MESSAGE_CONTINUE_QUEUED: handle_session_message_continue_queued,
            ReqMethod.SESSION_REWIND: handle_session_rewind,
            ReqMethod.SESSION_REWIND_AND_RESTORE: handle_session_rewind,
            ReqMethod.SESSION_REWIND_COMPACT: handle_session_rewind,
            ReqMethod.SESSION_REWIND_CONTEXT: handle_session_rewind_context,
            ReqMethod.HISTORY_GET: handle_history_get,
            ReqMethod.HISTORY_APPEND_RECORD: handle_history_append_record,
        }
        for method, handler in registered.items():
            assert HANDLERS[method] is handler

    def test_bootstrap_methods_are_registered(self) -> None:
        from jiuwenswarm.server.handlers.bootstrap import (
            handle_initialize,
            handle_session_create,
            handle_session_fork,
        )

        assert HANDLERS[ReqMethod.INITIALIZE] is handle_initialize
        assert HANDLERS[ReqMethod.SESSION_CREATE] is handle_session_create
        assert HANDLERS[ReqMethod.SESSION_FORK] is handle_session_fork

    def test_chat_cancel_is_registered(self) -> None:
        from jiuwenswarm.server.handlers.chat import handle_chat_cancel

        assert HANDLERS[ReqMethod.CHAT_CANCEL] is handle_chat_cancel

    def test_team_and_swarmflow_control_are_registered(self) -> None:
        from jiuwenswarm.server.handlers.team import (
            handle_swarmflow_control,
            handle_team_binding_create,
            handle_team_binding_generate,
            handle_team_bindings_list,
            handle_team_delete,
            handle_team_history_get,
            handle_team_members_get,
            handle_team_mq_publish,
            handle_team_session_bind,
            handle_team_snapshot,
            handle_team_templates_list,
        )

        registered = {
            ReqMethod.TEAM_TEMPLATES_LIST: handle_team_templates_list,
            ReqMethod.TEAM_BINDINGS_LIST: handle_team_bindings_list,
            ReqMethod.TEAM_BINDING_CREATE: handle_team_binding_create,
            ReqMethod.TEAM_BINDING_GENERATE: handle_team_binding_generate,
            ReqMethod.TEAM_SESSION_BIND: handle_team_session_bind,
            ReqMethod.TEAM_DELETE: handle_team_delete,
            ReqMethod.TEAM_SNAPSHOT: handle_team_snapshot,
            ReqMethod.TEAM_MQ_PUBLISH: handle_team_mq_publish,
            ReqMethod.TEAM_HISTORY_GET: handle_team_history_get,
            ReqMethod.TEAM_MEMBERS_GET: handle_team_members_get,
            ReqMethod.SWARMFLOW_PAUSE: handle_swarmflow_control,
            ReqMethod.SWARMFLOW_RESUME: handle_swarmflow_control,
            ReqMethod.SWARMFLOW_STOP: handle_swarmflow_control,
        }
        for method, handler in registered.items():
            assert HANDLERS[method] is handler

    def test_mcp_commands_are_registered(self) -> None:
        from jiuwenswarm.server.handlers.mcp import handle_command_mcp
        from jiuwenswarm.server.handlers.mcp_hub import (
            handle_mcp_cancel_connect,
            handle_mcp_connect,
            handle_mcp_delete_custom,
            handle_mcp_disconnect,
            handle_mcp_install,
            handle_mcp_list,
            handle_mcp_register_custom,
            handle_mcp_save_credentials,
            handle_mcp_show,
            handle_mcp_uninstall,
            handle_mcp_wait_auth,
        )

        registered = {
            ReqMethod.COMMAND_MCP: handle_command_mcp,
            ReqMethod.MCP_LIST: handle_mcp_list,
            ReqMethod.MCP_SHOW: handle_mcp_show,
            ReqMethod.MCP_INSTALL: handle_mcp_install,
            ReqMethod.MCP_UNINSTALL: handle_mcp_uninstall,
            ReqMethod.MCP_CONNECT: handle_mcp_connect,
            ReqMethod.MCP_WAIT_AUTH: handle_mcp_wait_auth,
            ReqMethod.MCP_CANCEL_CONNECT: handle_mcp_cancel_connect,
            ReqMethod.MCP_DISCONNECT: handle_mcp_disconnect,
            ReqMethod.MCP_REGISTER_CUSTOM: handle_mcp_register_custom,
            ReqMethod.MCP_DELETE_CUSTOM: handle_mcp_delete_custom,
            ReqMethod.MCP_SAVE_CREDENTIALS: handle_mcp_save_credentials,
        }
        for method, handler in registered.items():
            assert HANDLERS[method] is handler

    async def test_unknown_method_falls_through(self) -> None:
        ctx, _sink = _ctx(ReqMethod.CHAT_SEND)
        assert await try_dispatch(ctx) is False

    async def test_agents_list_is_handled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Service:
            def __init__(self, workspace_dir: object) -> None:
                assert workspace_dir == "/tmp/ws"

            def list_agents(self) -> list[_ListedAgent]:
                return [_ListedAgent(name="helper")]

        _install_fake_agent_config_service(_Service)
        ctx, sink = _ctx(ReqMethod.AGENTS_LIST, {"workspace_dir": "/tmp/ws"})
        assert await try_dispatch(ctx) is True
        assert sink.responses[0].ok is True
        assert sink.responses[0].payload == {"agents": [{"name": "helper"}]}


def _install_fake_agent_config_service(service_cls: type) -> None:
    """挡住真实配置服务。它会连带拉起当前环境里缺的模块。"""

    name = "jiuwenswarm.server.runtime.agent_config_service"
    fake = types.ModuleType(name)
    fake.AgentConfigService = service_cls
    sys.modules[name] = fake


class TestAgentsEnable:
    async def test_enable_reloads_through_services(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Agent:
            source = "user"
            name = "helper"

        class _Service:
            def __init__(self, workspace_dir: object) -> None:
                _ = workspace_dir

            def get_agent(self, name: str) -> _Agent:
                assert name == "helper"
                return _Agent()

        class _Manager:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            async def reload_agents_config(self, config: object, scope: object) -> None:
                self.calls.append((config, scope))

        _install_fake_agent_config_service(_Service)
        monkeypatch.setattr(agents_handlers, "upsert_subagent_in_config", lambda name, enabled: None)
        monkeypatch.setattr(agents_handlers, "get_config", lambda: {"ok": True})
        manager = _Manager()
        ctx, sink = _ctx(
            ReqMethod.AGENTS_ENABLE,
            {"name": "helper"},
            services=SimpleNamespace(agent_manager=manager),
        )
        await agents_handlers.handle_agents_enable(ctx)
        assert sink.responses[0].payload["enabled"] is True
        assert manager.calls == [({"ok": True}, None)]


class TestWsContext:
    def test_connection_id_follows_the_socket_object(self) -> None:
        socket = object()
        server = SimpleNamespace(
            _agent_manager="manager",
            _resolve_model=lambda name: f"model:{name}",
        )
        ctx = build_ws_request_context(
            server,
            socket,
            _request(ReqMethod.AGENTS_LIST),
            asyncio.Lock(),
        )
        assert ctx.connection_id == str(id(socket))
        assert ctx.services.agent_manager == "manager"
        assert ctx.services.resolve_model("a") == "model:a"

    def test_services_do_not_expose_the_server(self) -> None:
        services = AgentServerServices(SimpleNamespace(_agent_manager=1, _resolve_model=lambda _n: None))
        assert not hasattr(services, "host")


class TestWsSink:
    async def test_send_response_uses_the_lock(self) -> None:
        sent: list[str] = []

        class _Ws:
            async def send(self, data: str) -> None:
                sent.append(data)

        lock = asyncio.Lock()
        from jiuwenswarm.server.transports.ws_sink import WSSink

        sink = WSSink(_Ws(), lock)
        await sink.send_response(
            AgentResponse(request_id="req-1", channel_id="web", ok=True, payload={"n": 1})
        )
        assert lock.locked() is False
        body = json.loads(sent[0])
        assert body.get("request_id") == "req-1" or body.get("id") == "req-1"
        assert "req-1" in sent[0]


class TestWsDispatchStillKeepsUnmovedMethods:
    def test_unmoved_methods_stay_on_the_server(self) -> None:
        from pathlib import Path

        source = Path("jiuwenswarm/server/agent_ws_server.py").read_text(encoding="utf-8")
        start = source.index("async def dispatch_parsed_request")
        end = source.index("\n    async def ", start + 1)
        body = source[start:end]
        for name in (
            "SWARMFLOW_PAUSE",
            "SWARMFLOW_RESUME",
            "SWARMFLOW_STOP",
            "TEAM_TEMPLATES_LIST",
            "TEAM_DELETE",
            "TEAM_HISTORY_GET",
            "TEAM_MEMBERS_GET",
        ):
            assert f"ReqMethod.{name}" not in body
        assert "try_dispatch" in body
        for name in (
            "async def _handle_team_delete",
            "async def _handle_team_snapshot",
            "async def _handle_swarmflow_pause",
            "async def _run_swarmflow_control",
            "async def _handle_team_history_get",
        ):
            assert name not in source
        assert "_handle_agents_list" not in source
        for name in (
            "_handle_extensions_list",
            "_handle_hooks_list",
            "_handle_harness_packages_get",
            "_handle_harness_packages_delete",
            "_handle_permissions_config",
            "_handle_agent_reload_config",
            "_handle_proactive_tick",
            "_handle_browser_runtime_restart",
            "def _harness_error_code",
        ):
            assert name not in source
        for name in ("EXTENSIONS_LIST", "HOOKS_LIST", "HARNESS_PACKAGES_DELETE"):
            assert f"ReqMethod.{name}" not in body
        assert "ReqMethod.COMMAND_MCP" not in body
        assert "ReqMethod.COMMAND_SANDBOX" not in body
        assert "ReqMethod.MCP_LIST" not in body
        assert "async def _handle_command_workflows" not in source
        assert "async def _handle_command_compact" not in source
        assert "async def _handle_command_mcp" not in source
        assert "async def _handle_command_sandbox" not in source
        assert "async def _handle_mcp_list" not in source
        assert "ReqMethod.SCHEDULE_CHECK_CONFIG" not in body
        assert "ReqMethod.ISSUE_MATRIX" not in body
        assert "async def _handle_schedule_request" not in source
        for name in (
            "SESSION_LIST",
            "SESSION_SWITCH",
            "SESSION_REWIND",
            "HISTORY_GET",
            "HISTORY_APPEND_RECORD",
        ):
            assert f"ReqMethod.{name}" not in body
        assert "async def _handle_session_switch" not in source
        assert "async def _handle_history_get" not in source
        assert "async def _handle_session_create" not in source
        assert "async def _handle_session_fork" not in source
        assert "async def _handle_initialize" not in source
        assert "ReqMethod.ACP_TOOL_RESPONSE" in source
        assert "ReqMethod.CHAT_CANCEL" not in body
        assert "ReqMethod.HEARTBEAT_JOB" in body
        assert 'startswith("rsi.")' in body
        assert "async def _handle_unary" not in source
        assert "async def _handle_stream" not in source
        assert "chat_default.handle_unary" in body
        assert "chat_default.handle_stream" in body


class TestExtensionHandlers:
    async def test_list_returns_extensions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rail_mod = types.ModuleType("jiuwenswarm.agents.harness.common.plugins.rail_manager")

        class _Manager:
            def list_extensions(self) -> list[dict]:
                return [{"name": "demo"}]

        rail_mod.get_rail_manager = lambda: _Manager()
        monkeypatch.setitem(
            sys.modules,
            "jiuwenswarm.agents.harness.common.plugins.rail_manager",
            rail_mod,
        )

        ctx, sink = _ctx(ReqMethod.EXTENSIONS_LIST)
        await extensions_handlers.handle_extensions_list(ctx)
        assert sink.responses[0].ok is True
        assert sink.responses[0].payload == {"extensions": [{"name": "demo"}]}

    async def test_delete_native_package_is_rejected(self) -> None:
        ctx, sink = _ctx(ReqMethod.HARNESS_PACKAGES_DELETE, {"package_id": "native"})
        await extensions_handlers.handle_harness_packages_delete(ctx)
        assert sink.responses[0].ok is False
        assert sink.responses[0].payload["code"] == "BAD_REQUEST"

    def test_harness_error_code_mapping(self) -> None:
        assert extensions_handlers._harness_error_code(ValueError("already active")) == "CONFLICT"
        assert extensions_handlers._harness_error_code(ValueError("package not found")) == "NOT_FOUND"
        assert extensions_handlers._harness_error_code(ValueError("native package")) == "BAD_REQUEST"


def _install_fake_permissions_rpc(monkeypatch: pytest.MonkeyPatch, dispatch) -> None:
    """挡住会拉起重依赖的权限包，只留这次测试要用的分发函数。"""
    rpc_name = "jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc"
    for name in (
        "jiuwenswarm.agents.harness.common.rails",
        "jiuwenswarm.agents.harness.common.rails.permissions",
    ):
        if name not in sys.modules:
            package = types.ModuleType(name)
            package.__path__ = []
            monkeypatch.setitem(sys.modules, name, package)
    rpc = types.ModuleType(rpc_name)
    rpc.dispatch_permissions_config_request = dispatch
    monkeypatch.setitem(sys.modules, rpc_name, rpc)


class TestPermissionAndOpsHandlers:
    async def test_read_only_permission_does_not_reload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[object] = []

        def _dispatch(request: AgentRequest) -> AgentResponse:
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"tools": []},
            )

        _install_fake_permissions_rpc(monkeypatch, _dispatch)
        services = SimpleNamespace(
            agent_manager=SimpleNamespace(
                schedule_permissions_reload=lambda config: calls.append(config)
            )
        )
        ctx, sink = _ctx(ReqMethod.PERMISSIONS_TOOLS_GET, services=services)
        await permissions_handlers.handle_permissions_config(ctx)
        assert sink.responses[0].ok is True
        assert calls == []

    async def test_permission_update_schedules_reload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[object] = []

        def _dispatch(request: AgentRequest) -> AgentResponse:
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"updated": True},
            )

        _install_fake_permissions_rpc(monkeypatch, _dispatch)
        monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: {"captured": True})
        services = SimpleNamespace(
            agent_manager=SimpleNamespace(
                schedule_permissions_reload=lambda config: calls.append(config)
            )
        )
        ctx, sink = _ctx(ReqMethod.PERMISSIONS_TOOLS_UPDATE, services=services)
        await permissions_handlers.handle_permissions_config(ctx)
        assert sink.responses[0].ok is True
        assert calls == [{"captured": True}]

    async def test_prewarm_rejects_a_missing_channel_list(self) -> None:
        ctx, sink = _ctx(ReqMethod.AGENT_PREWARM_SYNC, {})
        await ops_handlers.handle_agent_prewarm_sync(ctx)
        assert sink.responses[0].ok is False
        assert sink.responses[0].payload["code"] == "BAD_REQUEST"

    async def test_proactive_tick_reports_missing_engine(self) -> None:
        ctx, sink = _ctx(
            ReqMethod.PROACTIVE_TICK,
            services=SimpleNamespace(proactive_engine=None),
        )
        await ops_handlers.handle_proactive_tick(ctx)
        assert sink.responses[0].ok is False
        assert "not initialized" in sink.responses[0].payload["error"]

    async def test_resume_returns_the_requested_session(self) -> None:
        from jiuwenswarm.server.handlers import commands as command_handlers

        ctx, sink = _ctx(ReqMethod.COMMAND_RESUME, {"query": "sess_demo"})
        await command_handlers.handle_command_resume(ctx)
        assert sink.responses[0].ok is True
        assert sink.responses[0].payload["session_id"] == "sess_demo"
        assert sink.responses[0].payload["resumed"] is True


class TestSandboxHandler:
    async def test_unknown_sub_is_a_bad_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from jiuwenswarm.server.handlers import sandbox as sandbox_handlers

        monkeypatch.setattr(sandbox_handlers, "_require_sandbox_supported", lambda: None)
        monkeypatch.setattr(
            sandbox_handlers,
            "get_sandbox_endpoint",
            lambda: {"type": "jiuwenbox"},
        )
        monkeypatch.setattr(sandbox_handlers, "get_sandbox_runtime", lambda: {"files": {}})
        monkeypatch.setattr(sandbox_handlers, "validate_sandbox_files_runtime", lambda _files: None)
        ctx, sink = _ctx(ReqMethod.COMMAND_SANDBOX, {"sub": "nope"})
        await sandbox_handlers.handle_command_sandbox(ctx)
        assert sink.responses[0].ok is False
        assert sink.responses[0].payload["code"] == "SANDBOX_BAD_REQUEST"
        assert "unknown sub" in sink.responses[0].payload["error"]

    def test_non_linux_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from jiuwenswarm.server.handlers import sandbox as sandbox_handlers

        monkeypatch.setattr(sandbox_handlers.sys, "platform", "win32")
        with pytest.raises(ValueError, match="only supported on Linux"):
            sandbox_handlers._require_sandbox_supported()


class TestScheduleHandler:
    async def test_check_config_uses_the_existing_scheduler(self) -> None:
        from jiuwenswarm.server.handlers.schedule import handle_schedule_request

        class _Scheduler:
            def check_schedule_config(self) -> dict[str, bool]:
                return {"enabled": True}

        ctx, sink = _ctx(
            ReqMethod.SCHEDULE_CHECK_CONFIG,
            services=SimpleNamespace(scheduler_service=_Scheduler()),
        )
        await handle_schedule_request(ctx)
        assert sink.responses[0].ok is True
        assert sink.responses[0].payload == {"enabled": True}

    async def test_issue_matrix_forwards_params(self) -> None:
        from jiuwenswarm.server.handlers.schedule import handle_schedule_request

        seen: dict[str, object] = {}

        class _Scheduler:
            async def refresh_issue_matrix(self, params: dict) -> dict[str, object]:
                seen["params"] = params
                return {"rows": 2}

        ctx, sink = _ctx(
            ReqMethod.ISSUE_MATRIX,
            {"repo": "demo"},
            services=SimpleNamespace(scheduler_service=_Scheduler()),
        )
        await handle_schedule_request(ctx)
        assert sink.responses[0].ok is True
        assert sink.responses[0].payload == {"rows": 2}
        assert seen["params"] == {"repo": "demo"}
