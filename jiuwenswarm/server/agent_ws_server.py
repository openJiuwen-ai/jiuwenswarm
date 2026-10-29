# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compatibility WebSocket transport over the resident execution host.

Worker execution and domain management are owned by WorkerExecutionHost. This
adapter only adds the historical listen socket, framing, Origin checks and
restartable singleton surface. Production Workers never import this module.
"""

from __future__ import annotations

import asyncio
import json
import logging
from importlib import import_module
from typing import Any, ClassVar

from websockets.exceptions import ConnectionClosed as WebSocketConnectionClosed

from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.e2a.constants import E2A_WIRE_INTERNAL_METADATA_KEYS
from jiuwenswarm.common.e2a.gateway_normalize import (
    E2A_FALLBACK_FAILED_KEY,
    E2A_INTERNAL_CONTEXT_KEY,
    E2A_LEGACY_AGENT_REQUEST_KEY,
)
from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.e2a.wire_codec import encode_json_parse_error_wire
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from gateway.common.security.ws_origin import (
    extract_handshake_request,
    forbidden_origin_response,
    get_header_value,
    is_allowed_browser_origin,
    is_origin_check_enabled,
)
from gateway.common.session_message import SESSION_MESSAGE_INTERNAL_KEY
from gateway.common.ws_diagnostics import (
    describe_ws_exception,
    describe_ws_peer,
    format_ws_diagnostics,
)
from gateway.common.ws_limits import AGENT_WS_MAX_MESSAGE_BYTES
from jiuwenswarm.runtime.plan import PlanModeController
from jiuwenswarm.server.worker.execution_host import WorkerExecutionHost
from jiuwenswarm.server.worker.execution_services import ProductionHostServices, TransportClosed
from jiuwenswarm.server.ws_send import send_wire_payload

logger = logging.getLogger(__name__)
_DIAGNOSIS_REQ_METHODS = frozenset(
    method for method in ReqMethod if method.value.startswith("diagnosis.")
)
_SERVER_PLAN_CONTROLLER = PlanModeController()
_session_mode_sync_locks = _SERVER_PLAN_CONTROLLER.sync_locks
_plan_exited_sessions = _SERVER_PLAN_CONTROLLER.exited_sessions
_plan_active_sessions = _SERVER_PLAN_CONTROLLER.active_sessions


def _renew_server_plan_controller() -> PlanModeController:
    """Create the plan-state owner for the next AgentServer lifecycle.

    The compatibility aliases are rebound together so handlers and diagnostics
    cannot retain state from the Runtime that has just been closed.
    """
    global _SERVER_PLAN_CONTROLLER
    global _session_mode_sync_locks
    global _plan_exited_sessions
    global _plan_active_sessions

    controller = PlanModeController()
    _SERVER_PLAN_CONTROLLER = controller
    _session_mode_sync_locks = controller.sync_locks
    _plan_exited_sessions = controller.exited_sessions
    _plan_active_sessions = controller.active_sessions
    return controller


def _inject_plan_mode_activation_reminder(request: AgentRequest) -> None:
    """Retain the historical import surface of the shared plan controller."""
    PlanModeController.inject_activation_reminder(request)


def _parse_single_byte_range(
    range_header: str, file_size: int
) -> tuple[int, int] | None:
    """Parse a single HTTP byte range for file-download bridge handlers."""
    if file_size <= 0:
        return None
    if not range_header.startswith("bytes="):
        return None
    if "," in range_header:
        return None
    start_text, end_text = (
        range_header[6:].split("-", 1) if "-" in range_header[6:] else ("", "")
    )
    if not start_text:
        if not end_text.isdecimal() or int(end_text) <= 0:
            return None
        return (max(0, file_size - int(end_text)), file_size - 1)
    if not start_text.isdecimal() or (end_text and (not end_text.isdecimal())):
        return None
    start = int(start_text)
    if start >= file_size:
        return None
    end = min(int(end_text), file_size - 1) if end_text else file_size - 1
    return (start, end) if end >= start else None


def _strip_untrusted_session_message_context(request: AgentRequest) -> None:
    """Remove the private origin marker from every Gateway-supplied request."""
    request.trusted_session_message_route = None
    if isinstance(request.params, dict):
        params = dict(request.params)
        params.pop(SESSION_MESSAGE_INTERNAL_KEY, None)
        if isinstance(params.get("metadata"), dict):
            nested_metadata = dict(params["metadata"])
            nested_metadata.pop(SESSION_MESSAGE_INTERNAL_KEY, None)
            params["metadata"] = nested_metadata
        request.params = params
    if isinstance(request.metadata, dict):
        metadata = dict(request.metadata)
        metadata.pop(SESSION_MESSAGE_INTERNAL_KEY, None)
        request.metadata = metadata


def _payload_to_request(data: dict[str, Any]) -> AgentRequest:
    """将 Gateway 发送的 JSON 载荷解析为 AgentRequest."""
    req_method = data.get("req_method")
    if req_method is not None and isinstance(req_method, str):
        req_method = ReqMethod(req_method)
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        metadata = {
            key: value
            for key, value in metadata.items()
            if key not in E2A_WIRE_INTERNAL_METADATA_KEYS
        } or None
    app_id = data.get("app_id")
    if app_id:
        if metadata is None:
            metadata = {}
        metadata.setdefault("app_id", app_id)
    return AgentRequest(
        request_id=data["request_id"],
        channel_id=data.get("channel_id", "web"),
        session_id=data.get("session_id"),
        req_method=req_method,
        params=data.get("params", {}),
        is_stream=data.get("is_stream", False),
        timestamp=data.get("timestamp", 0.0),
        metadata=metadata,
        user_id=str(data.get("user_id") or "").strip(),
        agent_ref=data.get("agent_ref"),
    )


def _compat_binding(name: str) -> Any:
    value = globals().get(name)
    return value if value is not None else __getattr__(name)


class _CompatibilityHostServices(ProductionHostServices):
    """Keep the legacy socket's integration ports outside the execution host."""

    def encode_response(self, response: Any, *, response_id: str, sequence: int = 0) -> dict[str, Any]:
        encode = _compat_binding("encode_agent_response_for_wire")
        if sequence:
            return encode(response, response_id=response_id, sequence=sequence)
        return encode(response, response_id=response_id)

    def encode_chunk(self, chunk: Any, *, response_id: str, sequence: int, is_stream: bool = True) -> dict[str, Any]:
        encode = _compat_binding("encode_agent_chunk_for_wire")
        if not is_stream:
            return encode(chunk, response_id=response_id, sequence=sequence, is_stream=False)
        return encode(chunk, response_id=response_id, sequence=sequence)

    async def send(self, sink: Any, payload: dict[str, Any]) -> bool:
        try:
            return await send_wire_payload(sink, payload)
        except WebSocketConnectionClosed as exc:
            raise TransportClosed(str(exc)) from exc


class AgentWebSocketServer(WorkerExecutionHost):
    """Historical Gateway socket API backed by the same execution owner."""

    _instance: ClassVar[AgentWebSocketServer | None] = None

    @property
    def services(self) -> Any:
        value = getattr(self, "_host_services", None)
        if value is None:
            value = self._host_services = _CompatibilityHostServices()
        return value

    @property
    def _plan_controller(self) -> PlanModeController:
        return self.__dict__.get("_compat_plan_controller", _SERVER_PLAN_CONTROLLER)

    @_plan_controller.setter
    def _plan_controller(self, value: PlanModeController) -> None:
        self._compat_plan_controller = value

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 18000,
        *,
        ping_interval: float | None = 30.0,
        ping_timeout: float | None = 300.0,
        services: Any = None,
    ) -> None:
        super().__init__(
            host, port, services=services if services is not None else _CompatibilityHostServices(),
            plan_controller=_SERVER_PLAN_CONTROLLER,
        )
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout

    async def _bind_transport(self) -> None:
        """Only this compatibility adapter can allocate a listening socket."""
        try:
            from websockets.legacy.server import serve
        except ImportError:
            from websockets import serve
        self._server = await serve(
            self._connection_handler,
            self._host,
            self._port,
            process_request=self._process_request,
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_timeout,
            max_size=AGENT_WS_MAX_MESSAGE_BYTES,
        )

    async def stop(self) -> None:
        """Preserve legacy restart without changing one-shot Worker ownership."""
        closing_runtime = self._runtime
        try:
            await super().stop()
        finally:
            # Close may finish persistence and then propagate cancellation.
            # Only a completed close permits the compatibility endpoint to
            # restart; rejected close keeps the same owner for a later retry.
            if closing_runtime.closed:
                _renew_server_plan_controller()
                AgentWebSocketServer.__init__(
                    self,
                    self._host,
                    self._port,
                    ping_interval=self._ping_interval,
                    ping_timeout=self._ping_timeout,
                    services=self.services,
                )

    @staticmethod
    def _session_mode_sync_lock(session_id: str) -> asyncio.Lock:
        return _SERVER_PLAN_CONTROLLER.lock_for(session_id)

    @classmethod
    def get_instance(
        cls,
        *,
        host: str = "127.0.0.1",
        port: int = 18000,
        ping_interval: float | None = 30.0,
        ping_timeout: float | None = 300.0,
    ) -> "AgentWebSocketServer":
        """返回单例实例。

        首次调用时创建实例，后续调用返回已存在的实例。
        """
        if cls._instance is not None:
            return cls._instance
        cls._instance = cls(
            host=host, port=port, ping_interval=ping_interval, ping_timeout=ping_timeout
        )
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（仅用于测试）。"""
        cls._instance = None

    async def on_gateway_disconnect(self, ws: Any, remote: Any) -> None:
        """Runtime-side cleanup when Front drops the Gateway connection."""
        owns_current_connection = self._current_ws is ws
        if owns_current_connection:
            self._current_ws = None
            self._current_send_lock = None
            await self._session_message_service.set_available(False)
        self._clear_ws_acp_client_capabilities(ws)
        try:
            await self._execution_runtime().cancel_all_inflight_work(
                reason=f"[gateway ws closed {remote}] ",
                exclude_session_ids=self._heartbeat_runtime.execution.active_session_ids(),
            )
        except Exception:
            logger.exception("[AgentWebSocketServer] cancel_all_inflight_work failed")
        try:
            await self._stop_scheduler()
        except Exception:
            logger.exception("[AgentWebSocketServer] scheduler stop failed")
        await self._stop_expert_streams()
        self._session_stream_tasks.clear()

    async def _process_request(self, *args: Any) -> Any:
        """在握手阶段执行 Origin 校验，兼容 legacy/new websockets APIs。"""
        path, request_headers = extract_handshake_request(args)
        origin = get_header_value(request_headers, "Origin")
        enable_origin_check = is_origin_check_enabled()
        if not enable_origin_check:
            logger.info(
                "[AgentWebSocketServer] 握手检查 path=%s origin=%s enable_origin_check=%s allowed=%s",
                path,
                origin,
                enable_origin_check,
                True,
            )
            return None
        allowed = is_allowed_browser_origin(origin)
        logger.info(
            "[AgentWebSocketServer] 握手检查 path=%s origin=%s enable_origin_check=%s allowed=%s",
            path,
            origin,
            enable_origin_check,
            allowed,
        )
        if allowed:
            return None
        logger.warning(
            "[AgentWebSocketServer] 握手拒绝 path=%s origin=%s reason=origin_not_allowed",
            path,
            origin,
        )
        return forbidden_origin_response(args)

    async def _connection_handler(self, ws: Any) -> None:
        """处理单个 Gateway WebSocket 连接，同一连接可并发处理多个请求."""
        remote = ws.remote_address
        logger.info("[AgentWebSocketServer] 新连接: %s", remote)
        send_lock = asyncio.Lock()
        self._current_ws = ws
        self._current_send_lock = send_lock
        session_message_service = getattr(self, "_session_message_service", None)
        try:
            ack_frame = {
                "type": "event",
                "event": "connection.ack",
                "payload": {
                    "status": "ready",
                    "heartbeat_job_owner": "agentserver",
                    "heartbeat_job_protocol": self._heartbeat_runtime.protocol_version,
                    "heartbeat_job_ready": self._heartbeat_runtime.is_available,
                },
            }
            await send_wire_payload(ws, ack_frame)
            if session_message_service is not None:
                await session_message_service.set_available(True)
            logger.info("[AgentWebSocketServer] 已发送 connection.ack: %s", remote)
        except Exception as e:
            logger.warning("[AgentWebSocketServer] 发送 connection.ack 失败: %s", e)
        tasks: set[asyncio.Task] = set()
        try:
            async for raw in ws:
                task = asyncio.create_task(self._handle_message(ws, raw, send_lock))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except WebSocketConnectionClosed as e:
            logger.info(
                "[AgentWebSocketServer] 连接关闭: %s",
                format_ws_diagnostics(
                    {
                        "remote": remote,
                        "active_tasks": len(tasks),
                        "session_stream_tasks": len(self._session_stream_tasks),
                        "ping_interval": self._ping_interval,
                        "ping_timeout": self._ping_timeout,
                    },
                    describe_ws_peer(ws),
                    describe_ws_exception(e),
                ),
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] 连接处理异常 (%s): %s", remote, e)
        finally:
            owns_current_connection = self._release_current_connection(ws)
            if owns_current_connection and session_message_service is not None:
                await session_message_service.set_available(False)
            self._clear_ws_acp_client_capabilities(ws)
            connection_tasks = list(tasks)
            for task in connection_tasks:
                if not task.done():
                    task.cancel()
            if owns_current_connection:
                try:
                    await self._execution_runtime().cancel_all_inflight_work(
                        reason=f"[gateway ws closed {remote}] ",
                        exclude_session_ids=self._heartbeat_runtime.execution.active_session_ids(),
                    )
                except Exception:
                    logger.exception(
                        "[AgentWebSocketServer] cancel_all_inflight_work failed"
                    )
                try:
                    await self._stop_scheduler()
                except Exception:
                    logger.exception("[AgentWebSocketServer] scheduler stop failed")
            if connection_tasks:
                await asyncio.gather(*connection_tasks, return_exceptions=True)
            if owns_current_connection:
                await self._stop_expert_streams()
                self._session_stream_tasks.clear()

    async def _handle_message(
        self, ws: Any, raw: str | bytes, send_lock: asyncio.Lock
    ) -> None:
        """解析一条 JSON 请求并分发到 IAgentServer 处理."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            wire = encode_json_parse_error_wire(
                request_id="", channel_id="", message=f"JSON 解析失败: {e}"
            )
            try:
                async with send_lock:
                    await send_wire_payload(ws, wire)
            except WebSocketConnectionClosed as send_exc:
                logger.info(
                    "[AgentWebSocketServer] WebSocket 已关闭，JSON 解析错误未发送: %s",
                    format_ws_diagnostics(
                        {"json_error": str(e)},
                        describe_ws_peer(ws),
                        describe_ws_exception(send_exc),
                    ),
                )
            return
        try:
            env = E2AEnvelope.from_dict(data)
        except Exception as parse_err:
            logger.warning(
                "[AgentWebSocketServer] E2A from_dict 失败，按旧载荷解析: %s", parse_err
            )
            request = _payload_to_request(data)
        else:
            jw = (env.channel_context or {}).get(E2A_INTERNAL_CONTEXT_KEY)
            if isinstance(jw, dict) and jw.get(E2A_FALLBACK_FAILED_KEY):
                legacy = jw.get(E2A_LEGACY_AGENT_REQUEST_KEY)
                logger.warning(
                    "[E2A][fallback] using legacy_agent_request request_id=%s",
                    env.request_id,
                )
                if not isinstance(legacy, dict):
                    raise ValueError("legacy_agent_request missing or not a dict")
                request = _payload_to_request(legacy)
            else:
                logger.info(
                    "[E2A][in] request_id=%s channel=%s method=%s is_stream=%s",
                    env.request_id,
                    env.channel,
                    env.method,
                    env.is_stream,
                )
                request = e2a_to_agent_request(env)
        _strip_untrusted_session_message_context(request)
        logger.info(
            "[AgentWebSocketServer] 收到请求: request_id=%s channel_id=%s is_stream=%s",
            request.request_id,
            request.channel_id,
            request.is_stream,
        )
        await self.dispatch_parsed_request(ws, request, send_lock)


# Public import compatibility is lazy; no duplicate business implementation
# lives in this transport module. These names resolve to their actual owners.
_COMPAT_EXPORTS = {
    "WorkerExecutionHost": (
        "jiuwenswarm.server.worker.execution_host",
        "WorkerExecutionHost",
    ),
    "ExecutionRequests": (
        "jiuwenswarm.server.worker.execution_requests",
        "ExecutionRequests",
    ),
    "asyncio": ("asyncio", None),
    "_dt": ("datetime", None),
    "inspect": ("inspect", None),
    "json": ("json", None),
    "logging": ("logging", None),
    "math": ("math", None),
    "os": ("os", None),
    "shutil": ("shutil", None),
    "sys": ("sys", None),
    "dataclass": ("dataclasses", "dataclass"),
    "time": ("time", None),
    "Path": ("pathlib", "Path"),
    "Any": ("typing", "Any"),
    "ClassVar": ("typing", "ClassVar"),
    "NamedTuple": ("typing", "NamedTuple"),
    "Optional": ("typing", "Optional"),
    "WeakValueDictionary": ("weakref", "WeakValueDictionary"),
    "server_logger": ("openjiuwen.core.common.logging", "server_logger"),
    "WebSocketConnectionClosed": ("websockets.exceptions", "ConnectionClosed"),
    "run_history_io": (
        "jiuwenswarm.server.runtime.session.history_io",
        "run_history_io",
    ),
    "reset_harness_packages_state": (
        "jiuwenswarm.agents.harness.common.auto_harness",
        "reset_harness_packages_state",
    ),
    "HeartbeatRailRuntime": (
        "jiuwenswarm.agents.harness.code.rails.heartbeat.runtime",
        "HeartbeatRailRuntime",
    ),
    "build_server_push_wire": (
        "jiuwenswarm.server.gateway_push.wire",
        "build_server_push_wire",
    ),
    "send_wire_payload": ("jiuwenswarm.server.ws_send", "send_wire_payload"),
    "get_acp_output_manager": (
        "jiuwenswarm.agents.harness.common.tools.acp_output_tools",
        "get_acp_output_manager",
    ),
    "get_user_workspace_dir": ("jiuwenswarm.common.utils", "get_user_workspace_dir"),
    "get_agent_root_dir": ("jiuwenswarm.common.utils", "get_agent_root_dir"),
    "get_agent_sessions_dir": ("jiuwenswarm.common.utils", "get_agent_sessions_dir"),
    "get_config_file": ("jiuwenswarm.common.utils", "get_config_file"),
    "mask_sensitive": ("jiuwenswarm.common.utils", "mask_sensitive"),
    "SESSION_MESSAGE_INTERNAL_KEY": (
        "gateway.common.session_message",
        "SESSION_MESSAGE_INTERNAL_KEY",
    ),
    "SESSION_MESSAGE_ORIGIN": (
        "gateway.common.session_message",
        "SESSION_MESSAGE_ORIGIN",
    ),
    "SESSION_MESSAGE_OWNER_SCOPE_METADATA_KEY": (
        "gateway.common.session_message",
        "SESSION_MESSAGE_OWNER_SCOPE_METADATA_KEY",
    ),
    "SESSION_MESSAGE_ANONYMOUS_OWNER_METADATA_KEY": (
        "gateway.common.session_message",
        "SESSION_MESSAGE_ANONYMOUS_OWNER_METADATA_KEY",
    ),
    "load_todo_snapshot_for_frontend": (
        "jiuwenswarm.common.todo_snapshot",
        "load_todo_snapshot_for_frontend",
    ),
    "e2a_to_agent_request": (
        "jiuwenswarm.common.e2a.agent_compat",
        "e2a_to_agent_request",
    ),
    "E2A_CANCEL_SOURCE_CLIENT_DISCONNECT": (
        "jiuwenswarm.common.e2a.constants",
        "E2A_CANCEL_SOURCE_CLIENT_DISCONNECT",
    ),
    "E2A_INTERNAL_CANCEL_SOURCE_KEY": (
        "jiuwenswarm.common.e2a.constants",
        "E2A_INTERNAL_CANCEL_SOURCE_KEY",
    ),
    "E2A_WIRE_INTERNAL_METADATA_KEYS": (
        "jiuwenswarm.common.e2a.constants",
        "E2A_WIRE_INTERNAL_METADATA_KEYS",
    ),
    "E2A_FALLBACK_FAILED_KEY": (
        "jiuwenswarm.common.e2a.gateway_normalize",
        "E2A_FALLBACK_FAILED_KEY",
    ),
    "E2A_INTERNAL_CONTEXT_KEY": (
        "jiuwenswarm.common.e2a.gateway_normalize",
        "E2A_INTERNAL_CONTEXT_KEY",
    ),
    "E2A_LEGACY_AGENT_REQUEST_KEY": (
        "jiuwenswarm.common.e2a.gateway_normalize",
        "E2A_LEGACY_AGENT_REQUEST_KEY",
    ),
    "E2AEnvelope": ("jiuwenswarm.common.e2a.models", "E2AEnvelope"),
    "encode_agent_chunk_for_wire": (
        "jiuwenswarm.common.e2a.wire_codec",
        "encode_agent_chunk_for_wire",
    ),
    "encode_agent_response_for_wire": (
        "jiuwenswarm.common.e2a.wire_codec",
        "encode_agent_response_for_wire",
    ),
    "encode_json_parse_error_wire": (
        "jiuwenswarm.common.e2a.wire_codec",
        "encode_json_parse_error_wire",
    ),
    "is_placeholder_api_base": (
        "jiuwenswarm.common.model_config_validation",
        "is_placeholder_api_base",
    ),
    "AgentRequest": ("jiuwenswarm.common.schema.agent", "AgentRequest"),
    "AgentResponse": ("jiuwenswarm.common.schema.agent", "AgentResponse"),
    "AgentResponseChunk": ("jiuwenswarm.common.schema.agent", "AgentResponseChunk"),
    "get_runtime_version": ("jiuwenswarm.common.version", "get_runtime_version"),
    "describe_ws_exception": (
        "gateway.common.ws_diagnostics",
        "describe_ws_exception",
    ),
    "describe_ws_peer": ("gateway.common.ws_diagnostics", "describe_ws_peer"),
    "format_ws_diagnostics": (
        "gateway.common.ws_diagnostics",
        "format_ws_diagnostics",
    ),
    "AGENT_WS_MAX_MESSAGE_BYTES": (
        "gateway.common.ws_limits",
        "AGENT_WS_MAX_MESSAGE_BYTES",
    ),
    "AgentServerHookEvents": (
        "jiuwenswarm.common.hook_event",
        "AgentServerHookEvents",
    ),
    "get_rail_manager": (
        "jiuwenswarm.agents.harness.common.plugins.rail_manager",
        "get_rail_manager",
    ),
    "persist_cli_trusted_directory": (
        "jiuwenswarm.agents.harness.common.rails.permissions.permissions_persist",
        "persist_cli_trusted_directory",
    ),
    "AgentServerChatHookContext": (
        "jiuwenswarm.common.hooks_context",
        "AgentServerChatHookContext",
    ),
    "AgentManager": ("jiuwenswarm.server.runtime.agent_manager", "AgentManager"),
    "ACP_DEFAULT_CAPABILITIES": (
        "jiuwenswarm.server.runtime.agent_manager",
        "ACP_DEFAULT_CAPABILITIES",
    ),
    "AgentRuntime": ("jiuwenswarm.runtime", "AgentRuntime"),
    "SessionCreateInput": (
        "jiuwenswarm.runtime.session_provisioner",
        "SessionCreateInput",
    ),
    "SessionForkInput": ("jiuwenswarm.runtime.session_provisioner", "SessionForkInput"),
    "SessionProvisionCommitContext": (
        "jiuwenswarm.runtime.session_provisioner",
        "SessionProvisionCommitContext",
    ),
    "SessionProvisionCommitTiming": (
        "jiuwenswarm.runtime.session_provisioner",
        "SessionProvisionCommitTiming",
    ),
    "SessionProvisionError": (
        "jiuwenswarm.runtime.session_provisioner",
        "SessionProvisionError",
    ),
    "SessionProvisionState": (
        "jiuwenswarm.runtime.session_provisioner",
        "SessionProvisionState",
    ),
    "SessionSwitchInput": (
        "jiuwenswarm.runtime.session_provisioner",
        "SessionSwitchInput",
    ),
    "TokenizerService": (
        "jiuwenswarm.server.runtime.tokenizer_service",
        "TokenizerService",
    ),
    "build_server_push_message": (
        "jiuwenswarm.server.runtime.session.session_metadata",
        "build_server_push_message",
    ),
    "get_all_sessions_metadata": (
        "jiuwenswarm.server.runtime.session.session_metadata",
        "get_all_sessions_metadata",
    ),
    "get_session_metadata": (
        "jiuwenswarm.server.runtime.session.session_metadata",
        "get_session_metadata",
    ),
    "SessionMessageExecutionResult": (
        "jiuwenswarm.server.runtime.session.session_message_service",
        "SessionMessageExecutionResult",
    ),
    "SessionMessageService": (
        "jiuwenswarm.server.runtime.session.session_message_service",
        "SessionMessageService",
    ),
    "SessionMessagingError": (
        "jiuwenswarm.server.runtime.session.session_message_service",
        "SessionMessagingError",
    ),
    "SessionMessageRecord": (
        "jiuwenswarm.server.runtime.session.session_message_store",
        "SessionMessageRecord",
    ),
    "SessionMessageStore": (
        "jiuwenswarm.server.runtime.session.session_message_store",
        "SessionMessageStore",
    ),
    "HistorySnapshotChanged": (
        "jiuwenswarm.server.runtime.session.session_history",
        "HistorySnapshotChanged",
    ),
    "InvalidHistoryCursor": (
        "jiuwenswarm.server.runtime.session.session_history",
        "InvalidHistoryCursor",
    ),
    "append_compact_history_records": (
        "jiuwenswarm.server.runtime.session.session_history",
        "append_compact_history_records",
    ),
    "append_history_record": (
        "jiuwenswarm.server.runtime.session.session_history",
        "append_history_record",
    ),
    "enqueue_history_request_completion": (
        "jiuwenswarm.server.runtime.session.session_history",
        "enqueue_history_request_completion",
    ),
    "history_exists": (
        "jiuwenswarm.server.runtime.session.session_history",
        "history_exists",
    ),
    "is_valid_session_id": (
        "jiuwenswarm.server.runtime.session.session_history",
        "is_valid_session_id",
    ),
    "load_history_records": (
        "jiuwenswarm.server.runtime.session.session_history",
        "load_history_records",
    ),
    "read_history_cursor_page": (
        "jiuwenswarm.server.runtime.session.session_history",
        "read_history_cursor_page",
    ),
    "read_member_history_records": (
        "jiuwenswarm.server.runtime.session.session_history",
        "read_member_history_records",
    ),
    "read_team_history_records": (
        "jiuwenswarm.server.runtime.session.session_history",
        "read_team_history_records",
    ),
    "wait_for_history_receipt": (
        "jiuwenswarm.server.runtime.session.session_history",
        "wait_for_history_receipt",
    ),
    "build_filesystem_policy": (
        "jiuwenswarm.server.runtime.agent_adapter.sysop_builder",
        "build_filesystem_policy",
    ),
    "build_yuanrong_sandbox_status_view": (
        "jiuwenswarm.server.runtime.agent_adapter.sysop_builder",
        "build_yuanrong_sandbox_status_view",
    ),
    "effective_files_from_policy": (
        "jiuwenswarm.server.runtime.agent_adapter.sysop_builder",
        "effective_files_from_policy",
    ),
    "find_auto_managed_match": (
        "jiuwenswarm.server.runtime.agent_adapter.sysop_builder",
        "find_auto_managed_match",
    ),
    "find_nested_files_conflict": (
        "jiuwenswarm.server.runtime.agent_adapter.sysop_builder",
        "find_nested_files_conflict",
    ),
    "list_effective_sandbox_files": (
        "jiuwenswarm.server.runtime.agent_adapter.sysop_builder",
        "list_effective_sandbox_files",
    ),
    "validate_sandbox_files_runtime": (
        "jiuwenswarm.server.runtime.agent_adapter.sysop_builder",
        "validate_sandbox_files_runtime",
    ),
    "is_team_params": ("jiuwenswarm.server.utils.utils", "is_team_params"),
    "deprecate_mode": ("gateway.common.mode_matrix", "deprecate_mode"),
    "is_plan_mode": ("gateway.common.mode_matrix", "is_plan_mode"),
    "is_single_agent_mode": ("gateway.common.mode_matrix", "is_single_agent_mode"),
    "is_team_mode": ("gateway.common.mode_matrix", "is_team_mode"),
    "get_permissions_config_req_methods": (
        "jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc",
        "get_permissions_config_req_methods",
    ),
    "get_a4p_req_methods": (
        "jiuwenswarm.agents.harness.common.a4p.rpc",
        "get_a4p_req_methods",
    ),
    "DEFAULT_SANDBOX_POLICY_FILE": (
        "jiuwenswarm.common.config",
        "DEFAULT_SANDBOX_POLICY_FILE",
    ),
    "DEFAULT_SANDBOX_STARTUP_MODE": (
        "jiuwenswarm.common.config",
        "DEFAULT_SANDBOX_STARTUP_MODE",
    ),
    "get_config": ("jiuwenswarm.common.config", "get_config"),
    "get_available_models": ("jiuwenswarm.common.config", "get_available_models"),
    "get_default_models": ("jiuwenswarm.common.config", "get_default_models"),
    "get_config_yaml_mcp_servers": (
        "jiuwenswarm.common.config",
        "get_config_yaml_mcp_servers",
    ),
    "get_mcp_server_config": ("jiuwenswarm.common.config", "get_mcp_server_config"),
    "get_mcp_servers": ("jiuwenswarm.common.config", "get_mcp_servers"),
    "get_sandbox_endpoint": ("jiuwenswarm.common.config", "get_sandbox_endpoint"),
    "get_sandbox_runtime": ("jiuwenswarm.common.config", "get_sandbox_runtime"),
    "get_sandbox_startup_mode": (
        "jiuwenswarm.common.config",
        "get_sandbox_startup_mode",
    ),
    "get_sandbox_startup_mode_explicit": (
        "jiuwenswarm.common.config",
        "get_sandbox_startup_mode_explicit",
    ),
    "remove_mcp_server": ("jiuwenswarm.common.config", "remove_mcp_server"),
    "resolve_preserve_file_sharing_mode_default": (
        "jiuwenswarm.common.config",
        "resolve_preserve_file_sharing_mode_default",
    ),
    "resolve_sandbox_api_token": (
        "jiuwenswarm.common.config",
        "resolve_sandbox_api_token",
    ),
    "resolve_sandbox_policy_path": (
        "jiuwenswarm.common.config",
        "resolve_sandbox_policy_path",
    ),
    "remove_subagent_from_config": (
        "jiuwenswarm.common.config",
        "remove_subagent_from_config",
    ),
    "set_mcp_server_enabled": ("jiuwenswarm.common.config", "set_mcp_server_enabled"),
    "sync_sandbox_api_token_environ": (
        "jiuwenswarm.common.config",
        "sync_sandbox_api_token_environ",
    ),
    "update_sandbox_endpoint": ("jiuwenswarm.common.config", "update_sandbox_endpoint"),
    "update_sandbox_runtime": ("jiuwenswarm.common.config", "update_sandbox_runtime"),
    "upsert_mcp_server": ("jiuwenswarm.common.config", "upsert_mcp_server"),
    "upsert_subagent_in_config": (
        "jiuwenswarm.common.config",
        "upsert_subagent_in_config",
    ),
    "JiuwenBoxRunner": (
        "jiuwenswarm.server.sandbox.jiuwenbox_runner",
        "JiuwenBoxRunner",
    ),
    "load_hooks_config": ("jiuwenswarm.common.hooks_config", "load_hooks_config"),
    "extract_handshake_request": (
        "gateway.common.security.ws_origin",
        "extract_handshake_request",
    ),
    "forbidden_origin_response": (
        "gateway.common.security.ws_origin",
        "forbidden_origin_response",
    ),
    "get_header_value": ("gateway.common.security.ws_origin", "get_header_value"),
    "is_origin_check_enabled": (
        "gateway.common.security.ws_origin",
        "is_origin_check_enabled",
    ),
    "is_allowed_browser_origin": (
        "gateway.common.security.ws_origin",
        "is_allowed_browser_origin",
    ),
    "PLAN_MODE_EXITED_EVENT_TYPE": (
        "jiuwenswarm.agents.harness.code.prompt.plan_approval",
        "PLAN_MODE_EXITED_EVENT_TYPE",
    ),
    "ReqMethod": ("jiuwenswarm.common.schema.message", "ReqMethod"),
    "PersonalContextHostAPI": (
        "jiuwenswarm.server.personal_context",
        "PersonalContextHostAPI",
    ),
    "_PERSONAL_CONTEXT_REQ_METHODS": (
        "jiuwenswarm.server.personal_context.ws_handler",
        "PERSONAL_CONTEXT_REQUEST_METHODS",
    ),
    "handle_personal_context_request": (
        "jiuwenswarm.server.personal_context.ws_handler",
        "handle_personal_context_request",
    ),
    "preview_text": ("jiuwenswarm.common.log_preview", "preview_text"),
    "_SESSION_PREVIOUS_MODE_KEY": (
        "jiuwenswarm.runtime.request",
        "PREVIOUS_SESSION_MODE_KEY",
    ),
    "_apply_resolved_mode_to_request": (
        "jiuwenswarm.runtime.request",
        "apply_resolved_mode_to_request",
    ),
    "prepare_chat_turn": ("jiuwenswarm.runtime.request", "prepare_chat_turn"),
    "resolve_agent_request_mode": (
        "jiuwenswarm.runtime.request",
        "resolve_agent_request_mode",
    ),
    "resolve_request_project_dir": (
        "jiuwenswarm.runtime.request",
        "resolve_request_project_dir",
    ),
    "resolve_request_runtime_mode": (
        "jiuwenswarm.runtime.request",
        "resolve_request_runtime_mode",
    ),
    "_sync_chat_request_metadata": (
        "jiuwenswarm.runtime.request",
        "sync_chat_request_metadata",
    ),
    "RuntimeEvent": ("jiuwenswarm.runtime.events", "RuntimeEvent"),
    "TERMINAL_ERROR_EVENT_TYPES": (
        "jiuwenswarm.runtime.events",
        "TERMINAL_ERROR_EVENT_TYPES",
    ),
    "install_runtime_push_handler": (
        "jiuwenswarm.runtime.host_services",
        "install_runtime_push_handler",
    ),
    "restore_runtime_push_handler": (
        "jiuwenswarm.runtime.host_services",
        "restore_runtime_push_handler",
    ),
    "PlanModeController": ("jiuwenswarm.runtime.plan", "PlanModeController"),
    "VoiceTaskServerAdapter": (
        "jiuwenswarm.server.runtime.video_task.server_adapter",
        "VoiceTaskServerAdapter",
    ),
    "AdapterRegistry": (
        "jiuwenswarm.server.runtime.gateway_adapter",
        "AdapterRegistry",
    ),
    "ConfigAdapter": ("jiuwenswarm.server.runtime.gateway_adapter", "ConfigAdapter"),
    "HarmonyOSAdapter": (
        "jiuwenswarm.server.runtime.gateway_adapter",
        "HarmonyOSAdapter",
    ),
    "MemoryAdapter": ("jiuwenswarm.server.runtime.gateway_adapter", "MemoryAdapter"),
    "ProjectAdapter": ("jiuwenswarm.server.runtime.gateway_adapter", "ProjectAdapter"),
    "SessionAdapter": ("jiuwenswarm.server.runtime.gateway_adapter", "SessionAdapter"),
    "WorkspaceFileAdapter": (
        "jiuwenswarm.server.runtime.gateway_adapter",
        "WorkspaceFileAdapter",
    ),
    "reload_runtime_after_config_save": (
        "jiuwenswarm.server.runtime.gateway_adapter.config_adapter",
        "reload_runtime_after_config_save",
    ),
    "_HISTORY_PAGE_SIZE": ("jiuwenswarm.server.wire_truncate", "_HISTORY_PAGE_SIZE"),
    "_HISTORY_WIRE_STRING_LIMIT": (
        "jiuwenswarm.server.wire_truncate",
        "_HISTORY_WIRE_STRING_LIMIT",
    ),
    "_HISTORY_WIRE_METADATA_STRING_LIMIT": (
        "jiuwenswarm.server.wire_truncate",
        "_HISTORY_WIRE_METADATA_STRING_LIMIT",
    ),
    "_HISTORY_WIRE_LIST_LIMIT": (
        "jiuwenswarm.server.wire_truncate",
        "_HISTORY_WIRE_LIST_LIMIT",
    ),
    "_HISTORY_WIRE_DEPTH_LIMIT": (
        "jiuwenswarm.server.wire_truncate",
        "_HISTORY_WIRE_DEPTH_LIMIT",
    ),
    "_HISTORY_WIRE_RECORD_MAX_BYTES": (
        "jiuwenswarm.server.wire_truncate",
        "_HISTORY_WIRE_RECORD_MAX_BYTES",
    ),
    "_TEAM_HISTORY_DEFAULT_LIMIT": (
        "jiuwenswarm.server.wire_truncate",
        "_TEAM_HISTORY_DEFAULT_LIMIT",
    ),
    "_TEAM_HISTORY_MAX_LIMIT": (
        "jiuwenswarm.server.wire_truncate",
        "_TEAM_HISTORY_MAX_LIMIT",
    ),
    "_TEAM_HISTORY_DEFAULT_MAX_BYTES": (
        "jiuwenswarm.server.wire_truncate",
        "_TEAM_HISTORY_DEFAULT_MAX_BYTES",
    ),
    "_TEAM_HISTORY_MIN_MAX_BYTES": (
        "jiuwenswarm.server.wire_truncate",
        "_TEAM_HISTORY_MIN_MAX_BYTES",
    ),
    "_TEAM_HISTORY_MAX_MAX_BYTES": (
        "jiuwenswarm.server.wire_truncate",
        "_TEAM_HISTORY_MAX_MAX_BYTES",
    ),
    "_TEAM_HISTORY_FRAME_OVERHEAD_BYTES": (
        "jiuwenswarm.server.wire_truncate",
        "_TEAM_HISTORY_FRAME_OVERHEAD_BYTES",
    ),
    "_WORKFLOW_AGENT_FIELD_PART_BYTES": (
        "jiuwenswarm.server.wire_truncate",
        "_WORKFLOW_AGENT_FIELD_PART_BYTES",
    ),
    "_WORKFLOW_LIST_DEFAULT_LIMIT": (
        "jiuwenswarm.server.wire_truncate",
        "_WORKFLOW_LIST_DEFAULT_LIMIT",
    ),
    "_WORKFLOW_LIST_MAX_LIMIT": (
        "jiuwenswarm.server.wire_truncate",
        "_WORKFLOW_LIST_MAX_LIMIT",
    ),
    "_WORKFLOW_PHASE_DEFAULT_LIMIT": (
        "jiuwenswarm.server.wire_truncate",
        "_WORKFLOW_PHASE_DEFAULT_LIMIT",
    ),
    "_WORKFLOW_PHASE_MAX_LIMIT": (
        "jiuwenswarm.server.wire_truncate",
        "_WORKFLOW_PHASE_MAX_LIMIT",
    ),
    "_WORKFLOW_AGENT_DEFAULT_LIMIT": (
        "jiuwenswarm.server.wire_truncate",
        "_WORKFLOW_AGENT_DEFAULT_LIMIT",
    ),
    "_WORKFLOW_AGENT_MAX_LIMIT": (
        "jiuwenswarm.server.wire_truncate",
        "_WORKFLOW_AGENT_MAX_LIMIT",
    ),
    "_SPLITTABLE_AGENT_FIELDS": (
        "jiuwenswarm.server.wire_truncate",
        "_SPLITTABLE_AGENT_FIELDS",
    ),
    "_HISTORY_RESTORABLE_ASSISTANT_EVENT_TYPES": (
        "jiuwenswarm.server.wire_truncate",
        "_HISTORY_RESTORABLE_ASSISTANT_EVENT_TYPES",
    ),
    "_json_wire_size": ("jiuwenswarm.server.wire_truncate", "_json_wire_size"),
    "_coerce_int": ("jiuwenswarm.server.wire_truncate", "_coerce_int"),
    "_truncate_string_by_bytes": (
        "jiuwenswarm.server.wire_truncate",
        "_truncate_string_by_bytes",
    ),
    "_compact_wire_metadata_value": (
        "jiuwenswarm.server.wire_truncate",
        "_compact_wire_metadata_value",
    ),
    "_sanitize_history_wire_value": (
        "jiuwenswarm.server.wire_truncate",
        "_sanitize_history_wire_value",
    ),
    "_collapse_oversized_history_record": (
        "jiuwenswarm.server.wire_truncate",
        "_collapse_oversized_history_record",
    ),
    "_minimal_history_record_for_wire": (
        "jiuwenswarm.server.wire_truncate",
        "_minimal_history_record_for_wire",
    ),
    "_sanitize_history_record_for_wire": (
        "jiuwenswarm.server.wire_truncate",
        "_sanitize_history_record_for_wire",
    ),
    "split_history_record_for_stream": (
        "jiuwenswarm.server.wire_truncate",
        "split_history_record_for_stream",
    ),
    "_select_history_record_page": (
        "jiuwenswarm.server.wire_truncate",
        "_select_history_record_page",
    ),
    "_split_oversized_agent_fields": (
        "jiuwenswarm.server.wire_truncate",
        "_split_oversized_agent_fields",
    ),
    "_workflow_list_summary_item": (
        "jiuwenswarm.server.wire_truncate",
        "_workflow_list_summary_item",
    ),
    "_workflow_phase_summary": (
        "jiuwenswarm.server.wire_truncate",
        "_workflow_phase_summary",
    ),
    "_workflow_run_meta": ("jiuwenswarm.server.wire_truncate", "_workflow_run_meta"),
    "_find_phase": ("jiuwenswarm.server.wire_truncate", "_find_phase"),
    "_find_agent": ("jiuwenswarm.server.wire_truncate", "_find_agent"),
    "_build_workflow_list_payload": (
        "jiuwenswarm.server.wire_truncate",
        "_build_workflow_list_payload",
    ),
    "_build_workflow_detail_paginated": (
        "jiuwenswarm.server.wire_truncate",
        "_build_workflow_detail_paginated",
    ),
    "_build_phase_detail_paginated": (
        "jiuwenswarm.server.wire_truncate",
        "_build_phase_detail_paginated",
    ),
    "_build_agent_detail": ("jiuwenswarm.server.wire_truncate", "_build_agent_detail"),
    "_reset_active_browser_runtimes_if_available": (
        "jiuwenswarm.server.management.agents",
        "_reset_active_browser_runtimes_if_available",
    ),
    "_reset_requested_browser_runtime_if_available": (
        "jiuwenswarm.server.management.agents",
        "_reset_requested_browser_runtime_if_available",
    ),
    "_AGENT_CREATION_SYSTEM_PROMPT": (
        "jiuwenswarm.server.management.agents",
        "_AGENT_CREATION_SYSTEM_PROMPT",
    ),
    "AgentsManagement": ("jiuwenswarm.server.management.agents", "AgentsManagement"),
    "AssetsManagement": ("jiuwenswarm.server.management.assets", "AssetsManagement"),
    "AuthorizationManagement": (
        "jiuwenswarm.server.management.authorization",
        "AuthorizationManagement",
    ),
    "_MANUAL_COMPACT_PROCESSOR_TYPES": (
        "jiuwenswarm.server.management.commands",
        "_MANUAL_COMPACT_PROCESSOR_TYPES",
    ),
    "_SIMPLIFY_PROMPT_TEMPLATE": (
        "jiuwenswarm.server.management.commands",
        "_SIMPLIFY_PROMPT_TEMPLATE",
    ),
    "_is_env_api_base_placeholder": (
        "jiuwenswarm.server.management.commands",
        "_is_env_api_base_placeholder",
    ),
    "_build_simplify_prompt": (
        "jiuwenswarm.server.management.commands",
        "_build_simplify_prompt",
    ),
    "_extract_compact_summary_processor": (
        "jiuwenswarm.server.management.commands",
        "_extract_compact_summary_processor",
    ),
    "resolve_status_code_graph": (
        "jiuwenswarm.server.management.commands",
        "resolve_status_code_graph",
    ),
    "CommandsManagement": (
        "jiuwenswarm.server.management.commands",
        "CommandsManagement",
    ),
    "ExtensionsManagement": (
        "jiuwenswarm.server.management.extensions",
        "ExtensionsManagement",
    ),
    "ManagementFacade": ("jiuwenswarm.server.management.facade", "ManagementFacade"),
    "_is_restorable_history_record": (
        "jiuwenswarm.server.management.history",
        "_is_restorable_history_record",
    ),
    "_todo_snapshot_session_fields": (
        "jiuwenswarm.server.management.history",
        "_todo_snapshot_session_fields",
    ),
    "HistoryManagement": ("jiuwenswarm.server.management.history", "HistoryManagement"),
    "McpUpsertTypes": ("jiuwenswarm.server.management.mcp", "McpUpsertTypes"),
    "_MCP_KEY_SENSITIVE_SUBSTRINGS": (
        "jiuwenswarm.server.management.mcp",
        "_MCP_KEY_SENSITIVE_SUBSTRINGS",
    ),
    "McpManagement": ("jiuwenswarm.server.management.mcp", "McpManagement"),
    "create_auto_harness_service": (
        "jiuwenswarm.server.management.schedule",
        "create_auto_harness_service",
    ),
    "_harness_error_code": (
        "jiuwenswarm.server.management.packages",
        "_harness_error_code",
    ),
    "PackagesManagement": (
        "jiuwenswarm.server.management.packages",
        "PackagesManagement",
    ),
    "ProactiveManagement": (
        "jiuwenswarm.server.management.proactive",
        "ProactiveManagement",
    ),
    "PROVIDER_TYPES": ("jiuwenswarm.server.management.registry", "PROVIDER_TYPES"),
    "ManagementServices": (
        "jiuwenswarm.server.management.registry",
        "ManagementServices",
    ),
    "RsiManagement": ("jiuwenswarm.server.management.rsi", "RsiManagement"),
    "_require_sandbox_supported": (
        "jiuwenswarm.server.management.sandbox",
        "_require_sandbox_supported",
    ),
    "_file_entry_matches_path": (
        "jiuwenswarm.server.management.sandbox",
        "_file_entry_matches_path",
    ),
    "_canonicalize_sandbox_files_path": (
        "jiuwenswarm.server.management.sandbox",
        "_canonicalize_sandbox_files_path",
    ),
    "_SANDBOX_FILES_PARAMS": (
        "jiuwenswarm.server.management.sandbox",
        "_SANDBOX_FILES_PARAMS",
    ),
    "_reject_extra_sandbox_files_params": (
        "jiuwenswarm.server.management.sandbox",
        "_reject_extra_sandbox_files_params",
    ),
    "SandboxManagement": ("jiuwenswarm.server.management.sandbox", "SandboxManagement"),
    "ScheduleManagement": (
        "jiuwenswarm.server.management.schedule",
        "ScheduleManagement",
    ),
    "SessionsManagement": (
        "jiuwenswarm.server.management.sessions",
        "SessionsManagement",
    ),
    "_request_query_text": (
        "jiuwenswarm.server.worker.execution_support",
        "_request_query_text",
    ),
    "TeamsManagement": ("jiuwenswarm.server.management.teams", "TeamsManagement"),
    "WorkflowsManagement": (
        "jiuwenswarm.server.management.workflows",
        "WorkflowsManagement",
    ),
    "WorkflowControlManagement": (
        "jiuwenswarm.server.management.workflow_control",
        "WorkflowControlManagement",
    ),
    "existing_workflow_handler": (
        "jiuwenswarm.server.management.workflow_reader",
        "existing_workflow_handler",
    ),
    "restored_workflows": (
        "jiuwenswarm.server.management.workflow_reader",
        "restored_workflows",
    ),
    "_UNNAMED_PHASE": (
        "jiuwenswarm.server.management.workflow_state",
        "_UNNAMED_PHASE",
    ),
    "_UNNAMED_HUMAN": (
        "jiuwenswarm.server.management.workflow_state",
        "_UNNAMED_HUMAN",
    ),
    "_UNNAMED_AGENT": (
        "jiuwenswarm.server.management.workflow_state",
        "_UNNAMED_AGENT",
    ),
    "_ACTIVITY_RING_CAP": (
        "jiuwenswarm.server.management.workflow_state",
        "_ACTIVITY_RING_CAP",
    ),
    "PhasePlan": ("jiuwenswarm.server.management.workflow_state", "PhasePlan"),
    "WorkflowProgress": (
        "jiuwenswarm.server.management.workflow_state",
        "WorkflowProgress",
    ),
    "_NON_ALNUM_RE": ("jiuwenswarm.server.management.workflow_state", "_NON_ALNUM_RE"),
    "_MULTI_DASH_RE": (
        "jiuwenswarm.server.management.workflow_state",
        "_MULTI_DASH_RE",
    ),
    "_slugify": ("jiuwenswarm.server.management.workflow_state", "_slugify"),
    "WorkflowAgentActivity": (
        "jiuwenswarm.server.management.workflow_state",
        "WorkflowAgentActivity",
    ),
    "WorkflowAgentState": (
        "jiuwenswarm.server.management.workflow_state",
        "WorkflowAgentState",
    ),
    "WorkflowVerifyGroupState": (
        "jiuwenswarm.server.management.workflow_state",
        "WorkflowVerifyGroupState",
    ),
    "WorkflowPhaseState": (
        "jiuwenswarm.server.management.workflow_state",
        "WorkflowPhaseState",
    ),
    "_BackfillTerminalCtx": (
        "jiuwenswarm.server.management.workflow_state",
        "_BackfillTerminalCtx",
    ),
    "WorkflowRunState": (
        "jiuwenswarm.server.management.workflow_state",
        "WorkflowRunState",
    ),
    "_StreamKeepalive": (
        "jiuwenswarm.server.worker.execution_support",
        "_StreamKeepalive",
    ),
    "_TurnOutcomeTracker": (
        "jiuwenswarm.server.worker.execution_support",
        "_TurnOutcomeTracker",
    ),
    "_SessionMessageResumeState": (
        "jiuwenswarm.server.worker.execution_support",
        "_SessionMessageResumeState",
    ),
    "_consume_keepalive_task_result": (
        "jiuwenswarm.server.worker.execution_support",
        "_consume_keepalive_task_result",
    ),
    "_stop_stream_keepalive": (
        "jiuwenswarm.server.worker.execution_support",
        "_stop_stream_keepalive",
    ),
    "_STREAM_KEEPALIVE_INTERVAL_SECONDS": (
        "jiuwenswarm.server.worker.execution_support",
        "_STREAM_KEEPALIVE_INTERVAL_SECONDS",
    ),
    "_STREAM_KEEPALIVE_STOP_TIMEOUT_SECONDS": (
        "jiuwenswarm.server.worker.execution_support",
        "_STREAM_KEEPALIVE_STOP_TIMEOUT_SECONDS",
    ),
}


def _uses_projectless_task_workspace(params: dict[str, object], channel_id: str) -> bool:
    """Retain the historical predicate through its current Runtime owner."""
    from jiuwenswarm.runtime.session_provisioner import RuntimeSessionProvisioner

    return RuntimeSessionProvisioner._uses_projectless_workspace(params, channel_id)


def __getattr__(name: str) -> Any:
    target = _COMPAT_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module, symbol = target
    imported = import_module(module)
    value = getattr(imported, symbol) if symbol is not None else imported
    globals()[name] = value
    return value
