# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentWebSocketServer - Gateway 与 AgentServer 之间的 WebSocket 服务端."""

from __future__ import annotations

import asyncio
import datetime as _dt
import inspect
import json
import logging
import os
import sys
from dataclasses import dataclass
import time
from pathlib import Path
from typing import Any, ClassVar, Optional
from weakref import WeakValueDictionary

from gateway_protocol.hooks import AgentServerChatHookContext, AgentServerHookEvents
from openjiuwen.core.common.logging import server_logger
from websockets.exceptions import ConnectionClosed as WebSocketConnectionClosed

from jiuwenswarm.agents.harness.common.auto_harness import AutoHarnessService, reset_harness_packages_state
from jiuwenswarm.agents.harness.code.rails.heartbeat.runtime import HeartbeatRailRuntime
from jiuwenswarm.server.gateway_push.wire import build_server_push_wire
from jiuwenswarm.server.ws_send import send_wire_payload
from jiuwenswarm.agents.harness.common.tools.acp_output_tools import get_acp_output_manager
from jiuwenswarm.common.utils import (
    get_agent_root_dir,
    get_agent_sessions_dir,
)
from jiuwenswarm.common.session_message import (
    SESSION_MESSAGE_INTERNAL_KEY,
    SESSION_MESSAGE_ORIGIN,
    SESSION_MESSAGE_OWNER_SCOPE_METADATA_KEY,
    SESSION_MESSAGE_ANONYMOUS_OWNER_METADATA_KEY,
)
from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.e2a.constants import (
    E2A_CANCEL_SOURCE_CLIENT_DISCONNECT,
    E2A_INTERNAL_CANCEL_SOURCE_KEY,
    E2A_WIRE_INTERNAL_METADATA_KEYS,
)
from jiuwenswarm.common.e2a.gateway_normalize import (
    E2A_FALLBACK_FAILED_KEY,
    E2A_INTERNAL_CONTEXT_KEY,
    E2A_LEGACY_AGENT_REQUEST_KEY,
)
from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.e2a.wire_codec import (
    encode_agent_chunk_for_wire,
    encode_agent_response_for_wire,
    encode_json_parse_error_wire,
)
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.common.ws_diagnostics import (
    describe_ws_exception,
    describe_ws_peer,
    format_ws_diagnostics,
)
from jiuwenswarm.common.ws_limits import AGENT_WS_MAX_MESSAGE_BYTES
from jiuwenswarm.server.runtime.agent_manager import AgentManager, ACP_DEFAULT_CAPABILITIES
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.server.context import build_ws_request_context
from jiuwenswarm.server.dispatch import try_dispatch
from jiuwenswarm.runtime.session_provisioner import (
    SessionCreateInput,
    SessionForkInput,
    SessionProvisionCommitContext,
    SessionProvisionCommitTiming,
    SessionProvisionError,
    SessionProvisionState,
)
from jiuwenswarm.server.runtime.tokenizer_service import TokenizerService
from jiuwenswarm.server.runtime.session.session_metadata import (
    build_server_push_message,
    get_session_metadata,
)
from jiuwenswarm.server.runtime.session.session_message_service import (
    SessionMessageExecutionResult,
    SessionMessageService,
)
from jiuwenswarm.server.runtime.session.session_message_store import (
    SessionMessageRecord,
    SessionMessageStore,
)
from jiuwenswarm.server.runtime.session.session_history import (
    append_history_record,
    enqueue_history_request_completion,
    read_member_history_records,
    read_team_history_records,
    wait_for_history_receipt,
)
from jiuwenswarm.server.utils.utils import is_team_params
from jiuwenswarm.common.mode_matrix import (
    deprecate_mode,
    is_single_agent_mode,
    is_team_mode,
)
from jiuwenswarm.agents.harness.common.a4p_rpc import get_a4p_req_methods
from jiuwenswarm.common.config import (
    DEFAULT_SANDBOX_POLICY_FILE,
    get_config,
    get_available_models,
    get_default_models,
    get_sandbox_endpoint,
    get_sandbox_startup_mode_explicit,
    resolve_sandbox_api_token,
    resolve_sandbox_policy_path,
    sync_sandbox_api_token_environ,
    update_sandbox_endpoint,
    update_sandbox_runtime,
)
from jiuwenswarm.server.sandbox.jiuwenbox_runner import JiuwenBoxRunner
from jiuwenswarm.common.security.ws_origin import (
    extract_handshake_request,
    forbidden_origin_response,
    get_header_value,
    is_origin_check_enabled,
    is_allowed_browser_origin,
)
from jiuwenswarm.agents.harness.code.prompt.plan_approval import (
    PLAN_MODE_EXITED_EVENT_TYPE,
)
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.personal_context import PersonalContextHostAPI
from jiuwenswarm.server.personal_context.ws_handler import (
    PERSONAL_CONTEXT_REQUEST_METHODS as _PERSONAL_CONTEXT_REQ_METHODS,
    handle_personal_context_request,
)
from jiuwenswarm.common.log_preview import preview_text
from jiuwenswarm.runtime.request import (
    PREVIOUS_SESSION_MODE_KEY as _SESSION_PREVIOUS_MODE_KEY,  # noqa: F401
    prepare_chat_turn,
    resolve_agent_request_mode,
    resolve_request_runtime_mode,
    sync_chat_request_metadata as _sync_chat_request_metadata,
)
from jiuwenswarm.runtime.events import RuntimeEvent, TERMINAL_ERROR_EVENT_TYPES
from jiuwenswarm.runtime.host_services import (
    install_runtime_push_handler,
    restore_runtime_push_handler,
)
from jiuwenswarm.runtime.plan import PlanModeController
from jiuwenswarm.extensions.video_duplex.backend.tasks.server_adapter import VoiceTaskServerAdapter
from jiuwenswarm.server.runtime.gateway_adapter import (
    AdapterRegistry,
    ConfigAdapter,
    HarmonyOSAdapter,
    MemoryAdapter,
    ProjectAdapter,
    SessionAdapter,
    WorkspaceFileAdapter,
)
from jiuwenswarm.server.runtime.gateway_adapter.config_adapter import (
    reload_runtime_after_config_save,
)

logger = logging.getLogger(__name__)


async def _reuse_server_runtime_dependencies() -> None:
    """Compatibility runtime wrappers borrow dependencies owned by AgentServer."""

# These handlers also perform AgentServer process-lifecycle cleanup that the
# neutral adapters deliberately do not own.  Keep their established branches.
_GATEWAY_ADAPTER_LEGACY_METHODS = frozenset({
    ReqMethod.SESSION_DELETE,
    ReqMethod.SESSION_RENAME,
})


def _parse_single_byte_range(
    range_header: str, file_size: int
) -> tuple[int, int] | None:
    """Parse a single HTTP byte range for file-download bridge handlers."""
    if file_size <= 0 or not range_header.startswith("bytes=") or "," in range_header:
        return None
    start_text, end_text = range_header[6:].split("-", 1) if "-" in range_header[6:] else ("", "")
    if not start_text:
        if not end_text.isdecimal() or int(end_text) <= 0:
            return None
        return max(0, file_size - int(end_text)), file_size - 1
    if not start_text.isdecimal() or (end_text and not end_text.isdecimal()):
        return None
    start = int(start_text)
    if start >= file_size:
        return None
    end = min(int(end_text), file_size - 1) if end_text else file_size - 1
    return (start, end) if end >= start else None

# Session owner preparation completes before the response. Optional KVC signals
# run after the response so affinity latency cannot fail a UI session change.
_background_session_kvc_tasks: set[asyncio.Task] = set()


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


def _is_session_prewarm_model_eligible(
    params: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> bool:
    """Return whether session prewarm can safely use the configured default model.

    A prewarmed single-agent child freezes its skill-retrieval budget before the
    first chat request arrives.  Requests for a non-default model must therefore
    create that child on the first chat, when the selected model is available.
    """
    requested = str(params.get("model_name") or "").strip()
    if not requested:
        return True

    resolved_config = config if config is not None else get_config()
    from jiuwenswarm.agents.harness.common.tools.skill_retrieval_toolkits import (
        is_skill_retrieval_enabled,
    )

    # The global switch promises the unchanged legacy runtime when disabled.
    if not is_skill_retrieval_enabled(resolved_config):
        return True

    entries = get_default_models(resolved_config)
    first_identifiers: set[str] | None = None
    selected_identifiers: set[str] | None = None
    name_counts: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_client_config = entry.get("model_client_config")
        if not isinstance(model_client_config, dict):
            continue
        model_name = str(model_client_config.get("model_name") or "").strip()
        if not model_name:
            continue

        occurrence = name_counts.get(model_name, 0)
        name_counts[model_name] = occurrence + 1
        identifiers = {model_name, f"{model_name}#{occurrence}"}
        alias = str(entry.get("alias") or "").strip()
        if alias:
            identifiers.add(alias)
        if first_identifiers is None:
            first_identifiers = identifiers
        if selected_identifiers is None and entry.get("is_default") is True:
            selected_identifiers = identifiers

    default_identifiers = selected_identifiers or first_identifiers or set()
    return requested in default_identifiers


def _log_background_session_kvc_failure(task: asyncio.Task) -> None:
    """Log optional post-response KVC failures without changing session state."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(
            "[AgentWebSocketServer] %s failed after ack: %s",
            task.get_name(),
            exc,
            exc_info=exc,
        )

_SERVER_PLAN_CONTROLLER = PlanModeController()
# Compatibility aliases for existing diagnostics/tests. Runtime semantics live
# in PlanModeController and are shared by AgentServer and process-style CLI.
_session_mode_sync_locks = _SERVER_PLAN_CONTROLLER.sync_locks

# Serialize switch owner preparation and acknowledgements per client
# connection. AgentServer handles WebSocket frames in independent tasks, so
# rapid navigation requests would otherwise race even on one socket.
_session_switch_locks: WeakValueDictionary[str, asyncio.Lock] = (
    WeakValueDictionary()
)

# Sessions that have successfully exited plan mode via exit_plan_mode tool.
# Set by _check_post_process_plan_exit, consumed by _ensure_code_mode_state
# to prevent TUI-race re-entrance to plan mode.
_plan_exited_sessions = _SERVER_PLAN_CONTROLLER.exited_sessions

# 本进程内曾进入过 plan 的 work 单 agent 会话。work 的准入面覆盖 IM / 定时任务 /
# CLI / Web work 的每一条普通消息，而其中绝大多数会话从未开过 Plan；有这个标记
# 才需要去同步 plan 状态。跨重启的情况另有一道判据（会话 metadata 里上一轮的
# canonical mode），见 ``_session_may_hold_plan_state``。
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

# ``plan_entry_source`` 的合法取值，表示"用户这一条消息明确要求进入 plan"。
# 一次性字段：TUI 的 ``/plan`` 命令、Web 用户手动打开 Plan 开关后的第一条消息。
# ── 流式连接保活间隔：当 Agent 处理时间超过此阈值时，发送 keepalive chunk --
# 避免 ping_timeout 导致连接关闭。默认 10 秒，小于服务端 ping_timeout=20s。
_STREAM_KEEPALIVE_INTERVAL_SECONDS = 10.0
_STREAM_KEEPALIVE_STOP_TIMEOUT_SECONDS = 1.0
from jiuwenswarm.server.wire_truncate import (  # noqa: F401  — re-exported for tests / handlers
    _HISTORY_PAGE_SIZE,
    _HISTORY_WIRE_STRING_LIMIT,
    _HISTORY_WIRE_METADATA_STRING_LIMIT,
    _HISTORY_WIRE_LIST_LIMIT,
    _HISTORY_WIRE_DEPTH_LIMIT,
    _HISTORY_WIRE_RECORD_MAX_BYTES,
    _TEAM_HISTORY_DEFAULT_LIMIT,
    _TEAM_HISTORY_MAX_LIMIT,
    _TEAM_HISTORY_DEFAULT_MAX_BYTES,
    _TEAM_HISTORY_MIN_MAX_BYTES,
    _TEAM_HISTORY_MAX_MAX_BYTES,
    _TEAM_HISTORY_FRAME_OVERHEAD_BYTES,
    _WORKFLOW_AGENT_FIELD_PART_BYTES,
    _WORKFLOW_LIST_DEFAULT_LIMIT,
    _WORKFLOW_LIST_MAX_LIMIT,
    _WORKFLOW_PHASE_DEFAULT_LIMIT,
    _WORKFLOW_PHASE_MAX_LIMIT,
    _WORKFLOW_AGENT_DEFAULT_LIMIT,
    _WORKFLOW_AGENT_MAX_LIMIT,
    _SPLITTABLE_AGENT_FIELDS,
    _HISTORY_RESTORABLE_ASSISTANT_EVENT_TYPES,
    _json_wire_size,
    _coerce_int,
    _truncate_string_by_bytes,
    _compact_wire_metadata_value,
    _sanitize_history_wire_value,
    _collapse_oversized_history_record,
    _minimal_history_record_for_wire,
    _sanitize_history_record_for_wire,
    split_history_record_for_stream,
    _select_history_record_page,
    _split_oversized_agent_fields,
    _workflow_list_summary_item,
    _workflow_phase_summary,
    _workflow_run_meta,
    _find_phase,
    _find_agent,
    _build_workflow_list_payload,
    _build_workflow_detail_paginated,
    _build_phase_detail_paginated,
    _build_agent_detail,
)


def _consume_keepalive_task_result(
    keepalive_task: asyncio.Task,
    request_id: str,
) -> None:
    """Observe an auxiliary keepalive result without replacing stream errors."""
    try:
        keepalive_task.result()
    except asyncio.CancelledError:
        return
    except WebSocketConnectionClosed:
        logger.info(
            "[AgentWebSocketServer] keepalive task stopped after connection closed: "
            "request_id=%s",
            request_id,
        )
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception(
            "[AgentWebSocketServer] keepalive task failed: request_id=%s",
            request_id,
        )


async def _stop_stream_keepalive(
    keepalive_task: asyncio.Task,
    keepalive_stop_event: asyncio.Event,
    stream_activity_event: asyncio.Event,
    request_id: str,
) -> None:
    """Stop an owned stream keepalive without blocking its owner indefinitely."""
    keepalive_stop_event.set()
    stream_activity_event.set()
    phase_timeout = _STREAM_KEEPALIVE_STOP_TIMEOUT_SECONDS / 2.0
    try:
        done, _ = await asyncio.wait(
            {keepalive_task},
            timeout=phase_timeout,
        )
        if keepalive_task not in done:
            logger.warning(
                "[AgentWebSocketServer] keepalive task did not stop cooperatively; "
                "cancelling: request_id=%s",
                request_id,
            )
            keepalive_task.cancel()
            done, _ = await asyncio.wait(
                {keepalive_task},
                timeout=phase_timeout,
            )
    except asyncio.CancelledError:
        if keepalive_task.done():
            _consume_keepalive_task_result(keepalive_task, request_id)
        else:
            keepalive_task.add_done_callback(
                lambda finished: _consume_keepalive_task_result(
                    finished,
                    request_id,
                )
            )
            keepalive_task.cancel()
        raise
    if keepalive_task not in done:
        logger.error(
            "[AgentWebSocketServer] keepalive task did not stop after bounded cleanup: "
            "request_id=%s timeout=%.3fs",
            request_id,
            _STREAM_KEEPALIVE_STOP_TIMEOUT_SECONDS,
        )
        keepalive_task.add_done_callback(
            lambda finished: _consume_keepalive_task_result(
                finished,
                request_id,
            )
        )
        return
    _consume_keepalive_task_result(keepalive_task, request_id)


@dataclass(slots=True)
class _SessionMessageResumeState:
    """Stable mailbox identity while one user answer resumes a target turn."""

    message_id: str
    target_session_id: str
    interrupt_request_id: str
    interrupt_source: str
    waiting_user: bool = False


@dataclass(slots=True)
class _TurnOutcomeTracker:
    """Classify Runtime events only when they contain a terminal signal."""

    saw_chat_final: bool = False
    saw_runtime_accepted: bool = False
    waiting_user: bool = False
    saw_error: bool = False
    error: str = ""

    def observe(self, event: RuntimeEvent) -> None:
        event_type = event.event_type
        if event_type == "chat.final":
            self.saw_chat_final = True
        elif event_type == "runtime.accepted":
            self.saw_runtime_accepted = True
        elif event_type == "chat.ask_user_question":
            self.waiting_user = True
        if not event.ok or event_type in TERMINAL_ERROR_EVENT_TYPES:
            self.fail(self._event_error(event))

    def fail(self, error: str) -> None:
        self.saw_error = True
        if error:
            self.error = error

    def outcome(self) -> str:
        if self.saw_error:
            return "failed"
        if self.waiting_user:
            return "waiting_user"
        if self.saw_chat_final:
            return "succeeded"
        return "unknown"

    def unconfirmed_error(self) -> str:
        if self.saw_runtime_accepted:
            return "Runtime accepted the request without confirming completion"
        return "Runtime stream ended without confirming completion"

    @staticmethod
    def _event_error(event: RuntimeEvent) -> str:
        payload = event.payload if isinstance(event.payload, dict) else {}
        return str(payload.get("error") or payload.get("message") or "")


class _StreamKeepalive:
    """Own the transport keepalive task for one streaming request."""

    def __init__(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        self._ws = ws
        self._request = request
        self._send_lock = send_lock
        self._channel_id = request.channel_id or "default"
        self._stop_event = asyncio.Event()
        self._activity_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start sending keepalives while the stream is idle."""
        self._task = asyncio.create_task(
            self._run(),
            name=f"stream-keepalive:{self._request.request_id}",
        )

    def notify_activity(self, *, terminal: bool = False) -> None:
        """Restart the idle timer and optionally prevent future keepalives."""
        self._activity_event.set()
        if terminal:
            self._stop_event.set()

    def signal_stop(self) -> None:
        """Prevent new sends and wake an idle keepalive immediately."""
        self._stop_event.set()
        self._activity_event.set()

    async def stop(self) -> None:
        """Join the owned task within the configured total time budget."""
        self.signal_stop()
        if self._task is None:
            return
        await _stop_stream_keepalive(
            self._task,
            self._stop_event,
            self._activity_event,
            self._request.request_id,
        )

    async def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._activity_event.wait(),
                        timeout=_STREAM_KEEPALIVE_INTERVAL_SECONDS,
                    )
                    self._activity_event.clear()
                except asyncio.TimeoutError:
                    if self._stop_event.is_set():
                        break
                    if self._activity_event.is_set():
                        continue
                    keepalive_chunk = AgentResponseChunk(
                        request_id=self._request.request_id,
                        channel_id=self._channel_id,
                        payload={"event_type": "keepalive"},
                        is_complete=False,
                    )
                    if keepalive_chunk.agent_ref is None:
                        keepalive_chunk.agent_ref = self._request.agent_ref
                    wire = encode_agent_chunk_for_wire(
                        keepalive_chunk,
                        response_id=self._request.request_id,
                        sequence=-1,
                    )
                    async with self._send_lock:
                        if self._stop_event.is_set():
                            break
                        if self._activity_event.is_set():
                            continue
                        await send_wire_payload(self._ws, wire)
                    logger.info(
                        "[AgentWebSocketServer] keepalive chunk 发送: request_id=%s",
                        self._request.request_id,
                    )
        except WebSocketConnectionClosed:
            logger.info(
                "[AgentWebSocketServer] keepalive 停止，WebSocket 已关闭: "
                "request_id=%s",
                self._request.request_id,
            )


def _request_query_text(request: AgentRequest) -> str:
    """Return text chat query only; structured events are handled downstream."""
    if not isinstance(request.params, dict):
        return ""
    query = request.params.get("query")
    if not isinstance(query, str):
        return ""
    return query.strip()






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
    # 将 app_id 注入 metadata，供 cron 路由等下游使用
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


def _uses_projectless_task_workspace(
    params: dict[str, Any],
    channel_id: str,
) -> bool:
    """Return whether the request should use an isolated task workspace.

    TUI sends its launch directory as ``project_dir``/``cwd``.  That is an
    explicit project workspace even when the request mode resolves to
    ``agent`` or ``code``; only requests without either directory should use
    the Documents/JiuwenSwarm projectless task workspace.
    """
    for key in ("project_dir", "cwd"):
        value = params.get(key)
        if isinstance(value, (str, os.PathLike)) and str(value).strip():
            return False

    raw_work_mode = params.get("work_mode")
    if not isinstance(raw_work_mode, str) or raw_work_mode.strip().lower() not in {
        "code",
        "work",
    }:
        from jiuwenswarm.server.runtime.session.work_mode import (
            default_work_mode_for_channel,
        )

        raw_work_mode = default_work_mode_for_channel(channel_id)
    manager_mode, _, _ = resolve_agent_request_mode(
        params.get("mode", "agent"),
        work_mode=raw_work_mode,
    )
    return manager_mode in {"agent", "code"}


def _inject_plan_mode_activation_reminder(request: AgentRequest) -> None:
    """Compatibility alias for the shared Runtime plan controller."""
    PlanModeController.inject_activation_reminder(request)


# Key substrings whose presence marks a payload field as credential-like.



class AgentWebSocketServer:
    """Gateway 与 AgentServer 之间的 WebSocket 服务端（单例）.

    监听来自 Gateway (WebSocketAgentServerClient) 的连接，按协议约定处理请求：
    - 收到 JSON：E2AEnvelope（或过渡期 legacy + 兜底信封）
    - is_stream=False：``process_message`` → 一条 **E2AResponse** JSON（``jiuwenswarm.e2a.wire_codec``）
    - is_stream=True：逐条 **E2AResponse** JSON（chunk/complete/error）
    - 例外：首帧 ``connection.ack`` 仍为 ``type/event`` 事件帧

    支持 send_push：推送帧亦为 E2AResponse 线格式（由 chunk 编码）。
    """

    _instance: ClassVar[AgentWebSocketServer | None] = None

    def __init__(
            self,
            host: str = "127.0.0.1",
            port: int = 18000,
            *,
            ping_interval: float | None = 30.0,
            ping_timeout: float | None = 300.0,
    ) -> None:
        self._host = host
        self._port = port
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._server: Any = None
        # 当前 Gateway 连接，用于 send_push 主动推送
        self._current_ws: Any = None
        self._current_send_lock: asyncio.Lock | None = None
        self._acp_client_capabilities_by_ws: dict[int, dict[str, Any]] = {}
        # AgentServer and the process CLI share this transport-independent Runtime.
        # Keep the manager alias for legacy transport handlers.
        self._runtime = self._build_runtime(
            plan_controller=_SERVER_PLAN_CONTROLLER,
        )
        self._agent_manager = self._runtime.agent_manager
        self._runtime_push_handler = None
        self._previous_runtime_push_handler = None
        self._runtime_services_started = False
        # RSI 服务域分发句柄（懒加载，见 _get_rsi_handlers）
        self._rsi_handlers = None
        # Optional production Provider injection point.  The concrete class is
        # supplied by the composition root once it is available.
        self._rsi_harness_provider: Any = None
        self._heartbeat_runtime = HeartbeatRailRuntime(self)
        self._runtime.set_admission_controller(self._heartbeat_runtime.admission)
        self._runtime.set_session_delete_lifecycle(self._heartbeat_runtime)
        self._agent_manager.set_heartbeat_service(self._heartbeat_runtime)
        self._install_session_message_service()
        # Gateway user-business RPCs execute in the current AgentServer's
        # injected data directory. Register adapters once per server; request
        # dispatch occurs before the legacy handler chain below.
        self._adapter_registry = AdapterRegistry()
        for adapter in (
            VoiceTaskServerAdapter(),
            SessionAdapter(),
            WorkspaceFileAdapter(),
            MemoryAdapter(),
            ProjectAdapter(runtime_probe=self._execution_runtime),
            HarmonyOSAdapter(),
            ConfigAdapter(on_config_saved=self._reload_after_config_save),
        ):
            self._adapter_registry.register(adapter)
        # AgentServer-side tokenizer cache/download service. The Gateway only
        # persists model profiles and notifies this process to refresh them.
        self._tokenizer_service = TokenizerService()
        # Tokenizer downloads are best-effort background work. Context creation
        # is local-only and falls back to string length while these tasks run.
        self._tokenizer_warmup_tasks: set[asyncio.Task] = set()
        # skills.* 等无状态 RPC：AgentManager 未缓存 agent 时复用的轻量 JiuWenSwarm，
        # 避免每次 cache miss 都 new 导致 SkillNet 异步安装等实例态断裂。
        self._stateless_fallback_agents: dict[str, Any] = {}
        self._asset_publish_api = None
        self._asset_publish_lock = asyncio.Lock()
        self._asset_start_task = None
        # session_id → all live stream tasks. This is host lifecycle tracking
        # for interrupt/connection cleanup only; it never decides interaction
        # output ownership.
        self._session_stream_tasks: dict[str, dict[asyncio.Task, asyncio.Event]] = {}
        # Scheduler service instance (for scheduled auto_harness tasks)
        self._scheduler_service: Optional[AutoHarnessService] = None
        self._scheduler_agent: Any = None
        # Model cache for scheduled task execution (same approach as interface_deep)
        self._model_cache: dict[str, Any] = {}
        self._default_model: Optional[Any] = None
        # 本地 jiuwenbox 子进程管理器 (lazy 启动, 在 /sandbox enable 时 ensure_running)
        self._jiuwenbox_runner = JiuwenBoxRunner.instance()
        # AgentServer 内唯一持有的进程内 PersonalContext Host；Context Rail 使用同一固定目录。
        self._personal_context_host = PersonalContextHostAPI(
            home=Path.home() / ".jiuwenswarm" / ".personal_context",
        )
        self._personal_context_start_task: asyncio.Task[None] | None = None
        # checkpointer 后台预热任务 (start() 里 fire-and-forget, stop() 时 cancel)
        self._checkpointer_warmup_task: Optional[asyncio.Task] = None
        # MCP 连接缓存预热任务 (同上, 建 Runner.resource_mgr 供首轮对话命中)
        self._mcp_prewarm_task: Optional[asyncio.Task] = None
        # 图像模态探针重探任务 (模型配置变更时拉起, stop() 时 cancel)
        self._image_modality_refresh_task: Optional[asyncio.Task] = None
        # Archive service for session/project lifecycle management
        self._archive_service = None
        self._login_credential_refresh_task: Optional[asyncio.Task] = None
        # Proactive recommendation engine (set by app_agentserver for debug trigger)
        self._proactive_engine: Any = None
        self._on_runtime_ready: Any = None
        self._on_runtime_warmup_retry: Any = None
        self._on_runtime_failed: Any = None
        get_acp_output_manager().set_send_push_callback(
            lambda msg: asyncio.create_task(self.send_push(msg))
        )

    def _install_session_message_service(self) -> None:
        """Create the AgentServer-owned durable mailbox for this Runtime."""

        service = SessionMessageService(
            store=SessionMessageStore(
                get_agent_root_dir() / "session_messages.sqlite3"
            ),
            admission=self._heartbeat_runtime.admission,
            execute=self.execute_internal_session_message,
            status_callback=self._push_session_message_status,
            on_abandoned_wait=self._release_abandoned_session_message_wait,
            requires_task_queue=self._runtime.session_message_requires_queue,
            available=self._current_ws is not None,
        )
        self._session_message_service = service
        self._runtime.set_session_message_service(service)

    async def _release_abandoned_session_message_wait(
        self, record: SessionMessageRecord
    ) -> None:
        await self._execution_runtime().release_session_message_interactions(
            record.target_session_id,
            request_id=record.execution_request_id,
        )

    def set_proactive_engine(self, engine: Any) -> None:
        """Store the proactive engine instance for debug trigger interface."""
        self._proactive_engine = engine

    def set_runtime_lifecycle_hooks(
        self,
        *,
        on_ready: Any = None,
        on_warmup_retry: Any = None,
        on_failed: Any = None,
    ) -> None:
        """Front readiness callbacks. ``on_ready`` is Agent Runtime start, not attach.

        Retryable warmup errors use ``on_warmup_retry`` (stay ``RUNTIME_WARMING``).
        ``on_failed`` is only for unrecoverable startup that stops retrying.
        """
        self._on_runtime_ready = on_ready
        self._on_runtime_warmup_retry = on_warmup_retry
        self._on_runtime_failed = on_failed

    def set_rsi_harness_provider(self, provider: Any) -> None:
        """Install the production ``HarnessProvider`` at the RSI seam."""
        self._rsi_harness_provider = provider
        handlers = self._rsi_handlers
        if handlers is not None:
            handlers.context.register_harness_provider(provider)

    @staticmethod
    def _ws_capabilities_key(ws: Any) -> int:
        return id(ws)

    def _set_ws_acp_client_capabilities(self, ws: Any, capabilities: dict[str, Any] | None) -> None:
        key = self._ws_capabilities_key(ws)
        if isinstance(capabilities, dict):
            self._acp_client_capabilities_by_ws[key] = dict(capabilities)
        else:
            self._acp_client_capabilities_by_ws.pop(key, None)

    def _get_ws_acp_client_capabilities(self, ws: Any) -> dict[str, Any]:
        key = self._ws_capabilities_key(ws)
        caps = self._acp_client_capabilities_by_ws.get(key)
        return dict(caps) if isinstance(caps, dict) else {}

    def _clear_ws_acp_client_capabilities(self, ws: Any) -> None:
        self._acp_client_capabilities_by_ws.pop(self._ws_capabilities_key(ws), None)

    def _release_current_connection(self, ws: Any) -> bool:
        if self._current_ws is not ws:
            return False
        self._current_ws = None
        self._current_send_lock = None
        return True

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
            host=host,
            port=port,
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
        )
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（仅用于测试）。"""
        cls._instance = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    # ---------- 生命周期 ----------

    def _schedule_tokenizer_warmup(
        self,
        config: dict[str, Any] | None,
        *,
        reason: str,
    ) -> None:
        """Schedule a non-blocking tokenizer warm-up task.

        The service-level lock deduplicates overlapping resolutions. Keeping
        each task until completion lets a reload submit its newest config
        without cancelling an in-flight worker-thread download.
        """

        async def _run() -> None:
            try:
                result = await self._tokenizer_service.warm(config, reason=reason)
                logger.info(
                    "[AgentWebSocketServer] tokenizer warm-up finished: "
                    "reason=%s warmed=%d degraded=%d failed=%d",
                    reason,
                    result.get("warmed", 0),
                    result.get("degraded", 0),
                    result.get("failed", 0),
                )
            except (
                AttributeError,
                ImportError,
                KeyError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as exc:
                logger.warning(
                    "[AgentWebSocketServer] tokenizer warm-up failed: reason=%s error=%s",
                    reason,
                    exc,
                )

        task = asyncio.create_task(_run(), name=f"tokenizer-warmup:{reason}")
        self._tokenizer_warmup_tasks.add(task)
        task.add_done_callback(self._tokenizer_warmup_tasks.discard)

    @staticmethod
    async def _start_symphony_recovery() -> None:
        """Recover Flow candidates without affecting AgentServer availability."""

        try:
            from jiuwenswarm.symphony.service import get_swarm_symphony_service

            await get_swarm_symphony_service().start()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AgentWebSocketServer] Symphony Flow recovery failed: %s",
                exc,
            )

    def attach_gateway_connection(self, ws: Any, send_lock: asyncio.Lock) -> None:
        """Publish the Front-owned Gateway socket for send_push / ACP caps."""
        self._current_ws = ws
        self._current_send_lock = send_lock

    async def on_gateway_disconnect(self, ws: Any, remote: Any) -> None:
        """Runtime-side cleanup when Front drops the Gateway connection."""
        if self._current_ws is ws:
            self._current_ws = None
            self._current_send_lock = None
        self._clear_ws_acp_client_capabilities(ws)
        try:
            await self._execution_runtime().cancel_all_inflight_work(
                reason=f"[gateway ws closed {remote}] ",
                exclude_session_ids=(
                    self._heartbeat_runtime.execution.active_session_ids()
                ),
            )
        except Exception:
            logger.exception("[AgentWebSocketServer] cancel_all_inflight_work failed")
        try:
            await self._stop_scheduler()
        except Exception:
            logger.exception("[AgentWebSocketServer] scheduler stop failed")
        try:
            await self._execution_runtime().cancel_all_team_stream_tasks(
                reason=f"[gateway ws closed {remote}] ",
                exclude_session_ids=(
                    self._heartbeat_runtime.execution.active_session_ids()
                ),
            )
        except Exception:
            logger.exception("[AgentWebSocketServer] team stream cancel failed")
        self._session_stream_tasks.clear()

    async def start(self, *, bind_transport: bool = False) -> None:
        """Start Runtime services. Production Front owns the listen socket.

        ``bind_transport=True`` is the test/compat entry only. AgentServer
        main always calls ``bind_transport=False`` so there is a single
        Gateway-facing port.
        """
        if bind_transport:
            logger.warning(
                "[AgentWebSocketServer] bind_transport=True is a test/compat "
                "entry; production listens through AgentServer Front"
            )
        if bind_transport and self._server is not None:
            logger.warning("[AgentWebSocketServer] 服务端已在运行")
            return
        if not bind_transport and self._runtime_services_started:
            logger.warning("[AgentWebSocketServer] Runtime 服务已在运行")
            return

        owner = self._kv_cache_application_owner
        await owner.activate_from_config()

        from jiuwenswarm.server.runtime.session.session_archive import SessionArchiveService
        self._archive_service = SessionArchiveService(self._execution_runtime())
        self._archive_service.start_recovery()

        # Reset harness package state to native on service startup
        reset_harness_packages_state()

        if bind_transport:
            try:
                from websockets.legacy.server import serve as legacy_serve
                self._server = await legacy_serve(
                    self._connection_handler,
                    self._host,
                    self._port,
                    process_request=self._process_request,
                    ping_interval=self._ping_interval,
                    ping_timeout=self._ping_timeout,
                    max_size=AGENT_WS_MAX_MESSAGE_BYTES,
                )
            except ImportError:
                import websockets
                self._server = await websockets.serve(
                    self._connection_handler,
                    self._host,
                    self._port,
                    process_request=self._process_request,
                    ping_interval=self._ping_interval,
                    ping_timeout=self._ping_timeout,
                    max_size=AGENT_WS_MAX_MESSAGE_BYTES,
                )
        else:
            self._server = None
        self._runtime_services_started = True
        self._runtime_push_handler = self.send_push
        self._previous_runtime_push_handler = install_runtime_push_handler(
            self._runtime_push_handler
        )
        logger.info(
            "[AgentWebSocketServer] 已启动: ws://%s:%s", self._host, self._port
        )

        self._asset_start_task = asyncio.create_task(self._start_asset_services())

        # The port is already listening. Remote tokenizer downloads must not
        # delay startup; ContextEngine is local-only and uses string fallback
        # if this task has not completed when the first context is created.
        self._schedule_tokenizer_warmup(get_config(), reason="startup")

        # 端口已 listen, 后台预热 checkpointer, 不阻塞启动与握手.
        # _checkpointer_warmup_task 供 shutdown 时 cancel, 避免任务悬挂.
        async def _start_runtime() -> None:
            retry_delay = 1.0
            while True:
                try:
                    await self._runtime.start()
                    await self._heartbeat_runtime.start()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[AgentWebSocketServer] Runtime warmup failed; "
                        "retrying in %.1fs: %s",
                        retry_delay,
                        exc,
                    )
                    retry = self._on_runtime_warmup_retry
                    if callable(retry):
                        retry(str(exc))
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(30.0, retry_delay * 2)
                    continue
                ready = self._on_runtime_ready
                if callable(ready):
                    ready()
                await self._start_symphony_recovery()
                return

        self._checkpointer_warmup_task = asyncio.create_task(
            _start_runtime(), name="runtime-start"
        )

        async def _warmup_mcp_connections() -> None:
            try:
                from jiuwenswarm.common.mcp_config import prewarm_connected_mcps
                await prewarm_connected_mcps()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[AgentWebSocketServer] MCP prewarm failed "
                    "(will lazy-connect on first chat): %s", exc,
                )

        # 端口已 listen, 后台预热 connected MCP 的进程级连接缓存, 不阻塞启动与握手.
        self._mcp_prewarm_task = asyncio.create_task(
            _warmup_mcp_connections(), name="mcp-prewarm"
        )
        self._personal_context_start_task = asyncio.create_task(
            self._start_personal_context_best_effort(),
            name="personal-context-host-start",
        )
        # 登录模型的 id_token 一小时过期：在用且快过期的，请Gateway续期后推回来
        from jiuwenswarm.common.auth.login_credentials import run_refresh_requests
        from jiuwenswarm.common.auth.remote_config import warm_up_in_background

        # 官网配置在后台先拉一次：AgentServer 读它的地方都在逐请求的热路径上，
        # 不预热的话第一个请求拿到的是空配置（认不出免费模型）
        warm_up_in_background()
        self._login_credential_refresh_task = asyncio.create_task(
            run_refresh_requests(self.send_push), name="login-credential-refresh"
        )
        # WS 监听已经开放, 现在按 config.yaml::sandbox 的 runtime.enabled +
        # startup_mode 决定要不要自动把 jiuwenbox 子进程也拉起来。失败不阻塞
        # 启动 (用户依然可以在 TUI 里跑 /sandbox enable 重试)。
        await self._bootstrap_internal_jiuwenbox()

    def schedule_image_modality_warmup(
        self, *, reason: str, reset_cache: bool = False
    ) -> None:
        """把图像模态探针任务放进统一槽位调度。

        启动预热与模型配置变更重探共用 ``_image_modality_refresh_task`` 这一个
        任务槽位：新任务启动前取消上一轮未完成的任务，避免启动预热在配置变化
        后继续跑完并写回过期结论（新配置先 reset 缓存、旧任务随后覆盖）。
        任务由 ``_stop_main_services`` 在 shutdown 时统一 cancel 回收。

        Args:
            reason: 传给 warm/refresh 的日志标签（"startup" / "model config change"）。
            reset_cache: True 时先清空旧结论再探（配置变更场景），False 仅补探。
        """
        from jiuwenswarm.server.runtime.image_modality_warmup import (
            refresh_image_modality_cache,
            warm_image_modality_cache,
        )

        previous_task = self._image_modality_refresh_task
        if previous_task is not None and not previous_task.done():
            previous_task.cancel()
        if reset_cache:
            coro = refresh_image_modality_cache(get_config(), reason=reason)
        else:
            coro = warm_image_modality_cache(get_config(), reason=reason)
        self._image_modality_refresh_task = asyncio.create_task(
            coro, name=f"image-modality-warmup-{reason}"
        )

    async def _start_personal_context_best_effort(self) -> None:
        """Start optional PersonalContext without changing AgentServer readiness."""
        start_cancelled: asyncio.CancelledError | None = None
        try:
            await self._personal_context_host.start()
        except asyncio.CancelledError as exc:
            start_cancelled = exc
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AgentWebSocketServer] optional PersonalContext startup failed: %s",
                type(exc).__name__,
            )
            return
        if start_cancelled is not None:
            raise start_cancelled

        rail_sync_cancelled: asyncio.CancelledError | None = None
        try:
            state_reader = getattr(
                self._personal_context_host, "is_runtime_enabled", None
            )
            enabled = bool(await state_reader()) if callable(state_reader) else False
            manager_setter = getattr(
                self._agent_manager, "set_personal_context_runtime_enabled", None
            )
            if callable(manager_setter):
                await manager_setter(enabled)
        except asyncio.CancelledError as exc:
            rail_sync_cancelled = exc
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AgentWebSocketServer] optional PersonalContext Rail sync failed: %s",
                type(exc).__name__,
            )
        if rail_sync_cancelled is not None:
            raise rail_sync_cancelled

    async def _bootstrap_internal_jiuwenbox(self) -> None:
        """启动时按 ``config.yaml::sandbox`` 自动拉起 jiuwenbox 子进程。

        触发条件: ``config.yaml::sandbox.startup_mode`` **显式**写为 ``internal``。
        这里刻意走 :func:`get_sandbox_startup_mode_explicit` 而不是
        :func:`get_sandbox_startup_mode` —— 后者在字段缺失时默认回落到
        ``internal``, 会让没在用沙箱的用户升级版本后突然多出 jiuwenbox 进程;
        boot 阶段必须严格区分 "用户写过 internal" 和 "走默认值"。

        不再单独依赖 ``sandbox.enabled``:
        - 老逻辑要 ``enabled=True`` AND ``startup_mode=internal`` 才拉, 但
          ``enabled`` 是 ``/sandbox`` 命令的产物, 用户手改 yaml 设了 ``internal``
          的话很容易漏配 ``enabled`` → boot 时一声不吭跳过, 体验差。
        - 现在: 只要 ``startup_mode=internal`` 就拉; 成功后顺手把
          ``sandbox.enabled`` 同步成 ``True``, ``/sandbox status`` 显示与实际
          运行的 jiuwenbox 一致。
        - ``/sandbox disable`` 仍然会停 jiuwenbox 并把 ``enabled`` 置 ``False``,
          但**重启后会被本方法重新拉起** (因为 ``startup_mode`` 没改)。要让
          disable 跨重启生效, 把 ``startup_mode`` 改为 ``external`` 或从 yaml
          里删掉该字段即可。

        与沙箱命令里的启用逻辑（``handlers/sandbox.py`` 的
        ``_handle_sandbox_enable``）其余差别:
        - 不调用 ``agent_manager.recreate_agent``: 启动阶段还没有任何会话/agent
          实例, 没东西需要重建; 后续会话首次进入时按现有 ``sandbox.url`` 直接装载。
        - 严格 best-effort: 任何失败 (policy 缺失 / 端口/spawn 失败) 一律记
          warning, 绝不让 agent-server 自身启动失败 (否则运维误配 yaml 会让整
          产品起不来, 也无从修复)。
        """
        try:
            # 非 Linux 平台直接跳过 auto-start: jiuwenbox 依赖 bwrap / Landlock /
            # 命名空间, Windows / macOS 起不来; 即便 spawn 成功后续 /sandbox 命
            # 令也会被沙箱命令入口的平台检查拒掉, 留着只会浪费一
            # 次失败的子进程启动。
            if not sys.platform.startswith("linux"):
                logger.info(
                    "[AgentWebSocketServer] skipping jiuwenbox auto-start: "
                    "/sandbox is only supported on Linux (current platform: %r)",
                    sys.platform,
                )
                return
            explicit_mode = get_sandbox_startup_mode_explicit()
            if explicit_mode is None:
                logger.info(
                    "[AgentWebSocketServer] sandbox.startup_mode 未在 config.yaml "
                    "中显式配置, skipping jiuwenbox auto-start (走默认 host 模式; "
                    "如需 agent-server 自动拉起 jiuwenbox 子进程, 设置 "
                    "sandbox.startup_mode: internal)"
                )
                return
            if explicit_mode != "internal":
                logger.info(
                    "[AgentWebSocketServer] sandbox.startup_mode=%r, skipping "
                    "jiuwenbox auto-start (external 模式由用户自行拉起 "
                    "jiuwenbox-server)",
                    explicit_mode,
                )
                return

            # startup_mode=internal 已经定下来; 其余字段从归一后的 endpoint
            # 取, 缺啥用默认。
            endpoint = get_sandbox_endpoint()
            url = endpoint.get("url") or "http://127.0.0.1:8321"
            sandbox_type = endpoint.get("type") or "jiuwenbox"
            # yuanrong 不需要本机 jiuwenbox 进程; 仅通过 config 启用 SysOperation。
            if str(sandbox_type).strip().lower() == "yuanrong":
                logger.info(
                    "[AgentWebSocketServer] sandbox.type=yuanrong, skipping "
                    "jiuwenbox auto-start (YuanRong uses YR_* env + yr.init)"
                )
                return
            raw_policy = endpoint.get("policy_file") or ""
            effective_policy_file = raw_policy or DEFAULT_SANDBOX_POLICY_FILE
            policy_path = resolve_sandbox_policy_path(effective_policy_file)
            if policy_path is None or not policy_path.is_file():
                logger.warning(
                    "[AgentWebSocketServer] sandbox auto-start skipped: "
                    "policy_file=%r 无法解析到一个存在的文件 "
                    "(resolved=%s). 进 TUI 跑 /sandbox enable 重试或修复 "
                    "config.yaml::sandbox.policy_file。",
                    effective_policy_file,
                    policy_path,
                )
                return

            host, preferred_port = self._parse_sandbox_host_port(url)
            port = self._allocate_internal_jiuwenbox_port(host, preferred_port)
            if port != preferred_port:
                url = f"http://{host}:{port}"
                logger.info(
                    "[AgentWebSocketServer] jiuwenbox auto-start: "
                    "preferred port %d busy, using %d",
                    preferred_port,
                    port,
                )

            try:
                api_token = resolve_sandbox_api_token(startup_mode="internal")
            except ValueError as exc:
                logger.warning(
                    "[AgentWebSocketServer] sandbox token 配置无效, "
                    "跳过 jiuwenbox auto-start: %s",
                    exc,
                )
                return
            # Sync onto the agent-server process so provider HTTP clients /
            # hybrid-shell host orchestration inherit the same Bearer token.
            sync_sandbox_api_token_environ(api_token)

            ok = await self._jiuwenbox_runner.ensure_running(
                host=host,
                port=port,
                startup_mode="internal",
                policy_path=policy_path,
                api_token=api_token,
            )
            if not ok:
                stderr_tail = self._jiuwenbox_runner.get_stderr_tail(10)
                logger.warning(
                    "[AgentWebSocketServer] jiuwenbox auto-start failed at "
                    "%s:%d (policy=%s); 进 TUI 跑 /sandbox enable 重试。"
                    " stderr tail:\n%s",
                    host,
                    port,
                    policy_path,
                    stderr_tail or "(empty)",
                )
                return

            # 端口可能在 _allocate_internal_jiuwenbox_port 里换过, 把最终生效
            # 的 url 落盘, 这样 (a) 后续会话/agent 重建直接读到正确端点,
            # (b) /sandbox status 显示也是真实值, 不再是 config 里旧的 8321。
            try:
                update_sandbox_endpoint(
                    url,
                    sandbox_type,
                    startup_mode="internal",
                    policy_file=effective_policy_file,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[AgentWebSocketServer] persist sandbox endpoint failed "
                    "after auto-start: %s",
                    exc,
                )

            # auto-start 成功 → ``runtime.enabled`` 同步为 True, 这样 /sandbox
            # status / TUI 显示的状态跟真实运行的 jiuwenbox 对齐。如果用户上次
            # /sandbox disable 留下了 False, 这里会被覆盖 —— 这是已知的、属于
            # 上面 docstring 提到的 "disable 不跨重启" 语义的一部分。
            try:
                update_sandbox_runtime({"enabled": True})
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[AgentWebSocketServer] persist sandbox.enabled=True "
                    "failed after auto-start: %s",
                    exc,
                )

            logger.info(
                "[AgentWebSocketServer] jiuwenbox auto-started at %s "
                "(policy=%s)",
                url,
                policy_path,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "[AgentWebSocketServer] jiuwenbox auto-start raised an "
                "unexpected error; skipping (用户可在 TUI 里 /sandbox enable 重试)"
            )

    async def _stop_scheduler(self) -> None:
        """Stop the auto_harness scheduler."""
        try:
            if self._scheduler_service is not None:
                await self._scheduler_service.stop_scheduler()
                logger.info("[AgentWebSocketServer] Scheduler stopped")
        except Exception as e:
            logger.warning("[AgentWebSocketServer] Failed to stop scheduler: %s", e)
        finally:
            self._scheduler_service = None
            scheduler_agent = getattr(self, "_scheduler_agent", None)
            if scheduler_agent is not None:
                unpin = getattr(self._agent_manager, "unpin_agent", None)
                if callable(unpin):
                    unpin(scheduler_agent)
            self._scheduler_agent = None

    def _set_scheduler_agent(self, agent: Any) -> None:
        """Pin the facade whose DeepAgent is retained by the scheduler."""
        previous = getattr(self, "_scheduler_agent", None)
        if previous is agent:
            return
        pin = getattr(self._agent_manager, "pin_agent", None)
        if callable(pin):
            pin(agent)
        self._scheduler_agent = agent
        if previous is not None:
            unpin = getattr(self._agent_manager, "unpin_agent", None)
            if callable(unpin):
                unpin(previous)

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

    async def _stop_personal_context_best_effort(self) -> None:
        """Cancel PersonalContext startup and stop PersonalContext without masking main shutdown."""
        start_task = self._personal_context_start_task
        self._personal_context_start_task = None
        if start_task is not None:
            if not start_task.done():
                start_task.cancel()
            try:
                await start_task
            except asyncio.CancelledError:
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[AgentWebSocketServer] optional PersonalContext startup cleanup failed: %s",
                    type(exc).__name__,
                )
        stop_cancelled: asyncio.CancelledError | None = None
        try:
            await self._personal_context_host.stop()
        except asyncio.CancelledError as exc:
            stop_cancelled = exc
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AgentWebSocketServer] optional PersonalContext stop failed: %s",
                type(exc).__name__,
            )
        if stop_cancelled is not None:
            raise stop_cancelled

    async def stop(self) -> None:
        """Stop the remote service and prepare a fresh Runtime for restart.

        The current Runtime is permanently closed. After shutdown this server
        owns a new Runtime/AgentManager pair, so a later start() restores the
        established Gateway/WebSocket service contract. Callers must not retain
        the pre-stop Runtime instance. If Runtime close is rejected before any
        resources are released, the original Runtime is retained and the error
        is propagated so its unfinished operation can be finalized before a
        retry.
        """
        try:
            await self._stop_main_services()
        finally:
            await self._stop_personal_context_best_effort()

    async def _stop_main_services(self) -> None:
        """Stop AgentServer-owned services before optional host cleanup."""
        task = getattr(self, "_asset_start_task", None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._asset_start_task = None
        api = getattr(self, "_asset_publish_api", None)
        if api is not None:
            await api.close()
            self._asset_publish_api = None
        from jiuwenswarm.server.runtime.marketplace.hub_catalog_cache import close_hub_catalog_cache
        await close_hub_catalog_cache()
        archive_service = getattr(self, "_archive_service", None)
        if archive_service is not None:
            await archive_service.close()
            self._archive_service = None
        tokenizer_tasks = tuple(self._tokenizer_warmup_tasks)
        self._tokenizer_warmup_tasks.clear()
        for task in tokenizer_tasks:
            if not task.done():
                task.cancel()
        if tokenizer_tasks:
            await asyncio.gather(*tokenizer_tasks, return_exceptions=True)
        session_message_service = getattr(self, "_session_message_service", None)
        if session_message_service is not None:
            await session_message_service.stop()
        # 先取消 checkpointer 预热任务, 避免在 server 关闭后仍在后台跑.
        warmup = self._checkpointer_warmup_task
        self._checkpointer_warmup_task = None
        if warmup is not None and not warmup.done():
            warmup.cancel()
            try:
                await warmup
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("[AgentWebSocketServer] checkpointer warmup cancel failed: %s", exc)
        await self._heartbeat_runtime.stop()
        # 同理取消 MCP 连接缓存预热任务.
        mcp_prewarm = self._mcp_prewarm_task
        self._mcp_prewarm_task = None
        if mcp_prewarm is not None and not mcp_prewarm.done():
            mcp_prewarm.cancel()
            try:
                await mcp_prewarm
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("[AgentWebSocketServer] MCP prewarm cancel failed: %s", exc)
        credential_refresh = self._login_credential_refresh_task
        self._login_credential_refresh_task = None
        if credential_refresh is not None and not credential_refresh.done():
            credential_refresh.cancel()
            await asyncio.gather(credential_refresh, return_exceptions=True)
        # 同理取消图像模态重探任务.
        image_modality_refresh = self._image_modality_refresh_task
        self._image_modality_refresh_task = None
        if image_modality_refresh is not None and not image_modality_refresh.done():
            image_modality_refresh.cancel()
            try:
                await image_modality_refresh
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[AgentWebSocketServer] image modality refresh cancel failed: %s", exc
                )
        had_server = self._server is not None
        runtime_only = self._runtime_services_started and not had_server
        if had_server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        closing_runtime = self._runtime
        runtime_close_completed = False
        runtime_close_error: BaseException | None = None
        # Stop optional remote KVC activity as soon as ingress is closed.  This
        # must also happen when Runtime close is later rejected by an unfinished
        # two-phase Session operation.  suspend() performs local cancellation
        # only; it never closes the shared KVC runtime or sends root eviction.
        try:
            await self._suspend_kv_cache()
        except BaseException as exc:  # preserve cancellation until host cleanup
            runtime_close_error = exc
        try:
            await closing_runtime.close()
            runtime_close_completed = True
        except BaseException as exc:  # preserve cancellation until host cleanup
            if runtime_close_error is None:
                runtime_close_error = exc
            if isinstance(exc, Exception):
                logger.warning(
                    "[AgentWebSocketServer] runtime shutdown failed: %s",
                    exc,
                )
        finally:
            # Do not discard an open Runtime after a fail-fast close.  An
            # unfinished two-phase provision still needs its original owner to
            # commit or abort it before shutdown can be retried.
            if runtime_close_completed or closing_runtime.closed:
                # AgentRuntime is intentionally one-shot for process-style CLI
                # commands. AgentServer historically supports start after stop,
                # so prepare a fresh Runtime, manager and plan-state owner for
                # its next lifecycle. Plan state is process-local and must not
                # cross a completed stop/start boundary.
                plan_controller = _renew_server_plan_controller()
                self._runtime = self._build_runtime(
                    plan_controller=plan_controller,
                )
                self._agent_manager = self._runtime.agent_manager
                self._heartbeat_runtime = HeartbeatRailRuntime(self)
                self._runtime.set_admission_controller(
                    self._heartbeat_runtime.admission
                )
                self._runtime.set_session_delete_lifecycle(
                    self._heartbeat_runtime
                )
                self._agent_manager.set_heartbeat_service(
                    self._heartbeat_runtime
                )
                self._install_session_message_service()
                self._adapter_registry = AdapterRegistry()
                for adapter in (
                    VoiceTaskServerAdapter(),
                    SessionAdapter(),
                    WorkspaceFileAdapter(),
                    MemoryAdapter(),
                    ProjectAdapter(runtime_probe=self._execution_runtime),
                    HarmonyOSAdapter(),
                    ConfigAdapter(on_config_saved=self._reload_after_config_save),
                ):
                    self._adapter_registry.register(adapter)

        try:
            from jiuwenswarm.symphony.service import get_swarm_symphony_service

            await get_swarm_symphony_service().close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AgentWebSocketServer] Symphony runtime close failed: %s",
                exc,
            )

        runtime_push_handler = getattr(self, "_runtime_push_handler", None)
        if runtime_push_handler is not None:
            restore_runtime_push_handler(
                runtime_push_handler,
                getattr(self, "_previous_runtime_push_handler", None),
            )
            self._runtime_push_handler = None

        if not had_server and not runtime_only:
            if runtime_close_error is not None and (
                not isinstance(runtime_close_error, Exception)
                or not closing_runtime.closed
            ):
                raise runtime_close_error
            return
        try:
            await self._jiuwenbox_runner.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[AgentWebSocketServer] jiuwenbox_runner.stop failed: %s", exc)
        if runtime_close_error is not None and (
            not isinstance(runtime_close_error, Exception)
            or not closing_runtime.closed
        ):
            raise runtime_close_error
        self._runtime_services_started = False
        logger.info("[AgentWebSocketServer] 已停止")

    async def _suspend_kv_cache(self) -> None:
        """Detach KVC locally without sending management requests during stop."""
        owner = getattr(self, "_kv_cache_application_owner", None)
        if owner is None:
            return
        try:
            await owner.suspend()
        except Exception:
            logger.warning("KVC application suspend failed; continue shutdown", exc_info=True)

    def _build_runtime(
        self,
        *,
        plan_controller: Any,
        agent_manager: Any | None = None,
        reuse_dependencies: bool = False,
    ) -> AgentRuntime:
        """Build every AgentServer Runtime through one product composition root."""
        from jiuwenswarm.agents.harness.team import get_team_manager
        from jiuwenswarm.runtime.session_lifecycle import RuntimeParticipantRegistry
        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_application_owner import (
            get_kv_cache_application_owner,
        )

        owner = get_kv_cache_application_owner()
        self._kv_cache_application_owner = owner
        registry = RuntimeParticipantRegistry()
        lease = owner.attach_runtime(registry)
        return AgentRuntime(
            agent_manager=agent_manager,
            initializer=(
                _reuse_server_runtime_dependencies if reuse_dependencies else None
            ),
            plan_controller=plan_controller,
            admission_controller=getattr(
                getattr(self, "_heartbeat_runtime", None),
                "admission",
                None,
            ),
            session_delete_lifecycle=getattr(self, "_heartbeat_runtime", None),
            participant_registry=registry,
            resource_lease=lease,
            team_execution_controller=get_team_manager(None),
        )

    # ---------- 连接处理 ----------

    async def _connection_handler(self, ws: Any) -> None:
        """处理单个 Gateway WebSocket 连接，同一连接可并发处理多个请求."""
        remote = ws.remote_address
        logger.info("[AgentWebSocketServer] 新连接: %s", remote)

        send_lock = asyncio.Lock()
        self._current_ws = ws
        self._current_send_lock = send_lock
        session_message_service = getattr(self, "_session_message_service", None)

        # 发送 connection.ack 事件，通知 Gateway 服务端已就绪
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
            if session_message_service is not None:
                await session_message_service.set_available(False)
            owns_current_connection = self._release_current_connection(ws)
            self._clear_ws_acp_client_capabilities(ws)
            connection_tasks = list(tasks)
            for task in connection_tasks:
                if not task.done():
                    task.cancel()
            if owns_current_connection:
                # Gateway 进程退出/端口关闭时，必须先取消各 session 内流式生产者（SessionManager）
                # 并中止 DeepAgent 内层循环；否则仅等待 _handle_message 任务结束会一直阻塞到任务自然完成。
                try:
                    await self._execution_runtime().cancel_all_inflight_work(
                        reason=f"[gateway ws closed {remote}] ",
                        exclude_session_ids=(
                            self._heartbeat_runtime.execution.active_session_ids()
                        ),
                    )
                except Exception:
                    logger.exception(
                        "[AgentWebSocketServer] cancel_all_inflight_work failed"
                    )
                # Stop scheduler on server shutdown
                try:
                    await self._stop_scheduler()
                except Exception:
                    logger.exception("[AgentWebSocketServer] scheduler stop failed")
                try:
                    await self._execution_runtime().cancel_all_team_stream_tasks(
                        reason=f"[gateway ws closed {remote}] ",
                        exclude_session_ids=(
                            self._heartbeat_runtime.execution.active_session_ids()
                        ),
                    )
                except Exception:
                    logger.exception("[AgentWebSocketServer] team stream cancel failed")
            if connection_tasks:
                await asyncio.gather(*connection_tasks, return_exceptions=True)
            if owns_current_connection:
                self._session_stream_tasks.clear()

    async def _dispatch_gateway_adapter_request(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> bool:
        """Dispatch migrated Gateway user-business RPCs to their adapters.

        The registry is intentionally checked before the legacy handler chain:
        AgentOS requests must operate on this process's injected user directory.
        A small set of legacy handlers is retained when it owns runtime cleanup.
        """
        if request.req_method is None or request.req_method in _GATEWAY_ADAPTER_LEGACY_METHODS:
            return False
        registry = getattr(self, "_adapter_registry", None)
        if registry is None:
            return False
        adapter = registry.get(request.req_method.value)
        if adapter is None:
            return False
        try:
            response = await adapter.handle(request)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "[AgentWebSocketServer] Gateway adapter failed: request_id=%s method=%s",
                request.request_id,
                request.req_method.value,
            )
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "INTERNAL_ERROR"},
                metadata=request.metadata,
            )
        # The mailbox belongs to this AgentServer, not the metadata adapter.
        if (
            request.req_method == ReqMethod.SESSION_GET_METADATA
            and request.channel_id == "web"
            and response.ok
        ):
            service = getattr(self, "_session_message_service", None)
            if service is not None and isinstance(response.payload, dict):
                try:
                    params = request.params if isinstance(request.params, dict) else {}
                    session_id = str(params.get("session_id") or "")
                    response.payload["queued_session_messages"] = (
                        await service.queued_for_target(session_id, request.user_id)
                    )
                except Exception:
                    logger.warning(
                        "[AgentWebSocketServer] queue snapshot failed: session_id=%s",
                        session_id,
                        exc_info=True,
                    )
        if getattr(response, "agent_ref", None) is None:
            response.agent_ref = request.agent_ref
        wire = encode_agent_response_for_wire(response, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)
        return True

    async def _handle_gateway_cron_callback(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> bool:
        """Consume Gateway-owned cron callbacks without creating a chat turn."""
        if request.req_method not in {
            ReqMethod.CRON_JOBS_SYNC,
            ReqMethod.CRON_COMMAND_ACK,
            ReqMethod.CRON_RUN_NOW_ACK,
        }:
            return False

        params = request.params if isinstance(request.params, dict) else {}
        from jiuwenswarm.agents.harness.common.tools.cron.cron_tools import (
            install_gateway_jobs_snapshot,
            resolve_gateway_cron_command_ack,
            resolve_gateway_run_ack,
        )

        if request.req_method == ReqMethod.CRON_JOBS_SYNC:
            install_gateway_jobs_snapshot(
                params.get("jobs", []), user_id=request.user_id
            )
        elif request.req_method == ReqMethod.CRON_COMMAND_ACK:
            resolve_gateway_cron_command_ack(
                str(params.get("command_id") or ""),
                {"data": params.get("data")},
            )
        else:
            resolve_gateway_run_ack(
                str(params.get("ack_request_id") or ""),
                str(params.get("run_id") or ""),
            )

        response = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"status": "ok"},
            metadata=request.metadata,
            agent_ref=request.agent_ref,
        )
        wire = encode_agent_response_for_wire(response, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)
        return True

    async def _handle_message(self, ws: Any, raw: str | bytes, send_lock: asyncio.Lock) -> None:
        """解析一条 JSON 请求并分发到 IAgentServer 处理."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            wire = encode_json_parse_error_wire(
                request_id="",
                channel_id="",
                message=f"JSON 解析失败: {e}",
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
                "[AgentWebSocketServer] E2A from_dict 失败，按旧载荷解析: %s",
                parse_err,
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

    async def dispatch_parsed_request(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Dispatch an already-parsed request. Used by Front after CONTROL_READY."""
        if request.req_method == ReqMethod.CHAT_SEND:
            server_logger.info(
                "[AgentServer] chat input received: request_id=%s session_id=%s channel_id=%s query=%s",
                request.request_id,
                request.session_id,
                request.channel_id,
                preview_text(_request_query_text(request)),
            )

        pending_chat_request: tuple[AgentRuntime, str, str] | None = None
        try:
            if request.req_method is not None and request.req_method.value.startswith("assets.publish."):
                await self._handle_asset_publish(ws, request, send_lock)
                return
            if request.req_method in _PERSONAL_CONTEXT_REQ_METHODS:
                manager = getattr(self, "_agent_manager", None)
                runtime_callback = getattr(
                    manager, "set_personal_context_runtime_enabled", None
                )
                await handle_personal_context_request(
                    self._personal_context_host,
                    ws,
                    request,
                    send_lock,
                    runtime_enabled_changed=(
                        runtime_callback if callable(runtime_callback) else None
                    ),
                )
                return

            if request.channel_id == "acp" and request.req_method != ReqMethod.INITIALIZE:
                metadata = dict(request.metadata or {})
                ws_caps = self._get_ws_acp_client_capabilities(ws)
                metadata.setdefault(
                    "acp_client_capabilities",
                    ws_caps or self._agent_manager.get_client_capabilities("acp"),
                )
                request.metadata = metadata

            if await self._handle_gateway_cron_callback(ws, request, send_lock):
                return

            if await self._handle_lifecycle_request(ws, request, send_lock):
                return
            from jiuwenswarm.server.runtime.session.lifecycle import guard, LifecycleError
            guarded_params = request.params if isinstance(request.params, dict) else {}
            guarded_method = request.req_method.value if request.req_method else ""
            unguarded_methods = {
                "session.list", "project.list", "project.info", "project.get_sessions",
                "project.get_cron_sessions", "project.pinned_sessions", "chat.cancel",
                "session.stop", "voice.task.checkpoint.ack",
            }
            if guarded_method not in unguarded_methods:
                try:
                    guard(
                        str(guarded_params.get("session_id") or request.session_id or ""),
                        str(guarded_params.get("project_id") or ""),
                    )
                except LifecycleError as exc:
                    resp = AgentResponse(request_id=request.request_id, channel_id=request.channel_id, ok=False,
                                         payload={"code": exc.code, "error": str(exc)}, metadata=request.metadata)
                    async with send_lock:
                        await send_wire_payload(
                            ws, encode_agent_response_for_wire(resp, response_id=request.request_id)
                        )
                    return

            if await self._dispatch_gateway_adapter_request(ws, request, send_lock):
                return

            if request.req_method in {
                ReqMethod.CHAT_SEND, ReqMethod.CHAT_RESUME, ReqMethod.CHAT_ANSWER,
            } and request.session_id:
                runtime = self._execution_runtime()
                runtime.begin_chat_request(request.session_id, request.request_id)
                pending_chat_request = (runtime, request.session_id, request.request_id)

            # Extensions must observe and may normalize chat input before
            # automatic team binding or any other request-side effect. Runtime
            # execution below is told not to trigger this hook a second time.
            await self._trigger_before_chat_request_hook(request)

            # 已搬到 handlers/ 的请求在这里查表。没命中的仍走下面原来的判断。
            # 放在会话守卫和聊天前钩子之后，这些前置步骤的行为保持不变。
            if await try_dispatch(build_ws_request_context(self, ws, request, send_lock)):
                return

            if request.req_method == ReqMethod.HEARTBEAT_JOB:
                await self._handle_heartbeat_job(ws, request, send_lock)
                return

            if request.req_method == ReqMethod.TEAM_TEMPLATES_LIST:
                await self._handle_team_templates_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_BINDINGS_LIST:
                await self._handle_team_bindings_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_BINDING_CREATE:
                await self._handle_team_binding_create(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_BINDING_GENERATE:
                await self._handle_team_binding_generate(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_SESSION_BIND:
                await self._handle_team_session_bind(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_DELETE:
                await self._handle_team_delete(ws, request, send_lock)
                return
            if request.req_method in get_a4p_req_methods():
                await self._handle_a4p_request(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_SNAPSHOT:
                await self._handle_team_snapshot(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_MQ_PUBLISH:
                await self._handle_team_mq_publish(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.SWARMFLOW_PAUSE:
                await self._handle_swarmflow_pause(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.SWARMFLOW_RESUME:
                await self._handle_swarmflow_resume(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.SWARMFLOW_STOP:
                await self._handle_swarmflow_stop(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_HISTORY_GET:
                await self._handle_team_history_get(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_MEMBERS_GET:
                await self._handle_team_members_get(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_LIST:
                await self._handle_team_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.ORG_SNAPSHOT:
                await self._handle_org_snapshot(ws, request, send_lock)
                return
            # RSI 优化平台：16 个 rsi.* web method 统一分发（B2）
            if (
                isinstance(request.req_method, ReqMethod)
                and request.req_method.value.startswith("rsi.")
            ):
                await self._handle_rsi_request(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.CHAT_CANCEL:
                if isinstance(request.params, dict) and request.params.get("target_team_id"):
                    await self._handle_cancel(ws, request, send_lock)
                    return
                if isinstance(request.params, dict) and request.params.get("wait_for_stop"):
                    try:
                        await self._execution_runtime().stop_session_for_archive(
                            channel_id=request.channel_id or "default",
                            session_id=request.session_id or "default",
                        )
                        response = AgentResponse(
                            request_id=request.request_id, channel_id=request.channel_id,
                            ok=True, payload={"success": True},
                        )
                    except Exception as exc:
                        response = AgentResponse(
                            request_id=request.request_id, channel_id=request.channel_id,
                            ok=False, payload={"success": False, "error": str(exc)},
                        )
                    async with send_lock:
                        await send_wire_payload(
                            ws, encode_agent_response_for_wire(response, response_id=request.request_id)
                        )
                    return
                # 中断请求：根据 intent 决定是否取消流式任务
                sid = request.session_id or "default"
                intent = request.params.get("intent", "cancel") if isinstance(request.params, dict) else "cancel"
                cleanup_after_cancel = self._is_client_disconnect_cancel_request(request)

                # 只有 cancel/supplement 才取消流式任务
                # pause/resume 不取消，因为任务仍在运行（pause 在 checkpoint 阻塞，resume 解除阻塞）
                stream_tasks: list[asyncio.Task] = []
                if intent in ("cancel", "supplement"):
                    entries = self._session_stream_tasks.get(sid, {})
                    for stream_task, stream_stop_event in list(entries.items()):
                        if stream_task.done():
                            continue
                        logger.info(
                            "[AgentWebSocketServer] cancel: 终止 session 流式任务: session_id=%s intent=%s",
                            sid,
                            intent,
                        )
                        stream_stop_event.set()
                        stream_task.cancel()
                        stream_tasks.append(stream_task)

                cancel_response: AgentResponse | None = None
                try:
                    # 专门处理 cancel，复用已有 agent（不再 fallthrough 到 _handle_unary）
                    # allow_create=False：找不到已有 agent 时不 fallback 新建（见 _handle_cancel docstring）。
                    cancel_response = await self._handle_cancel(
                        ws,
                        request,
                        send_lock,
                        allow_create=False,
                        send_response=not cleanup_after_cancel,
                    )
                finally:
                    if stream_tasks:
                        results = await asyncio.gather(*stream_tasks, return_exceptions=True)
                        for result in results:
                            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                                logger.warning(
                                    "[AgentWebSocketServer] cancel: stream task cleanup failed: "
                                    "session_id=%s intent=%s error=%s",
                                    sid,
                                    intent,
                                    result,
                                )
                    if cleanup_after_cancel and intent in ("cancel", "supplement"):
                        cleanup_succeeded = (
                            await self._cleanup_client_disconnect_session_runtime(
                                request
                            )
                        )
                        if cancel_response is not None:
                            if not cleanup_succeeded:
                                cancel_response.ok = False
                                cancel_response.payload = {
                                    "event_type": "chat.interrupt_result",
                                    "success": False,
                                    "error": "session runtime cleanup failed",
                                }
                            wire = encode_agent_response_for_wire(
                                cancel_response,
                                response_id=request.request_id,
                            )
                            async with send_lock:
                                await send_wire_payload(ws, wire)
                return
            await self._ensure_auto_team_binding_for_chat(request)
            # chat.send 入口采集隐式反馈：用户在推荐后的文本回复关联到最近推荐。
            # best-effort，失败绝不影响主 chat 流（见方法实现）。
            if request.req_method == ReqMethod.CHAT_SEND:
                await self._try_record_implicit_feedback(request)
            if request.is_stream:
                await self._handle_stream(ws, request, send_lock)
            else:
                await self._handle_unary(ws, request, send_lock)
        except asyncio.CancelledError:
            # 流式任务被 interrupt 取消，正常退出无需报错
            logger.info(
                "[AgentWebSocketServer] 任务被取消: request_id=%s session_id=%s",
                request.request_id,
                request.session_id,
            )
        except WebSocketConnectionClosed as e:
            logger.info(
                "[AgentWebSocketServer] WebSocket 已关闭，放弃请求回包: %s",
                format_ws_diagnostics(
                    {
                        "request_id": request.request_id,
                        "channel_id": request.channel_id,
                        "session_id": request.session_id,
                        "is_stream": request.is_stream,
                    },
                    describe_ws_peer(ws),
                    describe_ws_exception(e),
                ),
            )
        except Exception as e:
            logger.exception(
                "[AgentWebSocketServer] 处理请求失败: request_id=%s: %s",
                request.request_id,
                e,
            )
            wire = AgentWebSocketServer._send_error_response(
                ws,
                request,
                send_lock,
                str(e),
                (
                    getattr(e, "code", None)
                    if isinstance(getattr(e, "code", None), str)
                    and getattr(e, "code", None).startswith("AGENT_GROUP_")
                    else None
                ),
            )
            async with send_lock:
                await send_wire_payload(ws, wire)
        except BaseException as e:
            # Last-resort net for BaseExceptionGroup escapes. A
            # BaseExceptionGroup is only an Exception subclass when *every*
            # sub-exception is, so one containing GeneratorExit/CancelledError
            # (from anyio task-group teardown — the MCP SDK's streamable/
            # SSE clients, but also openjiuwen's runner/manager task groups
            # on cooperative cancellation) sails through ``except Exception``
            # above. Per-call coercion lives in the MCP module
            # (call_timeout_patch / tools fetch); this top-level net catches
            # anything that slips past those, coerces it to an Exception,
            # and sends a failure response so the frontend doesn't hang on a
            # missing res. KeyboardInterrupt and bare CancelledError
            # (cooperative cancellation, e.g. ws disconnect) are re-raised
            # verbatim by reraise_as_exception so they propagate as signals.
            from jiuwenswarm.server.runtime.mcp.exc_group import (
                reraise_as_exception,
            )
            try:
                reraise_as_exception(e)
            except KeyboardInterrupt:
                logger.exception(
                    "[AgentWebSocketServer] 处理请求失败(KeyboardInterrupt): request_id=%s",
                    request.request_id
                )
                raise
            except Exception as coerced:
                logger.exception(
                    "[AgentWebSocketServer] 处理请求失败(BaseException 逃逸): request_id=%s: %s",
                    request.request_id,
                    coerced,
                )
                wire = AgentWebSocketServer._send_error_response(
                    ws, request, send_lock, str(coerced),
                )
                async with send_lock:
                    await send_wire_payload(ws, wire)
        finally:
            if pending_chat_request is not None:
                runtime, session_id, request_id = pending_chat_request
                runtime.end_chat_request(session_id, request_id)

    @staticmethod
    def _should_trigger_before_chat_request_hook(request: AgentRequest) -> bool:
        return request.req_method in (
            ReqMethod.CHAT_SEND,
            ReqMethod.CHAT_RESUME,
            ReqMethod.CHAT_ANSWER,
        )

    async def _try_record_implicit_feedback(self, request: AgentRequest) -> None:
        """Best-effort 采集隐式反馈：用户在收到推荐后的文本回复。

        主动推荐送达后，用户若直接用文本回复（"简洁点""不需要"…）而不是点卡片
        上的赞/踩按钮，这条回复就是隐式反馈。把它关联到该会话最近一条推荐，
        交由 ``record_implicit_feedback`` 做情感分类后入 buffer，供下次 tick 梯度更新。

        与 Runtime activity participant 同款 best-effort：任何异常只 log debug，
        绝不阻断主 chat 流。只在 ``chat.send`` 且来源不是 proactive 自己触发的
        推荐指令时才介入（``source=proactive_recommendation`` 是系统主动塞给主
        agent 的指令，不是用户说的话，见 proactive_adapter 触发处）。
        """
        try:
            params = request.params if isinstance(request.params, dict) else {}
            # proactive 自己触发主 agent 的指令带 source=proactive_recommendation，
            # 那不是用户输入，跳过
            if str(params.get("source") or "").strip() == "proactive_recommendation":
                return
            session_id = str(request.session_id or params.get("session_id") or "").strip()
            if not session_id:
                return
            query = _request_query_text(request)
            if not query:
                return

            from jiuwenswarm.agents.harness.common.recommendation.feedback_collector import (
                find_latest_recommendation,
                record_implicit_feedback,
            )

            # max_age_seconds=0 砍掉时间窗：不靠时间硬挡"无关反馈"——是否相关、是否
            # 产生梯度交给梯度更新器的模型判断（看 rec_content + user_reply 语义）。
            # 配合 record_feedback 的"同 rec_id 只采紧跟第一条、后续不覆盖"逻辑，
            # 每条推荐只关联它之后紧跟的第一条用户回复。
            latest = find_latest_recommendation(session_id, max_age_seconds=0)
            if latest is None:
                return
            rec_id = latest.get("id")
            if not rec_id:
                return

            # 已对该 rec_id 给过显式反馈（赞/踩）的话，record_feedback 的去重逻辑
            # 会自动丢弃隐式补充——无需在此预判。
            record_implicit_feedback(rec_id, query)
        except Exception as exc:
            logger.debug(
                "[AgentWebSocketServer] implicit feedback hook failed; preserving chat: "
                "session_id=%s error=%s",
                request.session_id,
                exc,
            )

    @staticmethod
    def _is_client_disconnect_cancel_request(request: AgentRequest) -> bool:
        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        return (
            str(metadata.get(E2A_INTERNAL_CANCEL_SOURCE_KEY) or "").strip()
            == E2A_CANCEL_SOURCE_CLIENT_DISCONNECT
        )

    async def _cleanup_client_disconnect_session_runtime(self, request: AgentRequest) -> bool:
        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(request.session_id or params.get("session_id") or "").strip()
        if not session_id:
            return False
        channel_id = request.channel_id or "default"
        try:
            cleaned = await self._execution_runtime().cleanup_session(
                channel_id=channel_id,
                session_id=session_id,
            )
            logger.info(
                "[AgentWebSocketServer] client disconnect session runtime cleanup: "
                "channel_id=%s session_id=%s cleaned=%s",
                channel_id,
                session_id,
                cleaned,
            )
            return True
        except Exception as exc:
            logger.warning(
                "[AgentWebSocketServer] client disconnect session runtime cleanup failed: "
                "channel_id=%s session_id=%s error=%s",
                channel_id,
                session_id,
                exc,
            )
            return False
        finally:
            # Persisted history remains on disk, but this connection-scoped
            # marker must not grow with every short-lived TUI process. Mode
            # locks are weakly cached and disappear automatically after their
            # last active/waiting user releases them.
            _plan_exited_sessions.discard(session_id)
            # 同理：内存标记不留给已断开的会话。真在 plan 里的会话靠 metadata
            # 那道判据继续被识别，不依赖这个集合。
            _plan_active_sessions.discard(session_id)

    async def _trigger_before_chat_request_hook(self, request: AgentRequest) -> None:
        if not self._should_trigger_before_chat_request_hook(request):
            return
        from jiuwenswarm.extensions.registry import ExtensionRegistry

        params = request.params if isinstance(request.params, dict) else {}
        if not isinstance(request.params, dict):
            request.params = params

        ctx = AgentServerChatHookContext(
            request_id=request.request_id,
            channel_id=request.channel_id,
            session_id=request.session_id,
            req_method=request.req_method.value if request.req_method is not None else None,
            params=params,
        )

        await ExtensionRegistry.get_instance().trigger(AgentServerHookEvents.BEFORE_CHAT_REQUEST, ctx)

    async def _handle_cancel(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
        *,
        allow_create: bool = False,
        send_response: bool = True,
    ) -> AgentResponse:
        """Cancel through the transport-independent Runtime operation."""
        params = request.params if isinstance(request.params, dict) else {}
        if params.get("target_team_id"):
            resp = await self._cancel_targeted_expert(request)
        else:
            resp = await self._execution_runtime().cancel_request(
                request,
                allow_create=allow_create,
            )

        if send_response:
            wire = encode_agent_response_for_wire(
                resp,
                response_id=request.request_id,
            )
            async with send_lock:
                await send_wire_payload(ws, wire)
        return resp

    @staticmethod
    def _resolve_code_language() -> str:
        """Determine the display language for code mode plan approval messages.

        Returns ``"cn"`` or ``"en"`` based on configuration.
        Defaults to ``"cn"`` if the config key is missing.
        """
        try:
            config = get_config()
            return config.get("language", "cn")
        except Exception:
            return "cn"

    @staticmethod
    def _should_sync_code_mode_state(request: AgentRequest) -> bool:
        return PlanModeController.should_sync(request)

    @staticmethod
    def _is_explicit_plan_entry_request(request: AgentRequest) -> bool:
        return PlanModeController.is_explicit_entry(request)

    @staticmethod
    def _session_mode_sync_lock(session_id: str) -> asyncio.Lock:
        return _SERVER_PLAN_CONTROLLER.lock_for(session_id)

    async def _push_plan_mode_exited(
        self,
        request: AgentRequest,
        *,
        exit_mode: str | None = None,
    ) -> None:
        """Notify the client that plan mode ended after user approval.

        ``mode`` 保持 TUI 已消费的语义：退出 plan 后应回到的普通模式。code 单
        agent 仍是 ``code.normal``；work 单 agent / 集群按各自 profile 动态计算。
        """
        session_id = request.session_id
        if not session_id:
            return
        if not exit_mode:
            # ``normal_mode`` 对非 plan 的 canonical 模式原样返回，所以这里不需要
            # 分情况：plan 请求得到它的退出目标，普通请求得到它自己。别写死
            # "code.normal"——最常走的这条路径（plan→normal 恢复后回调）恰好是
            # 普通请求，写死会把 code 的模式推给 work 会话。
            exit_mode = resolve_request_runtime_mode(request).normal_mode
        await self.send_push({
            "channel_id": request.channel_id or "default",
            "session_id": session_id,
            "payload": {
                "event_type": PLAN_MODE_EXITED_EVENT_TYPE,
                "mode": exit_mode,
            },
        })

    async def _check_post_process_plan_exit(
        self,
        request: AgentRequest,
        agent: Any,
    ) -> None:
        controller = self._execution_runtime().plan_controller
        for payload in await controller.check_post_process_exit(request, agent):
            await self._push_plan_mode_exited(
                request,
                exit_mode=str(payload.get("mode") or ""),
            )

    @staticmethod
    def _is_stateless_method_request(request: AgentRequest) -> bool:
        """Return True for prefix-matched read-only RPCs that skip adapter setup."""
        return (
            request.req_method is not None
            and request.req_method.value.startswith(
                ("skills.", "skilldev.", "plugins.", "symphony.",
                 "agent_groups.", "agent_templates.", "plugin_packages.")
            )
        )

    @staticmethod
    def _is_readonly_goal_get_request(request: AgentRequest) -> bool:
        """``command.goal`` + ``action=get``：只读查询，不得兜底新建 session metadata.

        与 skills.list 同类问题：走 ``_prepare_code_mode_chat_turn`` 会触发
        ``sync_session_request_metadata`` 在无 metadata 时写出
        ``metadata.json``。get 仍需要真实 agent（可能从 checkpointer 读已有
        Goal），故不能整段塞进 ``_is_stateless_method_request``。
        """
        if request.req_method != ReqMethod.COMMAND_GOAL:
            return False
        params = request.params if isinstance(request.params, dict) else {}
        action = str(params.get("action") or "get").strip().lower()
        return action == "get"

    async def _get_stateless_agent(self, channel_id: str) -> Any:
        """为无状态请求取 agent，**不触发任何 mode 的 adapter 重建**.

        优先用 AgentManager 已缓存的 agent 模式 agent（get_agent_nowait 命中即返回，
        不命中返回 None，绝不创建）；都没缓存时复用（或首次构造）本 server 上按
        channel 缓存的轻量 JiuWenSwarm()（**不调 create_instance**，_adapter 保持
        None）——其 process_message 内部对 skills/skilldev/plugins/symphony 的无状态
        短路会在 _ensure_adapter 之前 return，碰不到 adapter。真正的 adapter 重建
        留给 chat.send。

        相比 5084467df 原版用 get_agent(mode="agent") 作 fallback（会触发 agent 模式
        adapter 重建，治标不治本），此处彻底解耦。Fallback 必须按 channel 复用，
        否则每次 cache miss 新建 SkillManager，SkillNet install/install_status 会
        落到不同实例并误报「安装会话已过期」。
        """
        cached = self._agent_manager.get_agent_nowait(
            channel_id=channel_id, mode="agent"
        )
        if cached is not None:
            return cached
        agent = self._stateless_fallback_agents.get(channel_id)
        if agent is not None:
            return agent
        from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
        agent = JiuWenSwarm()  # 不调 create_instance，_adapter 保持 None
        self._stateless_fallback_agents[channel_id] = agent
        return agent

    async def _prepare_code_mode_chat_turn(
        self,
        request: AgentRequest,
        channel_id: str,
        *,
        sync_metadata: bool = True,
    ) -> tuple[str, str | None, Any]:
        """Compatibility wrapper around transport-independent Runtime setup."""
        return await prepare_chat_turn(
            self._agent_manager,
            request,
            channel_id,
            sync_metadata=sync_metadata,
            metadata_sync=_sync_chat_request_metadata,
        )

    @staticmethod
    def _session_may_hold_plan_state(request: AgentRequest, session_id: str) -> bool:
        return _SERVER_PLAN_CONTROLLER.may_hold_state(request, session_id)




    @staticmethod
    async def _open_plan_state_session(
        agent: Any,
        session_id: str | None,
    ) -> tuple[Any, Any, bool]:
        return await PlanModeController.open_state_session(agent, session_id)

    async def _ensure_code_mode_state(
        self,
        request: AgentRequest,
        mode: str,
        sub_mode: str,
        agent: Any,
    ) -> bool:
        controller = self._execution_runtime().plan_controller
        result = await controller.ensure_state(request, mode, sub_mode, agent)
        if not result.restored:
            for payload in result.events:
                await self._push_plan_mode_exited(
                    request,
                    exit_mode=str(payload.get("mode") or ""),
                )
        return result.restored

    async def _handle_unary(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        await self._handle_unary_impl(ws, request, send_lock)

    async def _handle_unary_impl(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """非流式处理：调用 process_message，返回一条 E2AResponse 线 JSON。"""
        if request.req_method == ReqMethod.INITIALIZE:
            await self._handle_initialize(ws, request, send_lock)
            return

        if request.req_method == ReqMethod.SESSION_CREATE:
            await self._handle_session_create(ws, request, send_lock)
            return

        if request.req_method == ReqMethod.SESSION_FORK:
            await self._handle_session_fork(ws, request, send_lock)
            return

        if request.req_method == ReqMethod.ACP_TOOL_RESPONSE:
            await self._handle_acp_tool_response(ws, request, send_lock)
            return

        runtime = self._execution_runtime()
        resume_state = await self._open_session_message_resume(request)
        resume_outcome = "unknown"
        resume_error = "resumed turn ended before its outcome was confirmed"

        async def _send_control_event(event: RuntimeEvent) -> None:
            await self._send_runtime_event(
                ws,
                event,
                send_lock,
                streaming=False,
                sequence=0,
            )

        outcome_tracker = _TurnOutcomeTracker()
        try:
            if request.req_method == ReqMethod.CHAT_ANSWER:
                events = await runtime.answer_interaction(
                    request,
                    trigger_hook=False,
                    on_control_event=_send_control_event,
                )
            else:
                events = await runtime.invoke(
                    request,
                    trigger_hook=False,
                    on_control_event=_send_control_event,
                )

            for event in events:
                outcome_tracker.observe(event)
                if event.event_type == "chat.ask_user_question":
                    payload = event.payload if isinstance(event.payload, dict) else {}
                    await self._persist_repeated_session_question(
                        request,
                        next_interrupt_request_id=str(
                            payload.get("request_id") or ""
                        ).strip(),
                        next_interrupt_source=str(payload.get("source") or "").strip(),
                        resume_state=resume_state,
                    )
                await self._send_runtime_event(
                    ws,
                    event,
                    send_lock,
                    streaming=False,
                    sequence=0,
                )
            if resume_state is not None and resume_state.waiting_user:
                outcome_tracker.waiting_user = True
            resume_outcome = outcome_tracker.outcome()
            resume_error = (
                outcome_tracker.error
                if resume_outcome == "failed"
                else outcome_tracker.unconfirmed_error()
                if resume_outcome == "unknown"
                else ""
            )
        finally:
            try:
                await self._finalize_session_message_resume(
                    request,
                    resume_state,
                    outcome=resume_outcome,
                    error=resume_error,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "[SessionMessaging] failed to finalize resumed unary turn: "
                    "request_id=%s",
                    request.request_id,
                )
        logger.info(
            "[AgentWebSocketServer] 非流式响应已发送: request_id=%s",
            request.request_id,
        )


    async def _handle_stream(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        await self._handle_stream_impl(ws, request, send_lock)

    async def _handle_heartbeat_job(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        """Serve the stable heartbeat.job.* API from the local Rail runtime."""
        from jiuwenswarm.agents.harness.code.rails.heartbeat.runtime import (
            HeartbeatRuntimeUnavailableError,
        )

        params = request.params if isinstance(request.params, dict) else {}
        action = str(params.get("action") or "").strip()
        data = params.get("data") if isinstance(params.get("data"), dict) else {}
        effective_user_id = str(request.user_id or "").strip()
        if not effective_user_id and request.session_id:
            effective_user_id = str(
                (get_session_metadata(request.session_id) or {}).get("user_id") or ""
            ).strip()
        try:
            result = await self._heartbeat_runtime.handle_operation(
                action,
                data,
                channel_id=request.channel_id or "web",
                session_id=request.session_id or "",
                user_id=effective_user_id,
                source=("tui_rpc" if request.channel_id == "tui" else "web_rpc"),
            )
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"result": result},
                metadata=request.metadata,
            )
        except PermissionError as exc:
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "FORBIDDEN"},
                metadata=request.metadata,
            )
        except KeyError as exc:
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc).strip("'"), "code": "NOT_FOUND"},
                metadata=request.metadata,
            )
        except ValueError as exc:
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "BAD_REQUEST"},
                metadata=request.metadata,
            )
        except HeartbeatRuntimeUnavailableError as exc:
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "SERVICE_UNAVAILABLE"},
                metadata=request.metadata,
            )
        except RuntimeError as exc:
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "CONFLICT"},
                metadata=request.metadata,
            )
        wire = encode_agent_response_for_wire(
            response,
            response_id=request.request_id,
        )
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def execute_internal_heartbeat(self, request: AgentRequest) -> None:
        """Execute an AgentServer-owned Heartbeat through the shared Runtime."""
        channel_id = request.channel_id or "web"
        params = request.params if isinstance(request.params, dict) else {}
        prompt = str(params.get("content") or params.get("query") or "")
        await self.send_push(
            {
                "request_id": request.request_id,
                "channel_id": channel_id,
                "session_id": request.session_id,
                "payload": {
                    "event_type": "chat.processing_status",
                    "session_id": request.session_id,
                    "is_processing": True,
                    "is_complete": False,
                    "content": prompt,
                },
                "is_complete": False,
                "metadata": request.metadata,
            }
        )

        processing_finished = False

        def _retain_agent(agent: Any) -> None:
            self._heartbeat_runtime.retain_agent(
                request.session_id or "default",
                agent,
            )

        runtime_stream = self._execution_runtime().stream(
            request,
            trigger_hook=False,
            background=True,
            on_agent_ready=_retain_agent,
        )
        try:
            async for event in runtime_stream:
                payload = (
                    dict(event.payload)
                    if isinstance(event.payload, dict)
                    else event.payload
                )
                event_type = (
                    str(payload.get("event_type") or "")
                    if isinstance(payload, dict)
                    else ""
                )
                # 模型/Agent 级失败会以 chat.error / execution.error / team.error
                # 等事件类型送达，且 ok 默认 True（RuntimeEvent.from_agent_message）。
                # 仅靠 event.ok 会漏判这类终端失败，导致心跳本轮被误记为
                # succeeded（界面显示「上次运行成功」）。与
                # execute_internal_session_message / cron scheduler 对齐，按
                # event_type 识别终端错误。
                if not event.ok or event_type in TERMINAL_ERROR_EVENT_TYPES:
                    payload = dict(payload or {})
                    payload["event_type"] = "chat.error"
                    payload["error"] = str(
                        payload.get("error")
                        or payload.get("message")
                        or "Runtime execution failed"
                    )
                is_processing_start = (
                    isinstance(payload, dict)
                    and payload.get("event_type") == "chat.processing_status"
                    and bool(payload.get("is_processing"))
                )
                if is_processing_start and not str(payload.get("content") or ""):
                    payload = {**payload, "content": prompt}
                finishes_processing = (
                    isinstance(payload, dict)
                    and payload.get("event_type") == "chat.processing_status"
                    and payload.get("is_processing") is False
                )
                pushed = await self.send_push(
                    {
                        "request_id": event.request_id,
                        "channel_id": event.channel_id or channel_id,
                        "session_id": event.session_id or request.session_id,
                        "payload": payload,
                        "agent_ref": event.agent_ref,
                        "is_complete": event.is_complete,
                        "metadata": (
                            event.metadata
                            if event.metadata is not None
                            else request.metadata
                        ),
                    }
                )
                if finishes_processing and pushed:
                    processing_finished = True
                if not event.ok or event_type in TERMINAL_ERROR_EVENT_TYPES:
                    error_value = (
                        payload.get("error")
                        if isinstance(payload, dict)
                        else None
                    )
                    raise RuntimeError(
                        str(error_value or "Runtime execution failed")
                    )
        finally:
            try:
                await runtime_stream.aclose()
            finally:
                if not processing_finished:
                    await self.send_push(
                        {
                            "request_id": request.request_id,
                            "channel_id": channel_id,
                            "session_id": request.session_id,
                            "payload": {
                                "event_type": "chat.processing_status",
                                "session_id": request.session_id,
                                "is_processing": False,
                                "is_complete": True,
                            },
                            "is_complete": False,
                            "metadata": request.metadata,
                        }
                    )

    async def _push_session_message_status(
        self, record: SessionMessageRecord
    ) -> None:
        """Push a mailbox status hint using the target Session's saved route."""

        message = build_server_push_message(
            session_id=record.target_session_id,
            request_id=record.execution_request_id or record.message_id,
            payload={
                "event_type": "session.message.updated",
                "message": {
                    "message_id": record.message_id,
                    "source_session_id": record.source_session_id,
                    "source_title": record.source_title_snapshot,
                    "target_session_id": record.target_session_id,
                    "chain_id": record.chain_id,
                    "hop_count": record.hop_count,
                    "status": record.status,
                    "input_mode": record.input_mode,
                    "created_at": record.created_at,
                    "started_at": record.started_at,
                    "finished_at": record.finished_at,
                    "error_code": record.last_error_code,
                    "error": record.last_error,
                },
            },
            fallback_channel_id="web",
        )
        if message["channel_id"] == "web":
            message["payload"]["message"]["content"] = record.content
        message.setdefault("metadata", {})[SESSION_MESSAGE_OWNER_SCOPE_METADATA_KEY] = record.owner_scope_id
        message["metadata"][SESSION_MESSAGE_ANONYMOUS_OWNER_METADATA_KEY] = False
        if record.owner_scope_id == "local":
            target_metadata = await asyncio.to_thread(
                get_session_metadata,
                record.target_session_id,
                cache_bust=True,
                enable_writeback=False,
            )
            if not target_metadata or str(target_metadata.get("user_id") or "").strip() not in {"", "local"}:
                return
            message["metadata"][SESSION_MESSAGE_ANONYMOUS_OWNER_METADATA_KEY] = not bool(
                str(target_metadata.get("user_id") or "").strip()
            )
        await self.send_push(message)

    async def execute_internal_session_message(
        self, record: SessionMessageRecord
    ) -> SessionMessageExecutionResult:
        """Execute one claimed mailbox record in its target product Session."""

        metadata = get_session_metadata(
            record.target_session_id,
            cache_bust=True,
            enable_writeback=False,
        )
        mode = deprecate_mode(metadata.get("mode")) if metadata else ""
        channel_id = str(metadata.get("channel_id") or "").strip().lower()
        # metadata 缺失 / 非单 Agent 模式 / 渠道不支持 / cron 会话，均视为目标不可用
        target_unsupported = (
            not metadata
            or not is_single_agent_mode(mode)
            or channel_id not in {"web", "tui"}
            or bool(str(metadata.get("cron_id") or "").strip())
        )
        if target_unsupported:
            return SessionMessageExecutionResult(
                status="failed",
                error_code="UNSUPPORTED_TARGET",
                error="target Session no longer supports Agent messaging",
            )

        stored_user_id = str(metadata.get("user_id") or "").strip()
        owner_matches = (
            not stored_user_id
            if record.owner_scope_id == "local"
            else stored_user_id == record.owner_scope_id
        )
        if not owner_matches:
            return SessionMessageExecutionResult(
                status="failed",
                error_code="NOT_FOUND_OR_FORBIDDEN",
                error="target Session ownership changed before execution",
            )

        cross_session = {
            "message_id": record.message_id,
            "source_session_id": record.source_session_id,
            "source_request_id": record.source_request_id,
            "source_tool_call_id": record.source_tool_call_id,
            "source_title": record.source_title_snapshot,
            "chain_id": record.chain_id,
            "parent_message_id": record.parent_message_id,
            "hop_count": record.hop_count,
            "language": str(get_config().get("preferred_language") or "zh"),
        }
        params: dict[str, Any] = {
            "query": record.content,
            "mode": mode,
            SESSION_MESSAGE_INTERNAL_KEY: cross_session,
        }
        if record.input_mode:
            params["input_mode"] = record.input_mode
        for key in ("project_id", "project_dir", "work_mode"):
            value = metadata.get(key)
            if value is not None and str(value).strip():
                params[key] = value
        model_name = metadata.get("model")
        if isinstance(model_name, str) and model_name.strip():
            params["model_name"] = model_name.strip()

        request = AgentRequest(
            request_id=record.execution_request_id,
            channel_id=channel_id,
            session_id=record.target_session_id,
            req_method=ReqMethod.CHAT_SEND,
            params=params,
            is_stream=True,
            timestamp=_dt.datetime.now(_dt.timezone.utc).timestamp(),
            metadata={SESSION_MESSAGE_INTERNAL_KEY: cross_session},
            user_id=stored_user_id,
        )

        public_cross_session = {
            **cross_session,
            "content": record.content,
        }

        def _with_cross_session_marker(
            payload: dict[str, Any],
            *,
            request_id: str,
        ) -> dict[str, Any]:
            if payload.get("event_type") == "chat.input_received":
                # This input has its own author and message association, even
                # when the receiving task was started by another mailbox item.
                return payload
            return {
                **payload,
                # ask_user 等事件的 request_id 是交互关联 ID，不能覆盖；
                # 后台轮自身的稳定身份单独使用 turn_request_id。
                "request_id": payload.get("request_id") or request_id,
                "turn_request_id": request_id,
                "message_origin": SESSION_MESSAGE_ORIGIN,
                "session_message_id": record.message_id,
                "cross_session": public_cross_session,
            }

        async def start_processing() -> None:
            await self.send_push(
                build_server_push_message(
                    session_id=record.target_session_id,
                    request_id=request.request_id,
                    payload=_with_cross_session_marker(
                        {
                            "event_type": "chat.processing_status",
                            "session_id": record.target_session_id,
                            "is_processing": True,
                            "is_complete": False,
                            "content": record.content,
                        },
                        request_id=request.request_id,
                    ),
                    fallback_channel_id=channel_id,
                )
            )

        supplemental_delivery = record.input_mode == "steer"
        delivered = False
        delivery_error_code = ""
        if not supplemental_delivery:
            await start_processing()

        outcome_tracker = _TurnOutcomeTracker()
        processing_finished = False
        runtime = self._execution_runtime()
        if supplemental_delivery:
            # Resume persisted idle Sessions through the public lifecycle API.
            await runtime.start()
            await runtime.create_or_resume_session(
                channel_id=channel_id, session_id=record.target_session_id,
            )
        runtime_stream = runtime.stream(
            request,
            trigger_hook=False,
            background=not supplemental_delivery,
        )
        stream_completed = False
        try:
            async for event in runtime_stream:
                payload = (
                    dict(event.payload)
                    if isinstance(event.payload, dict)
                    else event.payload
                )
                event_type = (
                    str(payload.get("event_type") or "")
                    if isinstance(payload, dict)
                    else ""
                )
                if supplemental_delivery and event.ok and event_type == "runtime.accepted":
                    if payload.get("input_delivery") == "chat":
                        supplemental_delivery = False
                        await start_processing()
                    elif payload.get("input_boundary") == "stream":
                        delivered = True
                        continue
                elif record.input_mode == "steer" and event.ok and event_type == "runtime.accepted":
                    service = getattr(self, "_session_message_service", None)
                    if service is not None:
                        service.on_steering_fallback_started(record)
                outcome_tracker.observe(event)
                if not event.ok or event_type in TERMINAL_ERROR_EVENT_TYPES:
                    error_payload = dict(payload or {})
                    error_payload["event_type"] = "chat.error"
                    error_payload["error"] = str(
                        error_payload.get("error")
                        or error_payload.get("message")
                        or "Runtime execution failed"
                    )
                    payload = error_payload
                    delivery_error_code = str(error_payload.get("code") or "")
                    outcome_tracker.fail(str(error_payload.get("error") or ""))
                if supplemental_delivery:
                    # Receipt-only delivery does not own target task output or
                    # its processing/history completion. The original stream
                    # publishes the ordered, source-tagged input boundary.
                    continue
                if (
                    isinstance(payload, dict)
                    and payload.get("event_type") == "chat.ask_user_question"
                ):
                    interrupt_request_id = str(payload.get("request_id") or "").strip()
                    interrupt_source = str(payload.get("source") or "").strip()
                    service = getattr(self, "_session_message_service", None)
                    if not interrupt_request_id or not interrupt_source:
                        outcome_tracker.fail(
                            "Runtime emitted an uncorrelated user question"
                        )
                    elif service is None:
                        outcome_tracker.fail("Session message service is unavailable")
                    elif not await service.mark_waiting(
                        record.message_id,
                        interrupt_request_id=interrupt_request_id,
                        interrupt_source=interrupt_source,
                    ):
                        outcome_tracker.fail(
                            "Failed to persist user-question correlation"
                        )
                    if outcome_tracker.saw_error:
                        payload = {
                            "event_type": "chat.error",
                            "error": outcome_tracker.error,
                        }
                if isinstance(payload, dict):
                    payload = _with_cross_session_marker(
                        payload,
                        request_id=event.request_id or request.request_id,
                    )
                    processing_finished = (
                        processing_finished
                        or (
                            payload.get("event_type") == "chat.processing_status"
                            and payload.get("is_processing") is False
                        )
                    )
                push = build_server_push_message(
                    session_id=record.target_session_id,
                    request_id=event.request_id or request.request_id,
                    payload=payload if isinstance(payload, dict) else {},
                    fallback_channel_id=channel_id,
                )
                push["is_complete"] = event.is_complete
                await self.send_push(push)
            stream_completed = True
        finally:
            try:
                await runtime_stream.aclose()
            finally:
                try:
                    if not supplemental_delivery and not processing_finished:
                        await self.send_push(
                            build_server_push_message(
                                session_id=record.target_session_id,
                                request_id=request.request_id,
                                payload=_with_cross_session_marker(
                                    {
                                        "event_type": "chat.processing_status",
                                        "session_id": record.target_session_id,
                                        "is_processing": False,
                                        "is_complete": True,
                                    },
                                    request_id=request.request_id,
                                ),
                                fallback_channel_id=channel_id,
                            )
                        )
                finally:
                    if not stream_completed or outcome_tracker.outcome() in {
                        "failed", "unknown"
                    }:
                        release = getattr(
                            runtime, "release_session_message_interactions", None
                        )
                        if callable(release):
                            await release(
                                record.target_session_id, request_id=request.request_id
                            )

        if supplemental_delivery:
            if outcome_tracker.outcome() == "failed":
                return SessionMessageExecutionResult(
                    status="unknown" if delivery_error_code == "SESSION_INPUT_DELIVERY_UNKNOWN" else "failed",
                    error_code=delivery_error_code or "DELIVERY_FAILED",
                    error=outcome_tracker.error or "Runtime rejected supplemental input",
                )
            if delivered:
                return SessionMessageExecutionResult(status="delivered")
            return SessionMessageExecutionResult(
                status="unknown", error_code="DELIVERY_NOT_CONFIRMED",
                error="Runtime did not confirm supplemental delivery",
            )

        outcome = outcome_tracker.outcome()
        terminal_status = {
            "succeeded": "success",
            "failed": "failed",
            "waiting_user": "waiting_user",
            "unknown": "unknown",
        }[outcome]
        try:
            receipt = enqueue_history_request_completion(
                record.target_session_id,
                request.request_id,
                terminal_status=terminal_status,
            )
            if receipt is not None:
                await wait_for_history_receipt(receipt, timeout=5.0)
        except BaseException:
            if outcome == "waiting_user":
                release = getattr(runtime, "release_session_message_interactions", None)
                if callable(release):
                    await release(
                        record.target_session_id, request_id=request.request_id
                    )
            raise

        if outcome == "failed":
            return SessionMessageExecutionResult(
                status="failed",
                error_code="EXECUTION_FAILED",
                error=outcome_tracker.error or "Runtime execution failed",
            )
        if outcome == "unknown":
            return SessionMessageExecutionResult(
                status="unknown",
                error_code="EXECUTION_NOT_COMPLETED",
                error=outcome_tracker.unconfirmed_error(),
            )
        if outcome == "waiting_user":
            return SessionMessageExecutionResult(status="waiting_user")
        return SessionMessageExecutionResult(status="succeeded")

    async def _handle_stream_impl(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """流式处理：调用 process_message_stream，逐条发送 E2AResponse 线 JSON。"""
        session_id = self._organization_stream_key(request)
        resume_state = await self._open_session_message_resume(request)
        resume_outcome = "unknown"
        resume_error = "resumed turn ended before its outcome was confirmed"
        current_task = asyncio.current_task()
        stream_stop_event = asyncio.Event()
        uses_session_runtime = AgentRuntime.uses_session_runtime(request)
        if current_task is not None and not uses_session_runtime:
            self._session_stream_tasks.setdefault(session_id, {})[current_task] = stream_stop_event

        chunk_count = 0
        outcome_tracker = _TurnOutcomeTracker()
        keepalive = _StreamKeepalive(
            ws,
            request,
            send_lock,
        )
        runtime_stream: Any | None = None

        async def _send_control_event(event: RuntimeEvent) -> None:
            await self._send_runtime_event(
                ws,
                event,
                send_lock,
                streaming=True,
                sequence=chunk_count,
            )
        try:
            if self._is_targeted_expert_chat(request):
                runtime_stream = self._targeted_expert_runtime_stream(request)
            else:
                runtime_stream = self._execution_runtime().stream(
                    request,
                    trigger_hook=False,
                    on_control_event=_send_control_event,
                )
            keepalive.start()
            async for event in runtime_stream:
                # Runtime control events normally use the callback above. Keep
                # compatibility with custom Runtime implementations without
                # consuming a wire sequence number or resetting the keepalive timer.
                if event.event_type == PLAN_MODE_EXITED_EVENT_TYPE:
                    await _send_control_event(event)
                    continue
                outcome_tracker.observe(event)
                if event.event_type == "chat.ask_user_question":
                    payload = event.payload if isinstance(event.payload, dict) else {}
                    await self._persist_repeated_session_question(
                        request,
                        next_interrupt_request_id=str(
                            payload.get("request_id") or ""
                        ).strip(),
                        next_interrupt_source=str(payload.get("source") or "").strip(),
                        resume_state=resume_state,
                    )
                chunk_count += 1
                # 通知 keepalive 有真实 chunk 发送，重置空闲计时。
                keepalive.notify_activity(terminal=event.is_complete)
                try:
                    sent_original = await self._send_runtime_event(
                        ws,
                        event,
                        send_lock,
                        streaming=True,
                        sequence=chunk_count - 1,
                    )
                    if not sent_original:
                        logger.warning(
                            "[AgentWebSocketServer] 流式响应因单个 chunk 超限而停止: "
                            "request_id=%s seq=%s",
                            request.request_id,
                            chunk_count - 1,
                        )
                        return
                except WebSocketConnectionClosed:
                    logger.info(
                        "[AgentWebSocketServer] 流式响应停止，WebSocket 已关闭: request_id=%s",
                        request.request_id,
                    )
                    return
            if resume_state is not None and resume_state.waiting_user:
                outcome_tracker.waiting_user = True
            resume_outcome = outcome_tracker.outcome()
            resume_error = (
                outcome_tracker.error
                if resume_outcome == "failed"
                else outcome_tracker.unconfirmed_error()
                if resume_outcome == "unknown"
                else ""
            )
        finally:
            try:
                # 尽早阻止新的 keepalive；Runtime 清理仍先完成，以尽快释放其资源。
                keepalive.signal_stop()
                try:
                    if runtime_stream is not None:
                        close_stream = getattr(runtime_stream, "aclose", None)
                        if callable(close_stream):
                            await close_stream()
                finally:
                    try:
                        # 显式停止并唤醒 keepalive；Task.cancel 只作为有界的兜底。
                        await keepalive.stop()
                    finally:
                        # 清除自身的宿主生命周期记录；同 session 的其它请求不受影响。
                        entries = self._session_stream_tasks.get(session_id)
                        if entries is not None and current_task is not None:
                            entries.pop(current_task, None)
                            if not entries:
                                self._session_stream_tasks.pop(session_id, None)
            finally:
                try:
                    await self._finalize_session_message_resume(
                        request,
                        resume_state,
                        outcome=resume_outcome,
                        error=resume_error,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "[SessionMessaging] failed to finalize resumed stream turn: "
                        "request_id=%s",
                        request.request_id,
                    )
        logger.info(
            "[AgentWebSocketServer] 流式响应已发送: request_id=%s 共 %s 个 chunk",
            request.request_id,
            chunk_count,
        )

    async def _persist_repeated_session_question(
        self,
        request: AgentRequest,
        *,
        next_interrupt_request_id: str,
        next_interrupt_source: str,
        resume_state: _SessionMessageResumeState | None = None,
    ) -> bool | None:
        """Persist a resumed mailbox turn's next question before it is pushed."""

        if resume_state is None:
            resume_state = await self._open_session_message_resume(request)
        if resume_state is None:
            return None
        service = getattr(self, "_session_message_service", None)
        if service is None:
            return None
        next_request_id = str(next_interrupt_request_id or "").strip()
        next_source = str(next_interrupt_source or "").strip()
        if not next_request_id or not next_source:
            raise RuntimeError("Runtime emitted an uncorrelated follow-up question")
        persisted = await service.complete_waiting_after_resume(
            resume_state.target_session_id,
            interrupt_request_id=resume_state.interrupt_request_id,
            interrupt_source=resume_state.interrupt_source,
            waiting_user=True,
            failed=False,
            next_interrupt_request_id=next_request_id,
            next_interrupt_source=next_source,
        )
        if not persisted:
            raise RuntimeError("Failed to persist follow-up question correlation")
        resume_state.interrupt_request_id = next_request_id
        resume_state.interrupt_source = next_source
        resume_state.waiting_user = True
        return True

    async def _open_session_message_resume(
        self,
        request: AgentRequest,
    ) -> _SessionMessageResumeState | None:
        """Capture the mailbox identity before executing an interrupt answer."""

        from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
            is_interrupt_resume_payload,
        )

        if not is_interrupt_resume_payload(request.params):
            return None
        service = getattr(self, "_session_message_service", None)
        if service is None:
            return None
        params = request.params if isinstance(request.params, dict) else {}
        interrupt_request_id = str(params.get("request_id") or "").strip()
        interrupt_source = str(params.get("source") or "").strip()
        target_session_id = request.session_id or "default"
        record = await service.find_waiting_for_resume(
            target_session_id,
            interrupt_request_id,
            interrupt_source,
        )
        if record is None:
            return None
        request.trusted_session_message_route = {
            "message_id": record.message_id,
            "chain_id": record.chain_id,
            "parent_message_id": record.parent_message_id,
            "hop_count": record.hop_count,
        }
        return _SessionMessageResumeState(
            message_id=record.message_id,
            target_session_id=target_session_id,
            interrupt_request_id=interrupt_request_id,
            interrupt_source=interrupt_source,
        )

    async def _finalize_session_message_resume(
        self,
        request: AgentRequest,
        resume_state: _SessionMessageResumeState | None,
        *,
        outcome: str,
        error: str,
    ) -> None:
        """Persist history and resolve a resumed mailbox item exactly once."""

        if resume_state is None:
            return
        if outcome not in {"succeeded", "failed", "waiting_user", "unknown"}:
            raise ValueError(f"unsupported resumed-turn outcome: {outcome}")

        terminal_status = {
            "succeeded": "success",
            "failed": "failed",
            "waiting_user": "waiting_user",
            "unknown": "unknown",
        }[outcome]
        error_code = (
            "EXECUTION_FAILED"
            if outcome == "failed"
            else "RESUME_OUTCOME_UNKNOWN"
            if outcome == "unknown"
            else ""
        )
        try:
            receipt = enqueue_history_request_completion(
                resume_state.target_session_id,
                request.request_id,
                terminal_status=terminal_status,
            )
            if receipt is not None:
                await wait_for_history_receipt(receipt, timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            outcome = "unknown"
            error_code = "HISTORY_PERSISTENCE_UNCONFIRMED"
            error = f"history completion was not confirmed: {exc}"

        if outcome == "waiting_user":
            return
        service = getattr(self, "_session_message_service", None)
        if service is None:
            return
        await service.finalize_waiting_message(
            message_id=resume_state.message_id,
            target_session_id=resume_state.target_session_id,
            status=outcome,
            error_code=error_code,
            error=error,
        )
        if outcome in {"failed", "unknown"}:
            release = getattr(
                self._execution_runtime(), "release_session_message_interactions", None
            )
            if callable(release):
                await release(
                    resume_state.target_session_id,
                    request_id=f"session-message-{resume_state.message_id}",
                )

    async def _complete_waiting_session_message_after_external_turn(
        self,
        request: AgentRequest,
        *,
        waiting_user: bool,
        waiting_correlation_persisted: bool,
        failed: bool,
        error: str,
        next_interrupt_request_id: str,
        next_interrupt_source: str,
    ) -> None:
        """Compatibility wrapper for resolving a resumed mailbox turn."""

        from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
            is_interrupt_resume_payload,
        )

        if not is_interrupt_resume_payload(request.params):
            return
        service = getattr(self, "_session_message_service", None)
        if service is None:
            return
        if waiting_user and not waiting_correlation_persisted:
            return
        params = request.params if isinstance(request.params, dict) else {}
        interrupt_request_id = (
            next_interrupt_request_id
            if waiting_correlation_persisted
            else str(params.get("request_id") or "").strip()
        )
        interrupt_source = (
            next_interrupt_source
            if waiting_correlation_persisted
            else str(params.get("source") or "").strip()
        )
        target_session_id = request.session_id or "default"
        record = await service.find_waiting_for_resume(
            target_session_id,
            interrupt_request_id,
            interrupt_source,
        )
        if record is None:
            return
        state = _SessionMessageResumeState(
            message_id=record.message_id,
            target_session_id=target_session_id,
            interrupt_request_id=interrupt_request_id,
            interrupt_source=interrupt_source,
            waiting_user=waiting_user,
        )
        await self._finalize_session_message_resume(
            request,
            state,
            outcome=(
                "failed" if failed else "waiting_user" if waiting_user else "succeeded"
            ),
            error=error,
        )

    def _execution_runtime(self) -> AgentRuntime:
        runtime = getattr(self, "_runtime", None)
        manager = getattr(self, "_agent_manager", None)
        if runtime is None or runtime.agent_manager is not manager:
            runtime = self._build_runtime(
                agent_manager=manager,
                plan_controller=_SERVER_PLAN_CONTROLLER,
                reuse_dependencies=True,
            )
            self._runtime = runtime
            runtime.set_session_message_service(
                getattr(self, "_session_message_service", None)
            )
        return runtime

    async def _send_runtime_event(
        self,
        ws: Any,
        event: RuntimeEvent,
        send_lock: asyncio.Lock,
        *,
        streaming: bool,
        sequence: int,
    ) -> bool:
        if event.event_type == PLAN_MODE_EXITED_EVENT_TYPE:
            await self.send_push(
                {
                    "channel_id": event.channel_id,
                    "session_id": event.session_id,
                    "payload": event.payload,
                }
            )
            return True
        if not event.ok and event.event_type == "runtime.error":
            # Before Runtime extraction, an Agent execution exception escaped
            # to the AgentServer request boundary and was encoded as a unary
            # AgentResponse error even for a streaming request. Keep that
            # external wire contract; ``runtime.error`` is an internal Runtime
            # event used by in-process clients and must not leak onto E2A.
            error_value = (
                event.payload.get("error")
                if isinstance(event.payload, dict)
                else None
            )
            message = AgentResponse(
                request_id=event.request_id,
                channel_id=event.channel_id,
                ok=False,
                payload={
                    "error": str(
                        error_value
                        if error_value is not None
                        else "Runtime execution failed"
                    )
                },
                agent_ref=event.agent_ref,
                metadata=event.metadata,
            )
            wire = encode_agent_response_for_wire(
                message,
                response_id=event.request_id,
            )
        elif streaming:
            payload = dict(event.payload) if event.payload is not None else None
            if not event.ok:
                # AgentResponseChunk has no `ok` field. Reuse the established
                # chat.error contract so the E2A codec preserves failed status.
                payload = dict(payload or {})
                payload["event_type"] = "chat.error"
                payload.setdefault("error", "Runtime execution failed")
            if isinstance(payload, dict) and payload.get("event_type") == "chat.error":
                # 集群的失败是 ok 事件里的 chat.error（team.error 转过来的），也要分类
                self._annotate_model_error(payload, event.session_id)
            message = AgentResponseChunk(
                request_id=event.request_id,
                channel_id=event.channel_id,
                payload=payload,
                is_complete=event.is_complete,
                agent_ref=event.agent_ref,
                metadata=event.metadata,
            )
            wire = encode_agent_chunk_for_wire(
                message,
                response_id=event.request_id,
                sequence=sequence,
            )
        else:
            message = AgentResponse(
                request_id=event.request_id,
                channel_id=event.channel_id,
                ok=event.ok,
                payload=event.payload,
                agent_ref=event.agent_ref,
                metadata=event.metadata,
            )
            wire = encode_agent_response_for_wire(
                message,
                response_id=event.request_id,
            )
        async with send_lock:
            return await send_wire_payload(ws, wire)








    async def _find_team_session_ids(self, team_name: str) -> list[str]:
        sessions_dir = get_agent_sessions_dir()
        if not sessions_dir.exists():
            return []

        matched_session_ids: list[str] = []
        for session_dir in sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue

            session_id = session_dir.name
            metadata = get_session_metadata(session_id)
            if not self._is_team_metadata_mode(metadata):
                continue

            metadata_team_name = str(metadata.get("team_name") or "").strip()
            if metadata_team_name == team_name:
                matched_session_ids.append(session_id)

        return sorted(set(matched_session_ids))

    async def _ensure_persistent_checkpointer_response(
        self,
        request: AgentRequest,
    ) -> AgentResponse | None:
        """Return an error response when persistent checkpoint storage is unavailable."""
        try:
            from jiuwenswarm.server.runtime.agent_adapter.interface_deep import ensure_persistent_checkpointer

            await ensure_persistent_checkpointer()
            await self._try_start_heartbeat_runtime()
            return None
        except Exception as exc:
            logger.exception(
                "[AgentWebSocketServer] persistent checkpointer unavailable: request_id=%s error=%s",
                request.request_id,
                exc,
            )
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "error": "persistent checkpointer is unavailable",
                    "code": "CHECKPOINT_UNAVAILABLE",
                },
                metadata=request.metadata,
            )

    async def _try_start_heartbeat_runtime(self) -> bool:
        """Best-effort idempotent recovery after checkpointer readiness."""
        if self._heartbeat_runtime.is_available:
            return True
        try:
            await self._heartbeat_runtime.start()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AgentWebSocketServer] Heartbeat runtime is not ready yet: %s",
                exc,
            )
            return False

    @staticmethod
    def _team_binding_payload(binding: Any) -> dict[str, Any]:
        if hasattr(binding, "to_dict"):
            return binding.to_dict()
        if isinstance(binding, dict):
            return dict(binding)
        return {}

    @staticmethod
    async def _create_team_binding_from_template(
        *,
        team_name: str,
        template_id: str,
        config_base: dict[str, Any],
    ) -> Any:
        from jiuwenswarm.agents.harness.team import (
            get_team_template_snapshot,
            list_team_template_summaries,
        )
        from jiuwenswarm.server.runtime.team_binding_store import (
            TeamBindingStoreError,
            get_team_binding_store,
            validate_team_name,
        )
        from jiuwenswarm.server.runtime.team_entity_store import get_team_entity_store

        normalized_name = validate_team_name(team_name)
        from jiuwenswarm.runtime.session_delete import TEAM_DELETION_GATE
        template_ids = {
            str(item.get("template_id") or "")
            for item in list_team_template_summaries(config_base)
        }
        if template_id not in template_ids:
            raise TeamBindingStoreError("template_id not found", code="NOT_FOUND")

        async with TEAM_DELETION_GATE.mutation_lock(normalized_name):
            TEAM_DELETION_GATE.assert_not_deleting_locked(normalized_name)
            entity_store = get_team_entity_store()
            if entity_store.exists(normalized_name):
                raise TeamBindingStoreError("team_name already exists", code="CONFLICT")
            template_snapshot = get_team_template_snapshot(config_base, template_id=template_id)
            binding_store = get_team_binding_store()
            binding = binding_store.create(team_name=normalized_name, template_id=template_id)
            try:
                entity_store.write(
                    team_name=binding.team_name,
                    template_id=binding.template_id,
                    template_snapshot=template_snapshot,
                    created_at=binding.created_at,
                )
            except Exception:
                binding_store.delete(binding.team_name)
                raise
        return binding

    @classmethod
    async def _create_generated_team_binding(
        cls,
        *,
        description: str,
        config_base: dict[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        """Generate a unique team name and persist its binding and entity."""
        from jiuwenswarm.agents.harness.team import (
            generate_team_name,
            list_team_template_summaries,
        )
        from jiuwenswarm.server.runtime.team_binding_store import (
            TeamBindingStoreError,
        )

        normalized_description = str(description or "").strip()
        if not normalized_description:
            raise TeamBindingStoreError("description is required", code="BAD_REQUEST")

        templates = list_team_template_summaries(config_base)
        if not templates:
            raise TeamBindingStoreError("no team template configured", code="NOT_FOUND")

        default_template = templates[0]
        template_id = str(default_template.get("template_id") or "").strip()
        generated_name = await generate_team_name(
            normalized_description,
            config_base=config_base,
            template_id=template_id,
        )

        for candidate_index in range(100):
            suffix = "" if candidate_index == 0 else f"_{candidate_index + 1}"
            candidate = f"{generated_name[:64 - len(suffix)]}{suffix}"
            try:
                binding = await cls._create_team_binding_from_template(
                    team_name=candidate,
                    template_id=template_id,
                    config_base=config_base,
                )
                return binding, default_template
            except TeamBindingStoreError as exc:
                if exc.code != "CONFLICT":
                    raise

        raise TeamBindingStoreError(
            "unable to allocate a unique team_name",
            code="CONFLICT",
        )

    async def _ensure_auto_team_binding_for_chat(self, request: AgentRequest) -> Any | None:
        """Forward an existing team binding without creating one from the query."""
        if request.req_method != ReqMethod.CHAT_SEND:
            return None

        params = request.params if isinstance(request.params, dict) else {}
        if not isinstance(request.params, dict):
            request.params = params
        session_id = str(request.session_id or params.get("session_id") or "").strip()
        if not session_id:
            return None

        metadata = get_session_metadata(session_id, cache_bust=True)
        raw_mode = params.get("mode")
        effective_mode = (
            raw_mode
            if isinstance(raw_mode, str) and raw_mode.strip()
            else metadata.get("mode")
        )
        _, _, canonical_mode = resolve_agent_request_mode(effective_mode)
        if not self._is_team_metadata_mode({"mode": canonical_mode}):
            return None

        existing_team_name = str(metadata.get("team_name") or "").strip()
        if existing_team_name:
            params.setdefault("team_name", existing_team_name)
            template_id = str(metadata.get("team_template_id") or "").strip()
            if template_id:
                params.setdefault("team_template_id", template_id)
            return existing_team_name
        return None

    @staticmethod
    def _is_team_metadata_mode(metadata: dict[str, Any]) -> bool:
        return is_team_mode(metadata.get("mode"))

    @staticmethod
    def _active_team_session_map() -> dict[str, str]:
        from jiuwenswarm.agents.harness.team import get_all_team_managers

        active: dict[str, str] = {}
        for manager in get_all_team_managers():
            snapshot_fn = getattr(manager, "get_runtime_team_snapshot", None)
            if not callable(snapshot_fn):
                continue
            for session_id, info in snapshot_fn().items():
                team_name = str(info.get("team_name") or "").strip()
                state = str(info.get("state") or "").strip()
                if team_name and state in {"active", "pending"}:
                    active.setdefault(team_name, str(session_id))
        return active

    @staticmethod
    def _legacy_team_bindings_from_sessions(known_team_names: set[str]) -> list[dict[str, Any]]:
        sessions_dir = get_agent_sessions_dir()
        if not sessions_dir.exists():
            return []

        legacy: dict[str, dict[str, Any]] = {}
        for session_dir in sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            metadata = get_session_metadata(session_dir.name, cache_bust=True)
            if not metadata or not AgentWebSocketServer._is_team_metadata_mode(metadata):
                continue
            team_name = str(metadata.get("team_name") or "").strip()
            if not team_name or team_name in known_team_names:
                continue
            item = legacy.setdefault(
                team_name,
                {
                    "team_name": team_name,
                    "template_id": str(metadata.get("team_template_id") or team_name).strip() or team_name,
                    "created_at": float(metadata.get("created_at") or session_dir.stat().st_ctime),
                    "updated_at": float(metadata.get("last_message_at") or session_dir.stat().st_mtime),
                    "session_ids": [],
                    "last_session_id": "",
                    "legacy": True,
                },
            )
            if session_dir.name not in item["session_ids"]:
                item["session_ids"].append(session_dir.name)
            if float(metadata.get("last_message_at") or 0) >= float(item.get("updated_at") or 0):
                item["updated_at"] = float(metadata.get("last_message_at") or session_dir.stat().st_mtime)
                item["last_session_id"] = session_dir.name
        return list(legacy.values())

    async def _handle_team_templates_list(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from jiuwenswarm.agents.harness.team import list_team_template_summaries

        templates = list_team_template_summaries(get_config())
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"templates": templates},
            metadata=request.metadata,
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_team_bindings_list(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from jiuwenswarm.agents.harness.team import list_team_template_summaries
        from jiuwenswarm.server.runtime.team_entity_store import ensure_team_entity_for_binding, get_team_entity_store
        from jiuwenswarm.server.runtime.team_binding_store import get_team_binding_store

        active_by_team = self._active_team_session_map()
        config_base = get_config()
        templates = {
            str(item.get("template_id") or ""): item
            for item in list_team_template_summaries(config_base)
        }
        store = get_team_binding_store()
        bindings = store.list()
        bindings_by_name = {binding.team_name: binding for binding in bindings}
        entity_store = get_team_entity_store()
        teams = [self._team_binding_payload(binding) for binding in bindings]
        known_team_names = {str(item.get("team_name") or "") for item in teams}
        teams.extend(self._legacy_team_bindings_from_sessions(known_team_names))

        enriched: list[dict[str, Any]] = []
        for item in teams:
            team_name = str(item.get("team_name") or "").strip()
            template_id = str(item.get("template_id") or "").strip()
            active_session_id = active_by_team.get(team_name, "")
            legacy = bool(item.get("legacy", False))
            entity = None
            entity_path = ""
            if team_name and not legacy:
                binding = bindings_by_name.get(team_name)
                if binding is not None:
                    entity = ensure_team_entity_for_binding(binding, config_base=config_base, store=entity_store)
                else:
                    entity = entity_store.get(team_name)
                if entity is not None:
                    item["template_id"] = entity.template_id
                    template_id = entity.template_id
                    entity_path = str(entity_store.entity_path(team_name))
            source_template_available = bool(template_id and template_id in templates)
            team_config_available = entity is not None
            template_available = bool(team_config_available or source_template_available)
            selectable = bool(team_name and not active_session_id and not legacy and team_config_available)
            disabled_reason = ""
            if active_session_id:
                disabled_reason = "active"
            elif legacy:
                disabled_reason = "legacy"
            elif not team_config_available:
                disabled_reason = "team_config_missing"
            item.update(
                {
                    "template_available": template_available,
                    "source_template_available": source_template_available,
                    "team_config_available": team_config_available,
                    "team_config_path": entity_path,
                    "session_count": len(item.get("session_ids") or []),
                    "active_session_id": active_session_id,
                    "selectable": selectable,
                    "disabled_reason": disabled_reason,
                }
            )
            enriched.append(item)

        enriched.sort(key=lambda row: float(row.get("updated_at") or 0), reverse=True)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"teams": enriched},
            metadata=request.metadata,
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_team_binding_create(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from jiuwenswarm.runtime.session_delete import TeamDeletionInProgress
        from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStoreError
        from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStoreError

        params = request.params if isinstance(request.params, dict) else {}
        team_name = str(params.get("team_name") or "")
        template_id = str(params.get("template_id") or "").strip()
        config_base = get_config()
        try:
            binding = await self._create_team_binding_from_template(
                team_name=team_name,
                template_id=template_id,
                config_base=config_base,
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"team": binding.to_dict()},
                metadata=request.metadata,
            )
        except (TeamBindingStoreError, TeamEntityStoreError, TeamDeletionInProgress) as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": getattr(exc, "code", "BAD_REQUEST")},
                metadata=request.metadata,
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_team_binding_generate(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from jiuwenswarm.agents.harness.team import (
            TeamNameGenerationError,
        )
        from jiuwenswarm.runtime.session_delete import TeamDeletionInProgress
        from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStoreError
        from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStoreError

        params = request.params if isinstance(request.params, dict) else {}
        description = str(params.get("description") or params.get("prompt") or "").strip()
        config_base = get_config()
        try:
            binding, default_template = await self._create_generated_team_binding(
                description=description,
                config_base=config_base,
            )

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "team": binding.to_dict(),
                    "template": default_template,
                },
                metadata=request.metadata,
            )
        except TeamNameGenerationError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "GENERATION_FAILED"},
                metadata=request.metadata,
            )
        except (TeamBindingStoreError, TeamEntityStoreError, TeamDeletionInProgress) as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": getattr(exc, "code", "BAD_REQUEST")},
                metadata=request.metadata,
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_team_session_bind(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from jiuwenswarm.runtime.session_delete import TeamDeletionInProgress
        from jiuwenswarm.server.runtime.session.session_metadata import update_session_metadata
        from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStoreError, get_team_binding_store
        from jiuwenswarm.server.runtime.team_entity_store import (
            TeamEntityStoreError,
            ensure_team_entity_for_binding,
            get_team_entity_store,
        )

        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(params.get("session_id") or request.session_id or "").strip()
        team_name = str(params.get("team_name") or "").strip()
        _, _, canonical_mode = resolve_agent_request_mode(params.get("mode", "team"))
        try:
            if not session_id:
                raise TeamBindingStoreError("session_id is required", code="BAD_REQUEST")
            if not (get_agent_sessions_dir() / session_id).is_dir():
                raise TeamBindingStoreError("session not found", code="NOT_FOUND")
            binding_store = get_team_binding_store()
            existing_binding = binding_store.get(team_name)
            if existing_binding is None:
                raise TeamBindingStoreError("team binding not found", code="NOT_FOUND")
            entity = ensure_team_entity_for_binding(existing_binding, config_base=get_config())
            if entity is None:
                raise TeamBindingStoreError("team entity config missing", code="NOT_FOUND")
            from jiuwenswarm.runtime.session_delete import TEAM_DELETION_GATE

            async with TEAM_DELETION_GATE.mutation_lock(team_name):
                TEAM_DELETION_GATE.assert_not_deleting_locked(team_name)
                binding = binding_store.bind_session(
                    team_name=team_name,
                    session_id=session_id,
                )
                update_session_metadata(
                    session_id=session_id,
                    channel_id=str(request.channel_id or "").strip() or None,
                    mode=canonical_mode,
                    team_name=binding.team_name,
                    team_template_id=binding.template_id,
                    sync=True,
                )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "session_id": session_id,
                    "team_name": binding.team_name,
                    "team_template_id": binding.template_id,
                    "mode": canonical_mode,
                    "team": binding.to_dict(),
                    "team_config_path": str(get_team_entity_store().entity_path(binding.team_name)),
                },
                metadata=request.metadata,
            )
        except (TeamBindingStoreError, TeamEntityStoreError, TeamDeletionInProgress) as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": getattr(exc, "code", "BAD_REQUEST")},
                metadata=request.metadata,
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_team_delete(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """Validate transport input and delegate Team deletion to AgentRuntime."""

        params = request.params if isinstance(request.params, dict) else {}
        is_team = is_team_params(params)
        team_name = str(params.get("team_name") or "").strip()

        if not team_name:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "team_name is required", "code": "BAD_REQUEST"},
                metadata=request.metadata,
            )
        elif not is_team:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "error": "team.delete is only supported for team mode",
                    "code": "UNSUPPORTED_MODE",
                },
                metadata=request.metadata,
            )
        else:
            try:
                result = await self._execution_runtime().delete_team(
                    team_name=team_name,
                    channel_id=request.channel_id,
                )
                payload = {
                    "team_name": result.team_name,
                    "session_ids": list(result.session_ids),
                    "failed_session_ids": list(result.failed_session_ids),
                    "deleted": result.deleted,
                    "recovery_required": result.recovery_required,
                }
                if result.error_message:
                    payload["error"] = result.error_message
                if result.error_code:
                    payload["code"] = result.error_code
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=result.ok,
                    payload=payload,
                    metadata=request.metadata,
                )
            except Exception as exc:
                logger.exception("[AgentWebSocketServer] Runtime Team delete failed")
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": str(exc), "code": "DELETE_FAILED"},
                    metadata=request.metadata,
                )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_lifecycle_request(self, ws, request, send_lock) -> bool:
        from jiuwenswarm.server.runtime.session.session_archive import SessionArchiveService
        from jiuwenswarm.server.runtime.session import lifecycle as lc
        method = request.req_method.value if request.req_method else ""
        methods = {
            "session.archive", "session.unarchive", "session.archived.list",
            "session.delete",
            "cron.sessions.delete",
            "project.sessions.archive", "project.sessions.delete_archived",
            "project.lifecycle",
        }
        if method not in methods:
            return False
        service = getattr(self, "_archive_service", None)
        if service is None:
            service = self._archive_service = SessionArchiveService(self._execution_runtime())
        params = request.params if isinstance(request.params, dict) else {}
        ok = True
        try:
            if method == "session.archived.list":
                payload = await asyncio.to_thread(service.list_sessions, params)
            elif method == "cron.sessions.delete":
                payload = await service.delete_cron_sessions(
                    params.get("cron_id"), request.channel_id or ""
                )
            elif method.startswith("project.sessions."):
                payload = await service.project_batch(
                    params.get("project_id"), method.rsplit(".", 1)[1], request.channel_id or ""
                )
            elif method == "project.lifecycle" and params.get("events"):
                # Full-directory scan of lifecycle state: keep it off the loop.
                payload = {"events": await asyncio.to_thread(lc.event_snapshots)}
            elif method == "project.lifecycle" and params.get("inventory"):
                from jiuwenswarm.server.runtime.session.project_store import list_projects
                payload = {"projects": [dict(project_id=p.project_id,
                            operation=lc.state("project", p.project_id).get("operation"))
                            for p in list_projects(include_hidden=True, cache_bust=True)]}
                known = {item["project_id"] for item in payload["projects"]}
                directory = lc.get_agent_root_dir() / "lifecycle" / "resources"
                for path in directory.glob("project_*.json"):
                    operation = lc.read_json(path).get("operation") or {}
                    project_id = operation.get("resource_id")
                    if project_id and project_id not in known and operation.get("status") != "completed":
                        payload["projects"].append(dict(project_id=project_id, operation=operation))
            elif method == "project.lifecycle":
                project_id = lc.validate_id(params.get("project_id"))

                def _project_snapshot():
                    # cron 准入闸门会并发打这个分支：读盘必须离开事件循环
                    # （issue #4885，同 events 分支的 to_thread 先例）。
                    # state 只读一次，经 projection(value=) 复用，同一 JSON 不读两遍。
                    from jiuwenswarm.server.runtime.session.project_store import get_project_by_id
                    return lc.state("project", project_id), get_project_by_id(
                        project_id, cache_bust=True
                    )

                value, project = await asyncio.to_thread(_project_snapshot)
                payload = lc.projection("project", project_id, value=value)
                payload["exists"] = project is not None
                # 调度闸门(project_execution_allowed)据此拒隐藏项目:被移除
                # 项目的定时任务不到点触发、不进任务列表。
                payload["hidden"] = bool(project is not None and project.hidden)
                payload["operation"] = value.get("operation")
                if params.get("running_sessions"):
                    # project.remove 的移除前预检专用;cron 准入的常规查询不
                    # 带该参数,不付全量会话扫描的成本。判定与 project.remove
                    # 的 busy 扫描(_project_busy_sessions)完全一致。
                    if project is None or project.hidden:
                        payload["has_running_sessions"] = False
                    else:
                        from jiuwenswarm.server.runtime.gateway_adapter.project_adapter import (
                            _project_busy_sessions,
                        )
                        payload["has_running_sessions"] = bool(
                            await asyncio.to_thread(
                                _project_busy_sessions,
                                project_id,
                                self._execution_runtime(),
                            )
                        )
            elif method.startswith("session."):
                ids = lc.parse_ids(params, delete=method == "session.delete")
                results = []
                for sid in ids:
                    try:
                        results.append(await service.session(
                            sid,
                            method.split(".")[1],
                            request.channel_id or "",
                        ))
                    except lc.LifecycleError as exc:
                        results.append(dict(session_id=sid, ok=False, code=exc.code, error=str(exc), **exc.details))
                if method == "session.delete" and "session_ids" not in params:
                    ok = results[0]["ok"]
                    # project_id 超出 §5.10.5 单条字段表，但 Gateway 需要
                    # 它发出符合 §5.10.11 契约的 session.deleted 事件。
                    payload = {"session_id": ids[0], "project_id": results[0].get("project_id", "")} if ok else {
                        key: value for key, value in results[0].items()
                        if key not in {"session_id", "ok"}
                    }
                else:
                    succeeded = sum(item["ok"] for item in results)
                    payload = dict(succeeded_count=succeeded, failed_count=len(results) - succeeded, results=results)
            # methods 集合已穷尽上面的分支;不再有项目级删除级联,
            # 任何新增方法都必须在这里拿到显式分支。
        except lc.LifecycleError as exc:
            ok, payload = False, dict(code=exc.code, error=str(exc), **exc.details)
        except Exception as exc:
            logger.exception("lifecycle operation failed")
            ok, payload = False, dict(code="INTERNAL_ERROR", error=str(exc))
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=ok,
            payload=payload,
            metadata=request.metadata,
        )
        async with send_lock:
            await send_wire_payload(ws, encode_agent_response_for_wire(resp, response_id=request.request_id))
        return True


    @staticmethod
    def _send_error_response(ws: Any, request: AgentRequest,
                              send_lock: asyncio.Lock, error: str,
                              code: str | None = None) -> dict[str, Any]:
        """Build an error AgentResponse wire payload."""
        payload: dict[str, Any] = {"error": error}
        if code:
            payload["code"] = code
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload=payload,
            metadata=request.metadata,
        )
        return encode_agent_response_for_wire(
            resp,
            response_id=request.request_id,
        )




    async def _handle_a4p_request(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        """Handle a4p.* requests forwarded from the Web User Authorizer."""
        from jiuwenswarm.agents.harness.common.a4p_rpc import dispatch_a4p_request

        resp = await dispatch_a4p_request(request)
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)



    @staticmethod
    def _organization_stream_key(request: AgentRequest) -> str:
        params = request.params if isinstance(request.params, dict) else {}
        session_id = request.session_id or "default"
        team_id = str(params.get("target_team_id") or "").strip()
        return f"{session_id}::organization::{team_id}" if team_id else session_id

    async def _cancel_targeted_expert(self, request: AgentRequest) -> AgentResponse:
        """Cancel only a same-organization expert stream, never the owner turn."""
        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(request.session_id or "").strip()
        team_id = str(params.get("target_team_id") or "").strip()
        owner_id = str((get_session_metadata(session_id) or {}).get("team_name") or "")
        owner = self._resolve_team_backend(session_id, owner_id)
        target = self._resolve_team_backend(session_id, team_id)
        owner_org = getattr(getattr(owner, "org_task_manager", None), "organization_id", None)
        target_org = getattr(getattr(target, "org_task_manager", None), "organization_id", None)
        intent = str(params.get("intent") or "cancel")
        success = bool(owner_org and owner_org == target_org and team_id != owner_id and intent == "cancel")
        if success:
            tasks = []
            for task, stop in list(self._session_stream_tasks.get(self._organization_stream_key(request), {}).items()):
                if not task.done():
                    stop.set()
                    task.cancel()
                    tasks.append(task)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=success,
            payload={
                "event_type": "chat.interrupt_result",
                "success": success,
                "team_id": team_id,
                "source": "org_expert_direct",
                **({"error": "expert target is invalid or intent is unsupported"} if not success else {}),
            },
        )

    @staticmethod
    def _is_targeted_expert_chat(request: AgentRequest) -> bool:
        """Return whether chat.send explicitly targets a non-owner Team."""
        if request.req_method != ReqMethod.CHAT_SEND:
            return False
        params = request.params if isinstance(request.params, dict) else {}
        if not is_team_mode(params.get("mode")):
            return False
        team_id = str(params.get("target_team_id") or "").strip()
        if not team_id:
            return False

        session_id = str(request.session_id or params.get("session_id") or "").strip()
        owner_team_id = str(
            (get_session_metadata(session_id) or {}).get("team_name") or ""
        ).strip()
        return team_id != owner_team_id

    async def _targeted_expert_runtime_stream(self, request: AgentRequest) -> Any:
        """Adapt organization output to the existing Runtime wire/keepalive path."""
        stream = self._targeted_expert_response_stream(request)
        try:
            async for chunk in stream:
                yield RuntimeEvent.from_agent_message(
                    chunk,
                    request_id=request.request_id,
                    channel_id=request.channel_id or "default",
                    session_id=request.session_id,
                )
        finally:
            await stream.aclose()

    async def _targeted_expert_response_stream(self, request: AgentRequest) -> Any:
        """Run one selected expert as a normal cancellable AgentServer stream."""
        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(request.session_id or "").strip()
        team_id = str(params.get("target_team_id") or "").strip()
        channel_id = request.channel_id or "default"

        def _chunk(payload: dict[str, Any], *, complete: bool = False) -> AgentResponseChunk:
            return AgentResponseChunk(
                request_id=request.request_id,
                channel_id=channel_id,
                payload=payload,
                is_complete=complete,
            )

        error = ""
        owner_team_id = ""
        if session_id:
            owner_team_id = str(
                (get_session_metadata(session_id) or {}).get("team_name") or ""
            ).strip()
        owner_backend = self._resolve_team_backend(session_id, owner_team_id)
        target_backend = self._resolve_team_backend(session_id, team_id)
        owner_org_id = str(
            getattr(getattr(owner_backend, "org_task_manager", None), "organization_id", None)
            or ""
        ).strip()
        target_org_id = str(
            getattr(getattr(target_backend, "org_task_manager", None), "organization_id", None)
            or ""
        ).strip()
        if not session_id or target_backend is None:
            error = "target expert team is not bound to this session"
        elif not owner_org_id or target_org_id != owner_org_id:
            error = "target expert team does not belong to the owner's organization"
        precheck_failed = bool(error)

        succeeded = False
        if not error:
            from jiuwenswarm.agents.harness.team.expert_org.launcher import (
                JiuwenExpertTeamLauncher,
            )
            query = _request_query_text(request)
            append_history_record(
                session_id=session_id,
                request_id=request.request_id,
                channel_id=channel_id,
                role="user",
                content=query,
                timestamp=time.time(),
                extra={
                    "team_id": team_id,
                    "team_name": team_id,
                    "source": "org_expert_direct",
                },
                channel_metadata=request.metadata,
                mode="team",
            )
            succeeded = await JiuwenExpertTeamLauncher().run_organization_turn(
                team_id,
                session_id,
                {
                    "query": query,
                    **{key: params[key] for key in ("media_items", "files") if key in params},
                },
                source="org_expert_direct",
                channel_id=channel_id,
                request_id=request.request_id,
            )
            if not succeeded:
                error = "target expert team is unavailable or busy"

        if error:
            # Precheck only: turn-time skips are logged in ExpertTeamLauncher.
            if precheck_failed:
                logger.warning(
                    "[AgentWebSocketServer] targeted expert chat denied session=%s "
                    "owner_team=%s target_team=%s owner_org=%s target_org=%s error=%s",
                    session_id,
                    owner_team_id,
                    team_id,
                    owner_org_id,
                    target_org_id,
                    error,
                )
            error_payload = {
                "event_type": "chat.error",
                "error": error,
                "team_id": team_id,
                "team_name": team_id,
                "source": "org_expert_direct",
            }
            if not precheck_failed:
                # The stream owns direct-turn errors; persist the same payload
                # without also pushing a duplicate event through the launcher.
                append_history_record(
                    session_id=session_id,
                    request_id=request.request_id,
                    channel_id=channel_id,
                    role="assistant",
                    event_type="chat.error",
                    content=error,
                    timestamp=time.time(),
                    extra=error_payload,
                    mode="team",
                )
            yield _chunk(error_payload)
        yield _chunk(
            {
                "event_type": "chat.processing_status",
                "is_processing": False,
                "team_id": team_id,
                "team_name": team_id,
                "source": "org_expert_direct",
                "success": succeeded,
            },
            complete=True,
        )

    async def _handle_team_snapshot(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from jiuwenswarm.agents.harness.team import get_team_manager
        from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import (
            TeamMonitorHandler,
        )

        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(params.get("session_id") or request.session_id or "").strip()
        channel_id = request.channel_id or "web"
        empty_payload = {"members": [], "tasks": [], "team_id": None}

        # ``team_name`` 显式指定了要看的 team（team 选择器切 team 时就是这么调的）。
        requested_team_name = str(params.get("team_name") or "").strip()

        team_manager = get_team_manager(channel_id)
        monitor_handler = team_manager.get_monitor_handler(session_id) if session_id else None

        # TeamManager 的 monitor 表按 session_id 索引，一个 session 只保住最后一个
        # monitor；而 TeamRuntimePool 允许一个 session 同时挂多个 team（专家团就是
        # 每个 team 一条 `org-expert-*` 记录）。此时 session 级的 live 快照属于
        # *另一个* team，直接返回会把别队的成员/任务画到当前选中的 team 上。
        monitor_team = monitor_handler.team_id if monitor_handler is not None else None
        live_matches_request = (
            monitor_handler is not None
            and monitor_handler.is_running
            and (not requested_team_name or monitor_team == requested_team_name)
        )

        snapshot: dict[str, Any] | None = None
        source = "empty"
        if live_matches_request:
            try:
                snapshot = await monitor_handler.get_team_snapshot()
                if snapshot is not None:
                    source = "live"
            except Exception as e:
                logger.warning("[AgentWebSocketServer] team.snapshot (live) failed: %s", e)

        def _snapshot_tasks(payload: dict[str, Any] | None) -> list[Any]:
            if not isinstance(payload, dict):
                return []
            tasks = payload.get("tasks")
            return tasks if isinstance(tasks, list) else []

        # 请求的 team 活着、但 session 级 monitor 不是它（多 team 并发的会话）：另开一个
        # 精确绑定 (team_name, session_id) 的只读 monitor 读一次。单个 monitor 实例会读
        # 全量成员/消息，会话一多就是 N 倍 DB 开销，所以只在"请求的 team 不是当前
        # monitor"这个确实需要区分的场景才走这条路，且用完即弃。
        if snapshot is None and requested_team_name and session_id:
            try:
                from openjiuwen.core.runner.runner import GLOBAL_RUNNER

                scoped_monitor = await GLOBAL_RUNNER.get_agent_team_monitor(
                    team_name=requested_team_name,
                    session_id=session_id,
                )
                if scoped_monitor is not None:
                    scoped_snapshot = await TeamMonitorHandler(
                        scoped_monitor, session_id
                    ).get_team_snapshot()
                    if scoped_snapshot is not None:
                        snapshot = scoped_snapshot
                        source = "live-scoped"
            except Exception as e:
                logger.warning(
                    "[AgentWebSocketServer] team.snapshot (scoped live) failed: "
                    "session_id=%s team_name=%s error=%s",
                    session_id,
                    requested_team_name,
                    e,
                )

        # History restore often hits this RPC after the monitor has stopped, OR
        # while a live handler is still registered but already returns a truthy
        # empty board ({tasks: [], members: [], team_id: ...}). `if not snapshot`
        # alone would skip DB in that case and leave the frontend with no
        # title/content. Fall back whenever live has no tasks.
        needs_db = snapshot is None or not _snapshot_tasks(snapshot)
        if needs_db and session_id:
            team_name = requested_team_name
            if not team_name:
                team_name = str(
                    team_manager.get_active_team_name(session_id) or ""
                ).strip()
            if not team_name:
                team_name = str(
                    (get_session_metadata(session_id) or {}).get("team_name") or ""
                ).strip()
            if team_name:
                try:
                    db_snapshot = await TeamMonitorHandler.get_team_snapshot_from_db(
                        session_id, team_name
                    )
                except Exception as e:
                    logger.warning(
                        "[AgentWebSocketServer] team.snapshot (db) failed: "
                        "session_id=%s team_name=%s error=%s",
                        session_id,
                        team_name,
                        e,
                    )
                    db_snapshot = None
                # Prefer DB when it has tasks, or when live was missing entirely.
                # If both boards have empty tasks, keep live so in-memory
                # members (if any) are not wiped by an empty DB read.
                if db_snapshot is not None and (
                    snapshot is None or _snapshot_tasks(db_snapshot)
                ):
                    snapshot = db_snapshot
                    source = "db"

        payload = {
            **(snapshot or empty_payload),
            "members_source": source if snapshot is not None else "empty",
        }
        members = payload.get("members") if isinstance(payload, dict) else []
        tasks = _snapshot_tasks(payload if isinstance(payload, dict) else None)
        logger.info(
            "[AgentWebSocketServer] team.snapshot session_id=%s source=%s "
            "tasks_count=%s members_count=%s",
            session_id or "-",
            source if snapshot is not None else "empty",
            len(tasks),
            len(members) if isinstance(members, list) else 0,
        )

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=channel_id,
            ok=True,
            payload=payload,
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _team_list_entries(self, session_id: str, channel_id: str) -> list[dict[str, Any]]:
        """Collect every team that currently belongs to ``session_id``.

        Two sources are merged because a session can hold several teams, and
        they do not live in the same registry:

        * the Runner-owned ``TeamRuntimePool`` — the multi-team object pool,
          keyed by ``team_name``, entries bound to ``current_session_id``;
        * ``TeamManager``'s per-session markers (``_active_team_names`` /
          ``_pending_team_names``) plus session metadata ``team_name``, which
          cover sessions whose runtime has not been pooled yet (or is already
          torn down but still resumable).

        Later sources only fill gaps, so a pooled entry keeps its live state.
        """
        from jiuwenswarm.agents.harness.team import get_team_manager

        entries: dict[str, dict[str, Any]] = {}

        def _upsert(team_name: str, *, state: str, **extra: Any) -> None:
            team_name = str(team_name or "").strip()
            if not team_name:
                return
            existing = entries.get(team_name)
            if existing is None:
                entries[team_name] = {
                    "team_id": team_name,
                    "team_name": team_name,
                    "display_name": None,
                    "state": state,
                    "leader_id": None,
                    "organization_id": None,
                    "is_leader": True,
                    "capabilities": [],
                    "agent_group_name": None,
                    "source": extra.get("source") or "runtime",
                }
                existing = entries[team_name]
            for key, value in extra.items():
                if value is not None and existing.get(key) in (None, [], ""):
                    existing[key] = value
            # A live pool entry outranks a local marker, and running outranks
            # paused; never downgrade the state we already recorded.
            if state == "running" or existing.get("state") in (None, ""):
                existing["state"] = state

        runner_pool = None
        try:
            from openjiuwen.core.runner.runner import GLOBAL_RUNNER

            runtime_mgr = getattr(GLOBAL_RUNNER, "_team_runtime_manager", None)
            runner_pool = getattr(runtime_mgr, "pool", None)
        except Exception as e:
            logger.warning("[AgentWebSocketServer] team.list pool lookup failed: %s", e)

        if runner_pool is not None and session_id:
            try:
                for active in await runner_pool.teams_for_session(session_id):
                    backend = getattr(active.agent, "team_backend", None)
                    if backend is not None and not getattr(backend, "is_leader", True):
                        # Only leader-backed teams are addressable as a unit.
                        continue
                    spec_metadata = dict(
                        getattr(getattr(active.agent, "spec", None), "metadata", None)
                        or {}
                    )
                    organization_id = getattr(
                        getattr(backend, "org_task_manager", None), "organization_id", None
                    )
                    _upsert(
                        active.team_name,
                        state=getattr(getattr(active, "state", None), "value", None) or "running",
                        display_name=getattr(getattr(active.agent, "card", None), "name", None),
                        leader_id=getattr(backend, "leader_member_name", None),
                        organization_id=organization_id,
                        agent_group_name=spec_metadata.get("agent_group_name"),
                        source="pool",
                    )
            except Exception as e:
                logger.warning(
                    "[AgentWebSocketServer] team.list pool read failed: session_id=%s error=%s",
                    session_id,
                    e,
                )

        team_manager = get_team_manager(channel_id)
        metadata_team_name = ""
        if session_id:
            session_info = team_manager.get_runtime_team_snapshot().get(session_id)
            if session_info:
                _upsert(
                    str(session_info.get("team_name") or ""),
                    state="running" if session_info.get("state") == "active" else "pending",
                    source="manager",
                )

            active_name = team_manager.get_active_team_name(session_id)
            if active_name:
                _upsert(active_name, state="running", source="manager")
            pending_name = vars(team_manager).get("_pending_team_names", {}).get(session_id)
            if pending_name:
                _upsert(pending_name, state="pending", source="manager")

            metadata = get_session_metadata(session_id) or {}
            metadata_team_name = str(metadata.get("team_name") or "").strip()
            if metadata_team_name:
                _upsert(
                    metadata_team_name,
                    state="configured",
                    agent_group_name=metadata.get("agent_group_name"),
                    source="metadata",
                )

        for entry in entries.values():
            entry["is_owner"] = bool(
                metadata_team_name and entry["team_id"] == metadata_team_name
            )
            if not entry.get("organization_id"):
                org_id = self._lookup_session_org_binding(session_id, entry["team_id"])
                if org_id:
                    entry["organization_id"] = org_id

        return list(entries.values())

    @staticmethod
    def _lookup_session_org_binding(session_id: str, team_id: str) -> str | None:
        """Best-effort organization id for a team, read off its live backend."""
        if not session_id or not team_id:
            return None
        try:
            from openjiuwen.core.runner.runner import GLOBAL_RUNNER

            runtime_mgr = getattr(GLOBAL_RUNNER, "_team_runtime_manager", None)
            pool = getattr(runtime_mgr, "pool", None)
            teams = getattr(pool, "_teams", None)
            if not isinstance(teams, dict):
                return None
            active = teams.get(team_id)
            if active is None or getattr(active, "current_session_id", None) != session_id:
                return None
            backend = getattr(active.agent, "team_backend", None)
            organization_id = getattr(
                getattr(backend, "org_task_manager", None), "organization_id", None
            )
            return str(organization_id) if organization_id else None
        except Exception:
            return None

    async def _handle_team_list(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """List the teams belonging to one session, for the team selector."""
        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(params.get("session_id") or request.session_id or "").strip()
        channel_id = request.channel_id or "web"

        teams: list[dict[str, Any]] = []
        try:
            if session_id:
                teams = await self._team_list_entries(session_id, channel_id)
        except Exception as e:
            logger.warning(
                "[AgentWebSocketServer] team.list failed: session_id=%s error=%s", session_id, e
            )

        default_team_id = None
        if teams:
            default_team_id = next(
                (t["team_id"] for t in teams if t.get("is_owner")),
                next((t["team_id"] for t in teams if t.get("state") == "running"), teams[0]["team_id"]),
            )
        logger.info(
            "[AgentWebSocketServer] team.list session_id=%s teams_count=%s", session_id or "-", len(teams)
        )

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=channel_id,
            ok=True,
            payload={"session_id": session_id, "teams": teams, "default_team_id": default_team_id},
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_org_snapshot(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """Return organization + task-pool state for the right-hand info box.

        The organization is always resolved from the exact session-bound Team
        backend. A caller-supplied ``organization_id`` is only a consistency
        check and never selects which organization is queried.
        """
        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(params.get("session_id") or request.session_id or "").strip()
        team_id = str(params.get("team_id") or "").strip()
        requested_organization_id = str(params.get("organization_id") or "").strip()
        channel_id = request.channel_id or "web"
        empty_payload: dict[str, Any] = {
            "organization": None,
            "tasks": [],
            "unclaimed_tasks": [],
            "pending_reviews": [],
            "stats": {},
        }

        backend = self._resolve_team_backend(session_id, team_id)
        organization_id = str(
            getattr(getattr(backend, "org_task_manager", None), "organization_id", None) or ""
        ).strip()
        if not backend or not organization_id:
            payload: dict[str, Any] = dict(empty_payload)
        elif requested_organization_id and requested_organization_id != organization_id:
            logger.warning(
                "[AgentWebSocketServer] org.snapshot organization mismatch: "
                "session_id=%s team_id=%s requested=%s bound=%s",
                session_id or "-",
                team_id or "-",
                requested_organization_id,
                organization_id,
            )
            payload = dict(empty_payload)
        else:
            payload = await self._build_org_snapshot(
                session_id=session_id,
                team_id=team_id,
                organization_id=organization_id,
                backend=backend,
            )

        logger.info(
            "[AgentWebSocketServer] org.snapshot session_id=%s team_id=%s organization_id=%s tasks=%s",
            session_id or "-",
            team_id or "-",
            organization_id or "-",
            len(payload.get("tasks") or []),
        )

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=channel_id,
            ok=True,
            payload=payload,
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _build_org_snapshot(
        self,
        *,
        session_id: str,
        team_id: str,
        organization_id: str,
        backend: Any,
    ) -> dict[str, Any]:
        """Read one organization's spec + task pool through its process manager."""
        from openjiuwen.agent_teams.organization.pool import get_process_org_manager

        empty_payload: dict[str, Any] = {
            "organization": None,
            "tasks": [],
            "unclaimed_tasks": [],
            "pending_reviews": [],
            "stats": {},
        }

        db = getattr(backend, "db", None)
        if db is None:
            logger.warning(
                "[AgentWebSocketServer] org.snapshot has no db handle: session_id=%s team_id=%s",
                session_id or "-",
                team_id or "-",
            )
            return empty_payload

        manager = get_process_org_manager(
            organization_id=organization_id,
            db=db,
            messager=getattr(backend, "messager", None),
            session_id=session_id or None,
        )
        task_pool = manager.task_pool

        organization = await manager.get_organization()
        if organization is None:
            return empty_payload

        tasks = await task_pool.list_tasks(limit=200)
        unclaimed = [t for t in tasks if t.unclaimed is not None]
        open_tasks = [t for t in tasks if str(t.status) == "OPEN"]

        pending_reviews: list[dict[str, Any]] = []
        if team_id:
            try:
                pending_reviews = await task_pool.list_pending_reviews(team_id=team_id, limit=50)
            except Exception as e:
                logger.warning(
                    "[AgentWebSocketServer] org.snapshot pending reviews failed: "
                    "organization_id=%s team_id=%s error=%s",
                    organization_id,
                    team_id,
                    e,
                )

        status_counts: dict[str, int] = {}
        for task in tasks:
            key = str(task.status)
            status_counts[key] = status_counts.get(key, 0) + 1

        return {
            "organization": {
                "organization_id": organization.organization_id,
                "display_name": organization.display_name,
                "description": organization.description,
                "owner_team_id": organization.owner_team_id,
                "owner_leader_id": organization.owner_leader_id,
                "leaders": [
                    {
                        "organization_id": leader.organization_id,
                        "team_id": leader.team_id,
                        "leader_id": leader.leader_id,
                        "leader_member_name": leader.leader_member_name,
                        "capabilities": list(leader.capabilities or []),
                    }
                    for leader in organization.leaders
                ],
                "metadata": dict(organization.metadata or {}),
                "unclaimed_task_policy": organization.unclaimed_task_policy.model_dump(),
            },
            "tasks": [task.brief() | {"description": (task.description or "")[:280]} for task in tasks],
            "unclaimed_tasks": [task.brief() for task in unclaimed],
            "pending_reviews": pending_reviews,
            "stats": {
                "total": len(tasks),
                "open": len(open_tasks),
                "unclaimed": len(unclaimed),
                "pending_reviews": len(pending_reviews),
                "by_status": status_counts,
            },
        }

    @staticmethod
    def _resolve_team_backend(session_id: str, team_id: str) -> Any | None:
        """Return only the backend matching this exact session and Team."""
        if not session_id or not team_id:
            return None
        try:
            from openjiuwen.core.runner.runner import GLOBAL_RUNNER

            runtime_mgr = getattr(GLOBAL_RUNNER, "_team_runtime_manager", None)
            pool = getattr(runtime_mgr, "pool", None)
            teams = getattr(pool, "_teams", None)
            if not isinstance(teams, dict):
                return None
            active = teams.get(team_id)
            if active is None or getattr(active, "current_session_id", None) != session_id:
                return None
            return getattr(active.agent, "team_backend", None)
        except Exception as e:
            logger.warning(
                "[AgentWebSocketServer] resolve team backend failed: session_id=%s team_id=%s error=%s",
                session_id,
                team_id,
                e,
            )
        return None

    async def _handle_team_mq_publish(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        """Relay one external team event into the active core team runtime."""
        from jiuwenswarm.agents.harness.team import get_team_manager

        session_id = request.session_id or ""
        channel_id = request.channel_id or "web"
        payload = request.params.get("payload")

        if not session_id:
            success, reason = False, "session_id is required"
        elif payload is None:
            success, reason = False, "payload is required"
        elif not isinstance(payload, dict) or payload.get("type") != "team.external_event":
            success, reason = False, "invalid_external_event"
        else:
            success, reason = await get_team_manager(channel_id).interact(session_id, payload)

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=channel_id,
            ok=success,
            payload={"published": True} if success else {"error": reason},
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_team_members_get(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """返回 team human_agent 席位列表供 /join 校验。

        纯查询透传：mismatch 校验与对外文案均在 gateway，server 只查 member、过滤
        human_agent、回 ok/members。查不到或异常 → ok=False（payload 不带文案，由
        gateway 拼"team 不存在"）。
        """
        from jiuwenswarm.server.runtime.agent_adapter.team_helpers import (
            query_team_human_members_for_join,
        )

        params = request.params if isinstance(request.params, dict) else {}
        session_id = params.get("session_id") or request.session_id or ""
        team_name = str(params.get("team_name") or "").strip()
        channel_id = request.channel_id or "web"

        try:
            members_raw = await query_team_human_members_for_join(session_id, team_name)
        except Exception:
            logger.exception(
                "[AgentWebSocketServer] team.members.get failed: session=%s team=%s",
                session_id, team_name,
            )
            members_raw = []
        members = [
            m for m in members_raw
            if isinstance(m, dict) and m.get("role") == "human_agent" and m.get("member_id")
        ]
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=channel_id,
            ok=bool(members),
            payload={"members": members},
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_swarmflow_pause(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """Handle swarmflow.pause RPC — pause a live swarmflow run by run_id."""
        await self._run_swarmflow_control(ws, request, send_lock, action="pause")

    async def _handle_swarmflow_resume(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """Handle swarmflow.resume RPC — resume a paused swarmflow run by run_id."""
        await self._run_swarmflow_control(ws, request, send_lock, action="resume")

    async def _handle_swarmflow_stop(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """Handle swarmflow.stop RPC — stop a swarmflow run by run_id."""
        await self._run_swarmflow_control(ws, request, send_lock, action="stop")

    async def _run_swarmflow_control(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
        *,
        action: str,
    ) -> None:
        """Shared pause/resume/stop control-path handler for a swarmflow run.

        Looks up the session's BackgroundTaskController and applies the requested
        control to the run identified by ``run_id`` (accepting ``run_id`` or the
        ``workflow_run_id`` alias). Returns ok=False with a reason when run_id is
        missing, the team is asleep (no leader harness to host the run — the
        tree-view buttons are greyed then), or no matching run
        is registered on the controller.
        """
        from jiuwenswarm.server.runtime.agent_adapter.team_helpers import (
            classify_swarmflow_control_miss,
            get_background_task_controller,
        )

        session_id = request.session_id or ""
        channel_id = request.channel_id or "web"
        params = request.params if isinstance(request.params, dict) else {}
        run_id = params.get("run_id") or params.get("workflow_run_id")

        from jiuwenswarm.agents.harness.team import get_team_manager
        from openjiuwen.agent_teams.runtime.pool import RuntimeState
        from openjiuwen.core.runner import Runner

        tm = get_team_manager(channel_id)
        active_teams = await Runner.list_active_teams()
        team_runtime_running = any(
            str(info.current_session_id or "") == session_id
            and info.state == RuntimeState.RUNNING
            for info in active_teams
        )
        if not isinstance(run_id, str) or not run_id.strip():
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=channel_id,
                ok=False,
                payload={"error": "run_id is required"},
            )
        elif not team_runtime_running:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=channel_id,
                ok=False,
                payload={"error": "工作流控制不可用，发送一条消息唤醒团队后即可使用。"},
            )
        else:
            run_id = run_id.strip()
            controller = get_background_task_controller(session_id)
            status = {"pause": "paused", "resume": "resumed", "stop": "stopped"}.get(action, "")
            if action == "pause":
                acted = await controller.pause(run_id)
            elif action == "resume":
                acted = await controller.resume(run_id)
            elif action == "stop":
                acted = await controller.stop(run_id)
                # 已解栈的 paused run 没有引擎回发的 WORKFLOW_STOPPED，快照会永远停在
                # paused；由 handler 合成终态 delta（树刷新 + 落盘），不写 journal seal
                # （丢票不 seal，手动 resume_id 仍可续）。active run 由引擎事件路径更新，
                # stop_run 对非 paused run 是 no-op。
                wf_handler = tm.get_workflow_handler(session_id)
                if wf_handler is not None and await wf_handler.stop_run(run_id):
                    acted = True
            else:  # pragma: no cover - internal dispatch only
                acted = False
            if not acted:
                # controller 注册表 miss ≠ run 不存在：run 可能已经自然终态
                # （用户点的是一张停在 running/paused 的陈旧卡片），也可能是
                # 进程重启后注册表清空而状态快照还在。由 team 层找回权威
                # status 一并返回，前端据此纠正卡片并明确提示，而不是
                # 一句 not found。
                miss = classify_swarmflow_control_miss(channel_id, session_id, run_id)
                if miss is not None:
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=channel_id,
                        ok=False,
                        payload=miss,
                    )
                else:
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=channel_id,
                        ok=False,
                        payload={"error": "workflow run not found"},
                    )
            else:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=channel_id,
                    ok=True,
                    payload={"run_id": run_id, "status": status},
                )

        logger.info(
            "[SWARMFLOW] %s req channel_id=%s session_id=%s request_id=%s run_id=%s ok=%s",
            action,
            channel_id,
            session_id,
            request.request_id,
            run_id,
            resp.ok,
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_team_history_get(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """返回 team 模式历史记录的分页，避免与 history.get 并发竞争。

        支持可选 member_name 参数：传入时仅返回与该 member 相关的记录
        （p2p 消息 / @all 广播 / teammate 输出）。
        """
        params = request.params if isinstance(request.params, dict) else {}
        session_id = params.get("session_id")
        member_name = params.get("member_name")
        channel_id = request.channel_id or "web"

        if not isinstance(session_id, str) or not session_id.strip():
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=channel_id,
                ok=False,
                payload={"error": "session_id is required"},
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await send_wire_payload(ws, wire)
            return

        session_id = session_id.strip()
        try:
            if member_name and isinstance(member_name, str) and member_name.strip():
                records = await asyncio.to_thread(
                    read_member_history_records, session_id, str(member_name).strip()
                )
            else:
                records = await asyncio.to_thread(read_team_history_records, session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[team.history.get] read failed: session_id=%s error=%s", session_id, exc)
            records = []

        sanitized_records = [
            _sanitize_history_record_for_wire(record)
            for record in records
            if isinstance(record, dict)
        ]
        total = len(sanitized_records)
        cursor = _coerce_int(
            params.get("cursor", params.get("offset", 0)),
            default=0,
            minimum=0,
            maximum=max(0, total),
        )
        limit = _coerce_int(
            params.get("limit"),
            default=_TEAM_HISTORY_DEFAULT_LIMIT,
            minimum=1,
            maximum=_TEAM_HISTORY_MAX_LIMIT,
        )
        max_bytes = _coerce_int(
            params.get("max_bytes"),
            default=_TEAM_HISTORY_DEFAULT_MAX_BYTES,
            minimum=_TEAM_HISTORY_MIN_MAX_BYTES,
            maximum=_TEAM_HISTORY_MAX_MAX_BYTES,
        )
        page_records, next_cursor = _select_history_record_page(
            sanitized_records,
            cursor=cursor,
            limit=limit,
            max_bytes=max_bytes,
            session_id=session_id,
        )
        logger.debug(
            "[team.history.get] session_id=%s member=%s total=%d cursor=%d returned=%d next_cursor=%d max_bytes=%d",
            session_id, str(member_name or ""), total, cursor,
            len(page_records), next_cursor, max_bytes,
        )

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=channel_id,
            ok=True,
            payload={
                "records": page_records,
                "session_id": session_id,
                "cursor": cursor,
                "next_cursor": next_cursor,
                "has_more": next_cursor < total,
                "total": total,
            },
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)



    async def _get_asset_publish_api(self):
        async with self._asset_publish_lock:
            if self._asset_publish_api is None:
                from jiuwenswarm.common.utils import get_workspace_dir
                from jiuwenswarm.server.runtime.marketplace.asset_publish_api import AssetPublishAPI
                api = AssetPublishAPI(get_workspace_dir() / "marketplace" / "publishing")
                try:
                    await api.start()
                except BaseException:
                    await api.close()
                    raise
                self._asset_publish_api = api
            return self._asset_publish_api

    async def _start_asset_services(self):
        try:
            await self._get_asset_publish_api()
        except Exception:
            logger.warning("[AssetPublish] startup unavailable; requests may retry initialization")
        try:
            from jiuwenswarm.common.utils import get_agent_workspace_dir
            from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
            from jiuwenswarm.server.runtime.marketplace.hub_catalog_cache import start_hub_catalog_preload
            await start_hub_catalog_preload(SkillManager(workspace_dir=str(get_agent_workspace_dir())))
        except Exception:
            logger.warning("[HubCatalog] preload unavailable; requests may load on demand")

    async def _handle_asset_publish(self, ws, request, send_lock):
        from jiuwenswarm.server.runtime.marketplace.asset_publish_api import PublishAPIError
        try:
            if request.channel_id != "web":
                raise PublishAPIError("WEB_CHANNEL_REQUIRED")
            api = await self._get_asset_publish_api()
            payload = await api.call(request.req_method.value.rsplit(".", 1)[-1], request.params or {},
                                     gateway_user=request.user_id)
            ok = True
        except PublishAPIError as exc:
            payload = {"code": exc.code, "error": exc.code, "can_submit": False,
                       "errors": [{"code": exc.code, "field": exc.field}]}
            ok = False
        except Exception:
            # Do not log request params, authentication or arbitrary exception bodies.
            payload = {"code": "PUBLISH_UNAVAILABLE", "error": "PUBLISH_UNAVAILABLE", "can_submit": False}
            ok = False
        response = AgentResponse(request_id=request.request_id, channel_id=request.channel_id,
                                 ok=ok, payload=payload, agent_ref=request.agent_ref)
        wire = encode_agent_response_for_wire(response, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)


    @staticmethod
    def _resolve_adapter(agent: Any) -> Any:
        """从 JiuwenSwarm 中提取底层 Deep/Code Adapter (持 _sys_operation_card 的实例)."""
        if agent is None:
            return None
        for attr in ("_adapter", "adapter", "_active_adapter"):
            inner = getattr(agent, attr, None)
            if inner is not None and hasattr(inner, "apply_sandbox_runtime_patch"):
                return inner
        # 兜底: agent 本身有相关方法
        if hasattr(agent, "apply_sandbox_runtime_patch"):
            return agent
        return None

    @staticmethod
    def resolve_adapter(agent: Any) -> Any:
        """Public wrapper for :meth:`_resolve_adapter` (避开 protected-access)."""
        return AgentWebSocketServer._resolve_adapter(agent)

    @staticmethod
    def _parse_sandbox_host_port(url: str) -> tuple[str, int]:
        """从 sandbox url 解析 host:port; 默认 127.0.0.1:8321."""
        from jiuwenswarm.server.sandbox.host_port import parse_sandbox_host_port

        return parse_sandbox_host_port(url)

    @staticmethod
    def _is_tcp_port_bindable(host: str, port: int) -> bool:
        """``True`` 表示当前能在 ``host:port`` 上 ``bind`` 成功 (即没有被占用)。

        不去探测 ``/health`` 之类应用层信息——只看四层占用情况, 谁占着、占着的
        是不是 jiuwenbox 都不关心。
        """
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            try:
                sock.bind((host, port))
            except OSError:
                return False
            return True
        finally:
            sock.close()

    @staticmethod
    def _pick_free_tcp_port(host: str) -> int:
        """让内核挑一个空闲端口 (``bind`` 到 0); 仅用于绑定测试, 不会真正监听。

        存在 TOCTOU 风险 (返回后端口可能立即被别人抢), 但接下来 uvicorn 起来
        通常足够快; 即便撞上, uvicorn 自己会因 EADDRINUSE 失败, 上游再报错。
        """
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
            return int(sock.getsockname()[1])

    def _allocate_internal_jiuwenbox_port(
        self,
        host: str,
        preferred_port: int,
    ) -> int:
        """internal 模式下确定 jiuwenbox 实际监听端口。

        - 若本 runner 已经在 ``host:preferred_port`` 上拥有一个仍在跑的 jiuwenbox,
          直接复用 (避免重复 spawn);
        - 否则若 ``preferred_port`` 当前无人占用, 用之;
        - 再否则让内核挑一个空闲端口返回。
        """
        if self._jiuwenbox_runner.is_owned_listener(host, preferred_port):
            return preferred_port
        if self._is_tcp_port_bindable(host, preferred_port):
            return preferred_port
        new_port = self._pick_free_tcp_port(host)
        logger.warning(
            "[command.sandbox] preferred port %s:%d is busy; "
            "allocating fresh port %d for new jiuwenbox instance",
            host,
            preferred_port,
            new_port,
        )
        return new_port

    async def _reload_after_config_save(
        self,
        updated_keys: Any = None,
        *,
        env_updates: dict[str, Any] | None = None,
        config_payload: dict[str, Any] | None = None,
        reload_options: dict[str, Any] | None = None,
    ) -> bool:
        """Apply a ConfigAdapter save to the agents in this process."""
        return await reload_runtime_after_config_save(
            self._agent_manager,
            updated_keys,
            env_updates=env_updates,
            config_payload=config_payload,
            reload_options=reload_options,
        )

    async def send_push(self, msg) -> bool:
        """AgentServer 主动向 Gateway 推送消息。

        payload 格式与 AgentResponse.payload 一致，
        可含 event_type 等字段供 Gateway 转为 Message 派发到 Channel。
        """
        if self._current_ws is None or self._current_send_lock is None:
            logger.warning(
                "[AgentWebSocketServer] send_push 失败: 无活跃 Gateway 连接"
            )
            return False

        try:
            payload = msg.get("payload") if isinstance(msg, dict) else None
            if isinstance(payload, dict) and payload.get("event_type") == "chat.error":
                # 集群在没有进行中的请求时（自主轮次）失败，走的是这条推送
                msg = {**msg, "payload": dict(payload)}
                self._annotate_model_error(msg["payload"], msg.get("session_id"))
            wire = build_server_push_wire(msg)
            async with self._current_send_lock:
                sent_original = await send_wire_payload(self._current_ws, wire)
            if not sent_original:
                logger.warning(
                    "[AgentWebSocketServer] send_push 内容过大已降级为错误帧: channel_id=%s",
                    msg.get("channel_id", ""),
                )
                return False
            response_kind = str(msg.get("response_kind") or "").strip()
            if response_kind:
                logger.info(
                    "[AgentWebSocketServer] send_push response_kind wire sent: channel_id=%s kind=%s",
                    msg.get("channel_id", ""),
                    response_kind,
                )
            else:
                logger.info(
                    "[AgentWebSocketServer] send_push 已发送(E2A wire): channel_id=%s",
                    msg.get("channel_id", ""),
                )
            return True
        except Exception as e:
            logger.warning("[AgentWebSocketServer] send_push 失败: %s", e)
            return False

    def get_agent(self):
        """获取 default agent 实例（向后兼容）."""
        return self._agent_manager.get_agent_nowait()

    def get_agent_manager(self) -> AgentManager:
        """获取 AgentManager 实例."""
        return self._agent_manager

    def get_runtime(self) -> AgentRuntime:
        """Return the transport-independent Runtime owned by AgentServer."""
        return self._runtime



    async def _handle_initialize(
            self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """处理 initialize 方法（非流式）.

        调用 AgentManager.initialize 完成初始化，返回 capabilities。

        Args:
            ws: WebSocket 连接
            request: AgentRequest
            send_lock: 发送锁
        """
        logger.info("[AgentServer] initialize: request_id=%s channel_id=%s", request.request_id, request.channel_id)

        try:
            params = request.params if isinstance(request.params, dict) else {}
            client_capabilities = params.get("clientCapabilities", {})
            logger.info(
                "[AgentServer] initialize clientCapabilities: %s",
                client_capabilities,
            )

            extra_config = {
                "protocol_version": params.get("protocolVersion", "0.1.0"),
                "client_capabilities": client_capabilities,
            }
            if request.channel_id == "acp":
                self._set_ws_acp_client_capabilities(ws, client_capabilities)

            channel_id = request.channel_id or "default"
            capabilities = await self._agent_manager.initialize(
                channel_id=channel_id,
                extra_config=extra_config,
            )
            if capabilities is None:
                capabilities = ACP_DEFAULT_CAPABILITIES.copy()

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=capabilities,
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await send_wire_payload(ws, wire)

            logger.info("[AgentServer] initialize completed: capabilities=%s", capabilities)

        except Exception as e:
            logger.exception("[AgentServer] initialize failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await send_wire_payload(ws, wire)

    async def _handle_session_create(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        """Translate ``session.create`` between WebSocket wire and Runtime."""
        operation = "session.create"
        logger.info(
            "[AgentServer] %s: request_id=%s",
            operation,
            request.request_id,
        )

        runtime = None
        prepared = None
        response_delivered = False

        async def abort_prepared(
            primary_error: BaseException | None,
        ) -> None:
            if (
                runtime is None
                or prepared is None
                or prepared.state is not SessionProvisionState.PREPARED
            ):
                return
            try:
                await runtime.abort_session_provision(prepared)
            except asyncio.CancelledError:
                if primary_error is None:
                    raise
                logger.warning(
                    "[AgentServer] session.create abort was cancelled while "
                    "preserving %s",
                    type(primary_error).__name__,
                )
            except Exception as abort_error:  # noqa: BLE001
                logger.warning(
                    "[AgentServer] session.create abort failed while preserving %s: %s",
                    (
                        type(primary_error).__name__
                        if primary_error is not None
                        else "normal completion"
                    ),
                    abort_error,
                )

        try:
            channel_id = request.channel_id or "default"
            params = request.params if isinstance(request.params, dict) else {}
            persist_session_supplied = "persist_session" in params
            raw_persist_session = params.get("persist_session", False)
            if not isinstance(raw_persist_session, bool):
                raise SessionProvisionError(
                    "persist_session must be a boolean",
                    code="BAD_REQUEST",
                )

            explicit_work_mode_marker = params.get("_work_mode_explicit")
            requested = params.get("session_id")
            requested_session_id = (
                requested.strip() if isinstance(requested, str) else ""
            )
            if requested_session_id and str(channel_id).strip().lower() == "tui":
                logger.warning(
                    "[AgentServer] TUI supplied session_id via session.create; "
                    "bypassing prewarm compatibility path: session_id=%s",
                    requested_session_id,
                )

            runtime = self._execution_runtime()
            await runtime.start()
            prepared = await runtime.prepare_session_create(
                SessionCreateInput(
                    channel_id=channel_id,
                    requested_session_id=requested_session_id or None,
                    previous_session_id=str(
                        params.get("previous_session_id") or ""
                    ).strip(),
                    create_token=str(params.get("create_token") or "").strip(),
                    persist_session=raw_persist_session,
                    persist_session_supplied=persist_session_supplied,
                    mode=params.get("mode", "agent"),
                    previous_mode=params.get("previous_mode"),
                    is_swarm=bool(params.get("is_swarm")),
                    team_hint=bool(params.get("team")),
                    project_id=params.get("project_id", ""),
                    project_dir=params.get("project_dir", ""),
                    cwd=params.get("cwd", ""),
                    work_mode=params.get("work_mode"),
                    work_mode_explicit=(
                        explicit_work_mode_marker
                        if isinstance(explicit_work_mode_marker, bool)
                        else None
                    ),
                    title=params.get("title", ""),
                    user_id=str(
                        getattr(request, "user_id", "")
                        or params.get("user_id", "")
                        or ""
                    ).strip(),
                    model_name=str(params.get("model_name") or "").strip(),
                    cron_id=str(params.get("cron_id") or "").strip(),
                )
            )
            params.pop("_work_mode_explicit", None)
            result = prepared.result

            # Preserve the established mutation visible to in-process callers.
            params["project_id"] = result.project_id
            params["project_dir"] = result.project_dir
            params["work_mode"] = result.work_mode
            params["mode"] = result.canonical_mode

            payload = {
                "sessionId": result.session_id,
                "session_id": result.session_id,
                "projectId": result.project_id,
                "projectDir": result.project_dir,
                "workMode": result.work_mode,
                "persist_session": result.persist_session,
                "prewarm_hit": result.prewarm_hit,
                "prewarm_status": result.prewarm_status,
            }
            if result.explicit_id_compatibility:
                payload.update(
                    {
                        "created": result.created,
                        "mode": result.canonical_mode,
                    }
                )

            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
            wire = encode_agent_response_for_wire(
                response,
                response_id=request.request_id,
            )
            async with send_lock:
                await send_wire_payload(ws, wire)
            response_delivered = True

            await runtime.commit_session_provision(
                prepared,
                timing=SessionProvisionCommitTiming.AFTER_RESULT_DELIVERY,
                context=SessionProvisionCommitContext(
                    foreground_scope_id=str(params.get("view_id") or f"ws:{id(ws)}")
                ),
            )
            logger.info(
                "[AgentServer] %s completed: session_id=%s",
                operation,
                result.session_id,
            )
        except SessionProvisionError as error:
            logger.warning("[AgentServer] %s rejected: %s", operation, error)
            await abort_prepared(error)
            if response_delivered:
                return
            payload = {"error": str(error)}
            if error.code is not None:
                payload["code"] = error.code
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload=payload,
            )
            wire = encode_agent_response_for_wire(
                response,
                response_id=request.request_id,
            )
            async with send_lock:
                await send_wire_payload(ws, wire)
        except Exception as error:
            logger.exception("[AgentServer] %s failed: %s", operation, error)
            await abort_prepared(error)
            if response_delivered:
                return
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(error)},
            )
            wire = encode_agent_response_for_wire(
                response,
                response_id=request.request_id,
            )
            async with send_lock:
                await send_wire_payload(ws, wire)
        finally:
            await abort_prepared(sys.exception())

    async def _handle_session_fork(
            self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Translate ``session.fork`` between WebSocket wire and Runtime.

        Args:
            ws: WebSocket connection.
            request: AgentRequest with source_session_id, target_session_id, title.
            send_lock: Send lock.
        """
        logger.info(
            "[AgentServer] session.fork: request_id=%s", request.request_id
        )

        runtime = None
        prepared = None
        try:
            params = request.params if isinstance(request.params, dict) else {}
            if "side_conversation" in params:
                raise SessionProvisionError(
                    "side_conversation is no longer supported",
                    code="BAD_REQUEST",
                )
            source = str(params.get("source_session_id") or "").strip()
            target = str(params.get("target_session_id") or "").strip()
            fork_title = str(params.get("title") or "").strip()
            equipment_override = params.get("session_equipment_override")
            if equipment_override is not None and not isinstance(equipment_override, dict):
                raise SessionProvisionError(
                    "session_equipment_override must be an object",
                    code="BAD_REQUEST",
                )
            fork_point = params.get("fork_point")
            if not isinstance(fork_point, dict):
                fork_point = {}
            channel_id = request.channel_id or "default"

            if not source:
                raise SessionProvisionError(
                    "source_session_id is required",
                    code="BAD_REQUEST",
                )

            runtime = self._execution_runtime()
            await runtime.start()
            prepared = await runtime.prepare_session_fork(
                SessionForkInput(
                    channel_id=channel_id,
                    source_session_id=source,
                    target_session_id=target or None,
                    title=fork_title,
                    cutoff_message_id=str(
                        fork_point.get("message_id") or ""
                    ).strip(),
                    cutoff_role=str(fork_point.get("role") or "").strip(),
                    cutoff_content=str(fork_point.get("content") or ""),
                    cutoff_timestamp=fork_point.get("timestamp"),
                    session_equipment_override=equipment_override,
                )
            )
            result = await runtime.commit_session_provision(
                prepared,
                timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY,
            )

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "session_id": result.session_id,
                    "source_session_id": result.source_session_id,
                    "title": result.title,
                },
            )
            wire = encode_agent_response_for_wire(
                resp, response_id=request.request_id
            )
            async with send_lock:
                await send_wire_payload(ws, wire)

            logger.info(
                "[AgentServer] session.fork completed: source=%s target=%s title=%s",
                source,
                result.session_id,
                result.title,
            )

        except SessionProvisionError as e:
            logger.warning("[AgentServer] session.fork rejected: %s", e)
            payload = {"error": str(e)}
            if e.code is not None:
                payload["code"] = e.code
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload=payload,
            )
            wire = encode_agent_response_for_wire(
                resp, response_id=request.request_id
            )
            async with send_lock:
                await send_wire_payload(ws, wire)
        except ValueError as e:
            logger.warning("[AgentServer] session.fork ValueError: %s", e)
            code = (
                "NOT_FOUND" if "not found" in str(e)
                else "ALREADY_EXISTS" if "already exists" in str(e)
                else "BAD_REQUEST"
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e), "code": code},
            )
            wire = encode_agent_response_for_wire(
                resp, response_id=request.request_id
            )
            async with send_lock:
                await send_wire_payload(ws, wire)
        except Exception as e:
            logger.exception("[AgentServer] session.fork failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
            wire = encode_agent_response_for_wire(
                resp, response_id=request.request_id
            )
            async with send_lock:
                await send_wire_payload(ws, wire)
        finally:
            if (
                runtime is not None
                and prepared is not None
                and prepared.state is SessionProvisionState.PREPARED
            ):
                primary_error = sys.exception()
                try:
                    await runtime.abort_session_provision(prepared)
                except asyncio.CancelledError:
                    if primary_error is None:
                        raise
                    logger.warning(
                        "[AgentServer] session.fork abort was cancelled while "
                        "preserving %s",
                        type(primary_error).__name__,
                    )
                except Exception as abort_exc:  # noqa: BLE001
                    logger.warning(
                        "[AgentServer] session.fork abort failed: %s",
                        abort_exc,
                    )

    async def _handle_acp_tool_response(
            self,
            ws: Any,
            request: AgentRequest,
            send_lock: asyncio.Lock,
    ) -> None:
        params = request.params if isinstance(request.params, dict) else {}
        jsonrpc_id = params.get("jsonrpc_id")
        response_payload = params.get("response")
        if not isinstance(response_payload, dict):
            response_payload = {}

        if get_acp_output_manager().complete_jsonrpc_response(jsonrpc_id, response_payload):
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"accepted": True},
            )
        else:
            logger.info(
                "[AgentServer] ignore unknown/late acp tool response: jsonrpc_id=%s request_id=%s",
                jsonrpc_id,
                request.request_id,
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "accepted": False,
                    "ignored": True,
                    "reason": "unknown_or_late_response",
                    "jsonrpc_id": jsonrpc_id,
                },
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def handle_acp_tool_response_for_test(
            self,
            ws: Any,
            request: AgentRequest,
            send_lock: asyncio.Lock,
    ) -> None:
        """Public test helper that delegates to ACP tool-response handling."""
        await self._handle_acp_tool_response(ws, request, send_lock)

    # ------------------------------------------------------------------
    # RSI 优化平台分发（B2）：统一走 RsiAgentServerHandlers（服务域/推送见 rsi 包）
    # ------------------------------------------------------------------

    async def _handle_rsi_request(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle any rsi.* unary method (including Harness installation).

        Builds the RSI service context lazily (one per server process) and
        wires:
        - harness_refs 快照提供方 = RSI active version, then controlled generic fallback;
        - send_push 包装 = ``self.send_push``（E2A server_push，零改动）。
        """
        try:
            handlers = self._get_rsi_handlers()
            # Production handlers expose ``handle_async`` because Harness
            # installation awaits the DeepAgent load chain.  Keep a small
            # compatibility fallback for injected/test handler objects that
            # only implement the original synchronous ``handle`` method.
            handle_async = getattr(handlers, "handle_async", None)
            if callable(handle_async):
                result = handle_async(request)
            else:
                result = handlers.handle(request)
            if inspect.isawaitable(result):
                result = await result
            if result.get("ok"):
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload=result.get("payload"),
                )
            else:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={
                        "error": str(result.get("error") or "rsi request failed"),
                        "code": str(result.get("code") or "INTERNAL_ERROR"),
                    },
                )
        except Exception as exc:
            logger.exception("[AgentServer] rsi request failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "INTERNAL_ERROR"},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    def _get_rsi_handlers(self):
        """Lazily construct the RSI service context + AgentServer handlers once."""
        if self._rsi_handlers is not None:
            return self._rsi_handlers
        from jiuwenswarm.agents.harness.common.rsi import build_rsi_service_context
        from jiuwenswarm.server.rsi import RsiAgentServerHandlers

        provider_mode = os.environ.get("RSI_PROVIDER_MODE", "").strip().lower()
        if not provider_mode:
            provider_mode = (
                "mock"
                if os.environ.get("RSI_USE_MOCK_PROVIDER", "").strip().lower() == "true"
                else "real"
            )
        # Both modes materialize task-private datasets, model configs, and the
        # Validation profile. Mock mode alone may omit a source Harness because
        # its provider does not execute or publish a real Harness package.
        context = build_rsi_service_context(
            None,
            enable_harness_materialization=True,
            allow_missing_harness=(provider_mode == "mock"),
        )
        if provider_mode == "mock":
            from jiuwenswarm.agents.harness.common.rsi.provider_factory import build_rsi_adapters

            context.register_adapters(
                build_rsi_adapters(
                    context.tasks_root,
                    mode="mock",
                    model_resolver=self._resolve_model,
                )
            )
        else:
            harness_provider = getattr(self, "_rsi_harness_provider", None)
            if harness_provider is None:
                from jiuwenswarm.agents.harness.common.rsi.harness_provider import HarnessProvider

                harness_provider = HarnessProvider(
                    context.tasks_root,
                    model_resolver=context.model_resolver,
                )
                self._rsi_harness_provider = harness_provider
            from jiuwenswarm.agents.harness.common.rsi.provider_factory import build_rsi_adapters

            context.register_adapters(
                build_rsi_adapters(
                    context.tasks_root,
                    mode="real",
                    model_resolver=self._resolve_model,
                )
            )
            context.register_harness_provider(harness_provider)
        context.bind_harness_installer(self._agent_manager)
        handlers = RsiAgentServerHandlers(
            context,
            send_push=self.send_push,
            harness_refs_provider=(
                None if provider_mode == "mock" else self._rsi_harness_refs_provider
            ),
            default_channel_id="web",
        )
        self._rsi_handlers = handlers
        return handlers

    @staticmethod
    def _rsi_harness_refs_provider(params: dict[str, Any] | None = None) -> str | None:
        """Resolve an explicit installed Plugin, or use the active Harness.

        ``package_id`` uses the same Plugin registry as chat. Omitting it keeps
        the existing baseline fallback; ``harness_id`` remains a legacy registry
        selector. Arbitrary browser-supplied paths are not resolved here.
        """
        from jiuwenswarm.agents.harness.common.rsi.harness_activation import (
            RsiHarnessActivationStore,
            resolve_native_harness_baseline,
        )
        from jiuwenswarm.agents.harness.common.rsi.context import get_rsi_workspace_root
        from jiuwenswarm.common.utils import get_user_workspace_dir
        requested_id = str(
            (params or {}).get("package_id") or ""
        ).strip()
        if requested_id:
            from jiuwenswarm.agents.harness.common.rsi.errors import RsiInvalidHarness
            from jiuwenswarm.server.runtime import extension_package_manager as equipment

            # Resolve the existing package selector through the same registry
            # as chat.send. Never silently replace an explicit selection by H0.
            try:
                if not equipment.is_plugin_allowed(requested_id):
                    raise ValueError(f"Plugin is not installed: {requested_id}")
                package = equipment.resolve_plugin_dir(requested_id)
                manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
                if manifest.get("mcps"):
                    raise ValueError("RSI isolated evaluation does not yet support Plugin MCP dependencies")
                return str(package.resolve())
            except (ValueError, OSError) as exc:
                raise RsiInvalidHarness(str(exc)) from exc
        try:
            active = RsiHarnessActivationStore(
                get_rsi_workspace_root() / "tasks"
            ).resolve_active_runtime_path()
            if active:
                return active
        except Exception as exc:
            logger.warning("[RSI] active Harness 定位失败，回退 generic registry: %s", exc)
        configured_harness_root = os.environ.get("RSI_HARNESS_ROOT", "").strip()
        harness_root = (
            Path(configured_harness_root).expanduser().resolve()
            if configured_harness_root
            else (Path(get_user_workspace_dir()) / "rsi" / "harness").resolve()
        )
        initial_refs = harness_root / "initial_harness_refs.yaml"
        if initial_refs.is_file():
            # This is a trusted baseline input.  RsiTaskMaterializer copies
            # the selected package/ref into the task directory before the
            # engine sees it, so the external seed is never task output.
            return str(initial_refs)
        from jiuwenswarm.agents.harness.common.auto_harness.service import (
            _HARNESS_PACKAGES_FILE,
        )
        try:
            data = {}
            if _HARNESS_PACKAGES_FILE.is_file():
                with _HARNESS_PACKAGES_FILE.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
            active_ids = data.get("active_package_ids") or []
            packages = data.get("packages") or []
            by_id = {str(p.get("id")): p for p in packages if isinstance(p, dict)}
            legacy_id = str((params or {}).get("harness_id") or "").strip()
            for package_id in ([legacy_id] if legacy_id else active_ids):
                package = by_id.get(str(package_id))
                if not package:
                    continue
                runtime_path = str(package.get("runtime_path") or "")
                if runtime_path and Path(runtime_path).expanduser().is_dir():
                    # openjiuwen's epoch checkpoint composes retained changes
                    # by copying the referenced role directory.  Prefer the
                    # package root; load_plugin also accepts this directory.
                    return str(Path(runtime_path).expanduser().resolve())
                config_path = str(package.get("config_path") or "")
                if config_path and Path(config_path).expanduser().is_file():
                    return str(Path(config_path).expanduser().resolve())
        except Exception as exc:
            logger.warning("[RSI] harness refs 定位失败: %s", exc)
        baseline = resolve_native_harness_baseline()
        if baseline is not None:
            logger.info("[RSI] No active Harness found; using native Agent baseline")
            return str(baseline)
        return None


    @staticmethod
    def _annotate_model_error(payload: dict, session_id: Optional[str]) -> None:
        if payload.get("code"):
            return
        try:
            from jiuwenswarm.common.auth.apig import classify_model_error
            from jiuwenswarm.common.auth.login_credentials import session_uses_login_model

            code = classify_model_error(
                str(payload.get("error") or ""),
                login_model=session_uses_login_model(session_id),
            )
        except Exception:  # noqa: BLE001 — 分类失败不该影响错误本身的上报
            return
        if code:
            payload["code"] = code
            payload["upstream"] = True

    def _resolve_model(self, model_name: Optional[str] = None) -> Optional[Any]:
        """Resolve model from jiuwenswarm config.

        Args:
            model_name: Requested model name, falls back to default if None or not found

        Returns:
            Model instance or None if config cannot be loaded
        """
        # Build model cache if not already done
        if not self._model_cache:
            self._build_model_cache()

        # Resolve by name or use default
        if model_name and model_name in self._model_cache:
            return self._model_cache[model_name]
        if model_name:
            # 缓存是启动时的一次性快照，登录后新拿到的模型不在里面；静默回退到默认模型会让
            # 用户"选了 A 却跑了 B"。所以未命中时重建一次再查，仍然没有才回退。
            self._build_model_cache()
            if model_name in self._model_cache:
                return self._model_cache[model_name]
            logger.warning(
                "[_resolve_model] 模型 %r 不在可用列表里，回退到默认模型；可用模型: %s",
                model_name,
                sorted(self._model_cache),
            )
        return self._default_model

    def reset_model_cache(self) -> None:
        """清空模型缓存,下次 _resolve_model 触发懒重建。

        供 Zen 免费模型就绪回调使用:预热异步化后首个请求可能早于 Zen 拉取
        完成构建不含 Zen 条目的缓存(一次性、不自动重建),就绪后清空即可让
        重建带上 Zen 免费模型及占位符默认模型的 Zen 兜底。
        """
        if self._model_cache:
            self._model_cache.clear()
        self._default_model = None

    def _build_model_cache(self) -> None:
        """Build model cache from jiuwenswarm config.yaml (reuse interface_deep logic)."""
        # Use the same model building function as interface_deep
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import build_model_from_entry

        config = get_config()

        # Build from models.defaults list
        # 用 get_available_models：配置的模型 + 登录后自动获得的模型都要能选中
        for entry in get_available_models(config):
            mcc = entry.get("model_client_config") or {}
            model_name = mcc.get("model_name")
            if not model_name:
                continue
            mco = entry.get("model_config_obj") or {}
            self._model_cache[model_name] = build_model_from_entry(mcc, mco)

        # Fallback to legacy format if needed (same as interface_deep._build_model_cache_legacy)
        if not self._model_cache:
            default_model_config = config.get("models", {}).get("default", {})
            react_config = config.get("react", {})
            mcc = dict(
                default_model_config.get("model_client_config")
                or react_config.get("model_client_config")
                or {}
            )
            model_name = mcc.get("model_name") or react_config.get("model_name") or "gpt-4"
            if "model_name" not in mcc:
                mcc["model_name"] = model_name
            mco = (
                default_model_config.get("model_config_obj")
                or react_config.get("model_config_obj")
                or {}
            )
            self._model_cache[model_name] = build_model_from_entry(mcc, mco)

        # Set default model (first one)
        if self._model_cache:
            first_name = next(iter(self._model_cache))
            self._default_model = self._model_cache[first_name]
            logger.info(
                "[AgentServer] Built model cache with %d models, default=%s",
                len(self._model_cache), first_name
            )

        # 追加 Opencode Zen 免费模型（内存态，不入 config.yaml）。
        # 这些模型可被 _resolve_model 按名解析，但 _default_model 保持上面的用户自配模型。
        try:
            from jiuwenswarm.server.runtime.opencode_zen import (
                get_zen_free_model_entries,
            )
            for zent in get_zen_free_model_entries():
                zmcc = zent.get("model_client_config") or {}
                zname = zmcc.get("model_name", "")
                if zname and zname not in self._model_cache:
                    self._model_cache[zname] = build_model_from_entry(
                        zmcc, zent.get("model_config_obj") or {}
                    )
        except Exception:
            logger.debug(
                "[AgentServer] append zen free models to cache failed",
                exc_info=True,
            )

        # 首次启动兜底：默认模型仍为 .env 占位符时，改选 Zen 免费模型（如
        # DeepSeek V4 Flash）作为默认，避免把占位模型发往厂商。仅内存态生效。
        if self._default_model is not None:
            try:
                from jiuwenswarm.common.model_config_validation import (
                    is_placeholder_model_entry,
                    model_client_config_view,
                )
                from jiuwenswarm.server.runtime.opencode_zen import (
                    get_zen_default_free_model_entry,
                )
                if is_placeholder_model_entry(
                    model_client_config_view(self._default_model.model_client_config)
                ):
                    zen_default = get_zen_default_free_model_entry()
                    if zen_default is not None:
                        zmcc = zen_default["model_client_config"]
                        zname = zmcc["model_name"]
                        self._model_cache[zname] = build_model_from_entry(
                            zmcc, zen_default.get("model_config_obj") or {}
                        )
                        self._default_model = self._model_cache[zname]
                        logger.info(
                            "[AgentServer] default model is placeholder; "
                            "fallback to zen free model=%s",
                            zname,
                        )
            except Exception:
                logger.debug(
                    "[AgentServer] fallback default to zen free model failed",
                    exc_info=True,
                )

