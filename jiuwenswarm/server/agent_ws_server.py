# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentWebSocketServer - Gateway 与 AgentServer 之间的 WebSocket 服务端."""

from __future__ import annotations

import asyncio
import datetime as _dt
import importlib
import json
import logging
import math
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, ClassVar, Optional
from weakref import WeakValueDictionary

from openjiuwen.core.common.logging import server_logger
from websockets.exceptions import ConnectionClosed as WebSocketConnectionClosed

from jiuwenswarm.agents.harness.common.auto_harness import AutoHarnessService, reset_harness_packages_state
from jiuwenswarm.edition import is_enterprise
from jiuwenswarm.server.gateway_push.wire import build_server_push_wire
from jiuwenswarm.server.ws_send import send_wire_payload
from jiuwenswarm.agents.harness.common.tools.acp_output_tools import get_acp_output_manager
from jiuwenswarm.common.utils import (
    get_agent_sessions_dir,
    get_config_file,
    get_user_workspace_dir,
    mask_sensitive,
    resolve_tenant_sessions_dir,
)
from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.e2a.constants import (
    E2A_CANCEL_SOURCE_CLIENT_DISCONNECT,
    E2A_INTERNAL_CANCEL_SOURCE_KEY,
    E2A_RESPONSE_KIND_ACP_OUTPUT_REQUEST,
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
from jiuwenswarm.common.mode_matrix import is_team_mode
from jiuwenswarm.common.model_config_validation import is_placeholder_api_base
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.todo_snapshot import load_todo_snapshot_for_frontend
from jiuwenswarm.common.version import __version__
from jiuwenswarm.common.ws_diagnostics import (
    describe_ws_exception,
    describe_ws_peer,
    format_ws_diagnostics,
)
from jiuwenswarm.common.ws_limits import AGENT_WS_MAX_MESSAGE_BYTES
from jiuwenswarm.extensions.hook_event import AgentServerHookEvents
from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    is_interrupt_resume_payload,
)
from jiuwenswarm.agents.harness.common.rails.permissions.permissions_persist import persist_cli_trusted_directory
from jiuwenswarm.extensions.hooks_context import AgentServerChatHookContext, AgentWsServerStartHookContext
from jiuwenswarm.server.runtime.agent_manager import AgentManager, ACP_DEFAULT_CAPABILITIES
from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool
from jiuwenswarm.server.runtime.session.session_metadata import get_all_sessions_metadata
from jiuwenswarm.server.runtime.session.session_history import (
    append_compact_history_records,
    enrich_history_messages_session_id,
    history_exists,
    load_history_records,
    read_member_history_records,
    read_team_history_records,
)
from jiuwenswarm.server.runtime.session.session_metadata import remove_session_metadata_cache
from jiuwenswarm.server.runtime.agent_adapter.sysop_builder import (
    build_filesystem_policy,
    build_yuanrong_sandbox_status_view,
    effective_files_from_policy,
    find_auto_managed_match,
    find_nested_files_conflict,
    list_effective_sandbox_files,
    validate_sandbox_files_runtime,
)
from jiuwenswarm.server.utils.utils import is_team_params
from jiuwenswarm.server.context import AgentServerServices, RequestContext
from jiuwenswarm.server.dispatch import dispatch_to_handler
from jiuwenswarm.server.transports.push_registry import (
    make_ws_push_subscriber_id,
    get_push_registry,
)
from jiuwenswarm.server.transports.sink import WSSink
from jiuwenswarm.server.pipeline import dispatch_parsed_request
from jiuwenswarm.server.wire_parse import parse_inbound
from jiuwenswarm.server.handlers.ops import (  # noqa: F401  — re-exported for tests
    _reset_active_browser_runtimes_if_available,
)

# 默认路径实现在 handlers/_default.py。本模块留守的连接管理与解析段仍要用其中几个判定函数。
from jiuwenswarm.server.handlers._default import (  # noqa: F401  — re-exported for tests
    _handle_stream,
    _handle_unary,
    _is_explicit_plan_entry_request,
    _is_readonly_goal_get_request,
    _is_stateless_method_request,
    _prepare_tenant_code_mode_chat_turn,
    _session_mode_sync_lock,
    _should_sync_code_mode_state,
    _uses_tenant_pool,
)
# 进程级共享状态与「chat 轮次」方法集合定义在 _shared.py，两边 import 的是同一批对象。
from jiuwenswarm.server.handlers._shared import (  # noqa: F401  — re-exported for tests
    _CODE_MODE_SYNC_METHODS,
    _inject_plan_mode_activation_reminder,
    _session_mode_sync_locks,
    _sync_chat_request_metadata,
)

from jiuwenswarm.server.handlers._shared import (  # noqa: F401  — re-exported for tests
    _apply_resolved_mode_to_request,
    _is_team_metadata_mode,
    _background_session_kvc_tasks,
    _log_background_session_kvc_failure,
    _plan_active_sessions,
    _plan_exited_sessions,
    _request_query_text,
    _sessions_dir_for_request,
    resolve_agent_request_mode,
    resolve_request_project_dir,
)
from jiuwenswarm.server.handlers.commands import (
    _build_simplify_prompt,
    _extract_compact_summary_processor,
    _is_env_api_base_placeholder,
)
from jiuwenswarm.server.handlers.permissions import (
    _background_permission_reload_tasks,
    _log_permission_reload_failure,
)
from jiuwenswarm.server.handlers.sandbox import (
    _canonicalize_sandbox_files_path,
    _file_entry_matches_path,
    _reject_extra_sandbox_files_params,
    _require_sandbox_supported,
)
from jiuwenswarm.common.config import (
    DEFAULT_SANDBOX_POLICY_FILE,
    DEFAULT_SANDBOX_STARTUP_MODE,
    get_config,
    get_default_models,
    get_sandbox_endpoint,
    get_sandbox_runtime,
    get_sandbox_startup_mode,
    get_sandbox_startup_mode_explicit,
    resolve_preserve_file_sharing_mode_default,
    resolve_sandbox_policy_path,
    update_sandbox_endpoint,
    update_sandbox_runtime,
)
from jiuwenswarm.server.sandbox_config_rpc import get_sandbox_config_req_methods
from jiuwenswarm.server.sandbox.jiuwenbox_runner import JiuwenBoxRunner
from jiuwenswarm.runtime.host_services import (
    install_runtime_push_handler,
    restore_runtime_push_handler,
)
from jiuwenswarm.common.security.ws_origin import (
    extract_handshake_request,
    forbidden_origin_response,
    get_header_value,
    is_origin_check_enabled,
    is_allowed_browser_origin,
)
from jiuwenswarm.server.personal_context import PersonalContextHostAPI
from jiuwenswarm.server.personal_context.ws_handler import (
    PERSONAL_CONTEXT_REQUEST_METHODS as _PERSONAL_CONTEXT_REQ_METHODS,
    handle_personal_context_request,
)

logger = logging.getLogger(__name__)

_INTERFACE_DEEP_MODULE = "jiuwenswarm.server.runtime.agent_adapter.interface_deep"
_startup_warmup_task: asyncio.Task[None] | None = None


def _import_interface_deep_blocking() -> None:
    if _INTERFACE_DEEP_MODULE not in sys.modules:
        importlib.import_module(_INTERFACE_DEEP_MODULE)


async def _warm_interface_deep_module() -> None:
    """Import interface_deep in a worker thread so listen is not blocked."""
    if _INTERFACE_DEEP_MODULE in sys.modules:
        logger.info("[AgentWebSocketServer] interface_deep_warmup cache=hit elapsed_ms=0.0")
        return
    t0 = time.perf_counter()
    logger.info("[AgentWebSocketServer] interface_deep_warmup cache=miss starting background import")
    await asyncio.to_thread(_import_interface_deep_blocking)
    logger.info(
        "[AgentWebSocketServer] interface_deep_warmup cache=miss ok elapsed_ms=%.1f",
        (time.perf_counter() - t0) * 1000,
    )


def _is_std_cpython(python_exe: str) -> bool:
    """判断 python.exe 是否标准 CPython 安装 (非 venv trampoline/launcher).

    jbx-sandbox 跑不了 uv trampoline/venv launcher (WinError 5), runner 必须用
    标准 CPython. 判定: 同目录有 python3*.dll (CPython 根目录特征), 且不在
    .../Scripts/ 子目录 (venv 的 python.exe 在 Scripts/ 中, 无 python3*.dll).
    """
    p = Path(python_exe)
    try:
        if not p.is_file():
            return False
    except OSError:
        return False
    parent = p.parent
    if parent.name.lower() == "scripts":
        return False
    has_dll = any(parent.glob("python3*.dll"))
    return has_dll


async def ensure_interface_deep_and_checkpointer() -> None:
    """Make interface_deep importable without a synchronous import on this task.

    Chat/bootstrap used to ``from interface_deep import ...`` on the asyncio
    thread. If listen-after ``to_thread(import)`` was still running, that
    blocked the event loop on the import lock (WS recv + Creating starved).
    Await the background chain first; only then import (cache hit).
    """
    task = _startup_warmup_task
    if task is not None and not task.done():
        await task
    if _INTERFACE_DEEP_MODULE not in sys.modules:
        await _warm_interface_deep_module()
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        ensure_persistent_checkpointer,
    )

    await ensure_persistent_checkpointer()



from jiuwenswarm.server.wire_truncate import (  # noqa: F401  — re-exported for tests / handlers
    split_history_record_for_stream,
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
    _WORKFLOW_SNAPSHOT_MAX_BYTES,
    _WORKFLOW_SNAPSHOT_FRAME_OVERHEAD_BYTES,
    _WORKFLOW_SNAPSHOT_MAX_WORKFLOWS,
    _WORKFLOW_LIST_SUMMARY_STRING_LIMIT,
    _WORKFLOW_COLLAPSED_AGENT_TEXT_LIMIT,
    _WORKFLOW_WAITING_HUMAN_PROMPT_MAX_BYTES,
    _HISTORY_RESTORABLE_ASSISTANT_EVENT_TYPES,
    _json_wire_size,
    _coerce_int,
    _truncate_string_by_bytes,
    _compact_wire_metadata_value,
    _sanitize_history_wire_value,
    _collapse_oversized_history_record,
    _minimal_history_record_for_wire,
    _sanitize_history_record_for_wire,
    _select_history_record_page,
    _is_waiting_human_agent,
    _extract_waiting_human_prompts,
    _restore_waiting_human_prompts,
    _workflow_agent_for_collapse,
    _collapse_oversized_workflow_snapshot_item,
    _minimal_workflow_snapshot_item_for_wire,
    _minimal_workflow_detail_preserving_waiting_human,
    _sanitize_workflow_snapshot_item_for_wire,
    _fit_workflow_detail_to_budget,
    _workflow_list_summary_phase,
    _workflow_list_summary_item,
    _minimal_workflow_list_item,
    _fit_workflow_list_item_for_budget,
    _build_workflow_list_payload,
    _build_workflow_detail_payload,
    _find_workflow_agent,
    _build_workflow_human_prompt_payload,
    _build_workflow_snapshot_payload,
)


class _GatewayWSPushSink:
    """Gateway WS 连接在 :class:`PushRegistry` 里的推送出口。

    为什么不直接用 :class:`WSSink`
    -----------------------------
    ``PushRegistry.push`` 对**抛异常**的订阅者会当场注销（一条坏掉的 SSE 连接
    不该拖垮其他订阅者）。但 WS 侧要求的语义是**发送失败只记 warning，连接照旧
    留着** —— 直接塞裸 ``WSSink`` 会让一次瞬时发送失败就把 Gateway 踢出推送名单，
    直到重连才恢复。

    本类把异常吃在内部、返回 ``False``，注册表因此永远不会注销它。
    """

    __slots__ = ("_inner",)

    def __init__(self, ws: Any, send_lock: asyncio.Lock) -> None:
        self._inner = WSSink(ws, send_lock)

    async def send_wire(self, wire: dict[str, Any]) -> bool:
        try:
            return await self._inner.send_wire(wire)
        # CancelledError 继承 BaseException，不会被下面的 ``except Exception`` 接住，
        # 无需显式放行（同 push_registry.PushRegistry.push）。
        except Exception as e:  # noqa: BLE001 - 失败只 warning，不注销自己（见类 docstring）
            logger.warning("[AgentWebSocketServer] send_push 失败: %s", e)
            return False

    async def send_unary(self, resp: Any, *, response_id: str | None = None) -> bool:
        return await self._inner.send_unary(resp, response_id=response_id)

    async def send_chunk(self, chunk: Any, *, sequence: int, response_id: str | None = None) -> bool:
        return await self._inner.send_chunk(chunk, sequence=sequence, response_id=response_id)

    async def send_error(
        self, request_id: str, message: str, *, code: str = "INTERNAL_ERROR", channel_id: str = ""
    ) -> bool:
        return await self._inner.send_error(request_id, message, code=code, channel_id=channel_id)



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
        # 事件循环饥饿观测（非保活）：测量 sleep(1) 唤醒延迟，便于对齐 pong 超时。
        self._loop_lag_task: asyncio.Task[None] | None = None
        # send_push的推送订阅者统一由PushRegistry持有，本类不持有当前连接；
        # WS侧以每连接唯一id：``make_ws_push_subscriber_id(ws)``注册。
        # key是``_ws_capabilities_key``返回的str(id(ws))，与RequestContext.connection_id
        self._acp_client_capabilities_by_ws: dict[str, dict[str, Any]] = {}
        # AgentManager 实例（企业多租户入口见 TenantAgentPool，按企业版使用）
        self._agent_manager = AgentManager()
        # skills.* 等无状态 RPC：AgentManager 未缓存 agent 时复用的轻量 JiuWenSwarm，
        # 避免每次 cache miss 都 new 导致 SkillNet 异步安装等实例态断裂。
        self._stateless_fallback_agents: dict[str, Any] = {}
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
            home=get_user_workspace_dir() / ".personal_context",
        )
        self._personal_context_start_task: asyncio.Task[None] | None = None
        # Proactive recommendation engine (set by app_agentserver for debug trigger)
        self._proactive_engine: Any = None
        self._runtime_push_handler = None
        self._previous_runtime_push_handler = None
        get_acp_output_manager().set_send_push_callback(
            lambda msg: asyncio.create_task(self.send_push(msg))
        )
        get_push_registry().set_reverse_rpc_owner_lost_callback(
            lambda: get_acp_output_manager().fail_pending_requests(
                RuntimeError("Gateway reverse RPC connection lost")
            )
        )

    def set_proactive_engine(self, engine: Any) -> None:
        """Store the proactive engine instance for debug trigger interface."""
        self._proactive_engine = engine

    @staticmethod
    def _ws_capabilities_key(ws: Any) -> str:
        """连接标识。
        返回``str(id(ws))``而非``id(ws)``，与``RequestContext.connection_id``
        对齐 —— 业务层用``ctx.connection_id``写入、传输层用``ws``读取.
        """
        return str(id(ws))

    def _set_acp_client_capabilities(
        self, connection_id: str, capabilities: dict[str, Any] | None
    ) -> None:
        if isinstance(capabilities, dict):
            self._acp_client_capabilities_by_ws[connection_id] = dict(capabilities)
        else:
            self._acp_client_capabilities_by_ws.pop(connection_id, None)

    def _get_acp_client_capabilities(self, connection_id: str) -> dict[str, Any]:
        caps = self._acp_client_capabilities_by_ws.get(connection_id)
        return dict(caps) if isinstance(caps, dict) else {}

    def _set_ws_acp_client_capabilities(self, ws: Any, capabilities: dict[str, Any] | None) -> None:
        self._set_acp_client_capabilities(self._ws_capabilities_key(ws), capabilities)

    def _get_ws_acp_client_capabilities(self, ws: Any) -> dict[str, Any]:
        return self._get_acp_client_capabilities(self._ws_capabilities_key(ws))

    def _clear_ws_acp_client_capabilities(self, ws: Any) -> None:
        self._acp_client_capabilities_by_ws.pop(self._ws_capabilities_key(ws), None)

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

    @classmethod
    def current_instance(cls) -> AgentWebSocketServer | None:
        """Return the live singleton without creating a new AgentServer."""
        return cls._instance

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    # ---------- 生命周期 ----------

    async def start(self, *, listen: bool = True) -> None:
        """启动 WebSocket 服务端，开始监听连接。优先使用 legacy.server.serve 以与 Gateway 的 legacy client 握手兼容.

        注: persistent checkpointer 的初始化历史在 ``legacy_serve`` 之前同步 await,
        首次约耗时 ~14s (sqlite 文件 + openjiuwen 工厂反射), 期间 WS 端口未 listen,
        是 Gateway connect 重试 (头两次必失败, 白等 ~6s) 的元凶。现改为 ``legacy_serve``
        之后后台预热 (fire-and-forget), 让端口尽快开放; 首条 chat 请求若赶在预热完成前
        到达, 走 ``_ensure_persistent_checkpointer_response`` 兜底等待, 不影响握手.
        """
        await self._trigger_before_ws_server_start_hook()
        if self._server is not None:
            logger.warning("[AgentWebSocketServer] 服务端已在运行")
            return

        # Reset harness package state to native on service startup
        reset_harness_packages_state()

        if listen:
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
                    max_queue=None,
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
            logger.info(
                "[AgentWebSocketServer] 已启动: ws://%s:%s", self._host, self._port
            )
        else:
            logger.info("[AgentServer] handler initialized without an unauthenticated WS listener")
        # 启动端到端预热：interface_deep import → checkpointer → 临时 DeepAgent → query。
        # 拆成两个 task：
        #   - _startup_warmup_task 承载阶段1/2（import+checkpointer），快速有界，
        #     供 ensure_interface_deep_and_checkpointer 兜底 await。
        #   - 阶段3（临时 DeepAgent+query）独立 fire-and-forget，慢速，不兜底 await，
        #     避免首请求被预热 query（mock ~2-3s / 真实 LLM 最长 120s）阻塞。
        global _startup_warmup_task

        wm = (get_config().get("startup") or {}).get("warmup") or {}
        if not wm.get("enabled", True):  # 默认开启；关闭时跳过预热
            _startup_warmup_task = None
        else:
            async def _warmup_phase12() -> None:
                from jiuwenswarm.server.runtime.prewarm import warmup_import_and_checkpointer

                await warmup_import_and_checkpointer()

            async def _warmup_phase3() -> None:
                from jiuwenswarm.server.runtime.prewarm import warmup_deep_agent_query

                await warmup_deep_agent_query(
                    query=wm.get("query", "hello"),
                    channel_id=wm.get("channel_id", "__prewarm__"),
                    mode=wm.get("mode", "agent"),
                    timeout_s=wm.get("timeout_s", 120),
                    mock_model=wm.get("mock_model", True),  # 默认 mock，不消耗 token
                )

            _startup_warmup_task = asyncio.create_task(
                _warmup_phase12(), name="startup-warmup"
            )

            # 阶段3 在阶段1/2 完成后启动（独立 task，不阻塞 listen，不被兜底 await）。
            async def _phase3_after_phase12() -> None:
                task12 = _startup_warmup_task
                if task12 is not None:
                    await task12  # 等阶段1/2 完成再起阶段3（checkpointer 是 DeepAgent 前置）
                await _warmup_phase3()

            asyncio.create_task(_phase3_after_phase12(), name="startup-warmup-query")
        self._personal_context_start_task = asyncio.create_task(
            self._start_personal_context_best_effort(),
            name="personal-context-host-start",
        )
        # WS 监听已经开放, 现在按 config.yaml::sandbox 的 runtime.enabled +
        # startup_mode 决定要不要自动把 jiuwenbox 子进程也拉起来。失败不阻塞
        # 启动 (用户依然可以在 TUI 里跑 /sandbox enable 重试)。
        await self._bootstrap_internal_jiuwenbox()
        await self._start_loop_lag_monitor()
        self._install_runtime_push_host()
        if not is_enterprise():
            try:
                from jiuwenswarm.server.im.im_hosting.service import get_hosting_service

                hosting = get_hosting_service()
                hosting.set_agent_manager(TenantAgentPool.get_instance())
                await hosting.start()
            except Exception:
                logger.exception("[AgentWebSocketServer] im hosting poll 启动失败（已忽略）")

    def _install_runtime_push_host(self) -> None:
        """Register send_push as the optional Runtime host for this lifetime."""
        if self._runtime_push_handler is not None:
            return
        self._runtime_push_handler = self.send_push
        self._previous_runtime_push_handler = install_runtime_push_handler(
            self._runtime_push_handler
        )

    def _restore_runtime_push_host(self) -> None:
        """Drop this server's push ownership without reviving a removed owner."""
        handler = self._runtime_push_handler
        if handler is None:
            return
        restore_runtime_push_handler(handler, self._previous_runtime_push_handler)
        self._runtime_push_handler = None
        self._previous_runtime_push_handler = None

    async def _start_loop_lag_monitor(self) -> None:
        """启动事件循环 lag 观测 task 与停摆探针（验收用，不主动断连/不发应用心跳）。"""
        # 事件循环停摆探针 + 主线程栈采样器：用于定位「同步处理占住事件循环
        # 数秒导致网关 ping/pong 超时、任务被一刀切取消」类问题（见
        # ``jiuwenswarm/server/event_loop_monitor.py`` 模块 docstring）。
        # 幂等挂载；失败仅告警，绝不影响服务启动。
        try:
            from jiuwenswarm.server.event_loop_monitor import (
                ensure_event_loop_monitor,
            )

            await ensure_event_loop_monitor()
        except Exception:
            logger.exception(
                "[AgentWebSocketServer] 事件循环监控装载失败（已忽略）"
            )
        if self._loop_lag_task is not None and not self._loop_lag_task.done():
            return
        self._loop_lag_task = asyncio.create_task(
            self._loop_lag_monitor(),
            name="ws-loop-lag-monitor",
        )

    async def _loop_lag_monitor(self) -> None:
        """每隔约 1s 测量预期唤醒 vs 实际唤醒延迟。

        lag > 1.0s → WARNING；lag > 5.0s → ERROR。仅观测，不干预连接。
        """
        interval_s = 1.0
        while True:
            started = time.monotonic()
            await asyncio.sleep(interval_s)
            lag_s = time.monotonic() - started - interval_s
            if lag_s <= 1.0:
                continue
            lag_ms = lag_s * 1000.0
            if lag_s > 5.0:
                logger.error(
                    "[AgentWebSocketServer] event loop lag high lag_ms=%.0f",
                    lag_ms,
                )
            else:
                logger.warning(
                    "[AgentWebSocketServer] event loop lag elevated lag_ms=%.0f",
                    lag_ms,
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

        与 :meth:`_handle_sandbox_enable` 的其余差别:
        - 不调用 ``agent_manager.recreate_agent``: 启动阶段还没有任何会话/agent
          实例, 没东西需要重建; 后续会话首次进入时按现有 ``sandbox.url`` 直接装载。
        - 严格 best-effort: 任何失败 (policy 缺失 / 端口/spawn 失败) 一律记
          warning, 绝不让 agent-server 自身启动失败 (否则运维误配 yaml 会让整
          产品起不来, 也无从修复)。
        """
        try:
            # 企业级:沙箱由 agentserver Pod 内的 jiuwenbox 容器提供
            # (K8s 启动,external),不走 internal 子进程拉起;config.yaml 为
            # 部署工具下发的只读挂载,也不做回写
            if is_enterprise():
                logger.info(
                    "[sandbox_lifecycle] enterprise: sandbox 由 jiuwenbox "
                    "sidecar 提供, skipping internal jiuwenbox auto-start"
                )
                return
            # 非 Linux/Windows 平台直接跳过 auto-start: jiuwenbox 依赖平台专属
            # 内核能力 (Linux: bwrap/Landlock/命名空间; Windows: win_setup 用户
            # 创建 + WFP + ACL), 其它平台 (macOS 等) 起不来; 即便 spawn 成功后续
            # /sandbox 命令也会被 :func:`_require_sandbox_supported` 拒掉, 留着
            # 只会浪费一次失败的子进程启动。
            if not (sys.platform.startswith("linux") or sys.platform == "win32"):
                logger.info(
                    "[sandbox_lifecycle] skipping jiuwenbox auto-start: "
                    "/sandbox is only supported on Linux/Windows (current: %r)",
                    sys.platform,
                )
                return
            # sandbox.enabled 门控: false 时整个跳过 (不拉起 box-server, 不触发
            # install/创建 jbx-sandbox 用户)。优先级高于 startup_mode — enabled=false
            # 即使用户配了 startup_mode=internal 也不拉起。默认 false: shipped 模板
            # enabled=false, 用户不显式写 sandbox.enabled: true 就不拉起 (opt-in,
            # 避免开箱即 install + 建进程)。
            if sys.platform == "win32":
                sandbox_runtime = get_sandbox_runtime()
                if not sandbox_runtime.get("enabled"):
                    logger.info(
                        "[sandbox_lifecycle] sandbox.enabled=false, "
                        "skipping jiuwenbox auto-start (不拉起 box-server, 不创建沙箱用户). "
                        "如需沙箱, 在 config.yaml 设 sandbox.enabled: true + startup_mode: internal"
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

            # Windows: policy = 打包基底 (windows-policy.yaml) + workspace 副本
            # (windows-policy.runtime.yaml) 合并, 由 box-server PolicyReader.load_policy
            # 做 (机制对齐 config.yaml template+override). 副本路径经
            # JIUWENBOX_POLICY_PATH 注入 (基底由 box-server 自己解析). 副本不存在则
            # 建空骨架. Linux 不走 windows-policy.
            if sys.platform == "win32":
                try:
                    from jiuwenswarm.server.sandbox_policy_render import (
                        ensure_copy_exists,
                    )
                    runtime_policy = ensure_copy_exists()
                    if runtime_policy is not None and runtime_policy.is_file():
                        policy_path = runtime_policy
                        logger.info(
                            "[sandbox_lifecycle] using runtime policy copy: %s "
                            "(box-server merges base + copy)",
                            policy_path,
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[sandbox_lifecycle] ensure runtime copy failed, "
                        "fall back to base policy: %s",
                        exc,
                    )

            from jiuwenswarm.server.handlers.sandbox import (
                allocate_internal_jiuwenbox_port,
                parse_sandbox_host_port,
            )

            host, preferred_port = parse_sandbox_host_port(url)
            port = allocate_internal_jiuwenbox_port(
                AgentServerServices(self), host, preferred_port
            )
            if port != preferred_port:
                url = f"http://{host}:{port}"
                logger.info(
                    "[AgentWebSocketServer] jiuwenbox auto-start: "
                    "preferred port %d busy, using %d",
                    preferred_port,
                    port,
                )

            # 注入动态路径 env 给 box-server 子进程 (Windows 沙箱用):
            # JIUWENBOX_BUNDLED_PYTHON / JIUWENBOX_VENV_DIR / JIUWENBOX_RUNNER_PYTHON
            # (runner 用的标准 CPython, 非 uv venv — jbx-sandbox 跑不了 uv trampoline).
            # P0-5: 不写 os.environ (主进程全局污染), 改构建 sandbox_env dict 给
            # ensure_running(extra_env=...) 传子进程. 仅 Windows 需要.
            sandbox_env: dict[str, str] = {}
            if sys.platform == "win32":
                try:
                    from jiuwenswarm.server.runtime.pip_env import (
                        ensure_runtime_venv, resolve_base_python,
                    )
                    try:
                        bundled_python = resolve_base_python()
                        sandbox_env["JIUWENBOX_BUNDLED_PYTHON"] = str(bundled_python.parent)
                        sandbox_env["JIUWENCLAW_BASE_PYTHON"] = str(bundled_python)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "[sandbox_lifecycle] inject JIUWENBOX_BUNDLED_PYTHON failed: %s",
                            exc,
                        )
                    try:
                        venv_dir = ensure_runtime_venv()
                        sandbox_env["JIUWENBOX_VENV_DIR"] = str(venv_dir)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "[sandbox_lifecycle] inject JIUWENBOX_VENV_DIR failed: %s",
                            exc,
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[sandbox_lifecycle] inject sandbox python/venv env failed: %s",
                        exc,
                    )
            for _py_key in ("CLAW_PYTHON_HOME", "JIUWENCLAW_BASE_PYTHON"):
                _py_val = (os.environ.get(_py_key) or "").strip()
                if _py_val and not sandbox_env.get(_py_key):
                    sandbox_env[_py_key] = _py_val
            _desktop_data = (os.environ.get("JIUWENBOX_DESKTOP_DATA_DIR") or "").strip()
            if _desktop_data:
                sandbox_env["JIUWENBOX_DESKTOP_DATA_DIR"] = _desktop_data
            if getattr(sys, "frozen", False):
                sandbox_env["JIUWENBOX_RUNNER_PYTHON"] = str(Path(sys.executable).resolve())
            elif not (sandbox_env.get("JIUWENBOX_RUNNER_PYTHON")
                      or os.environ.get("JIUWENBOX_RUNNER_PYTHON") or "").strip():
                logger.info(
                    "[sandbox_lifecycle] JIUWENBOX_RUNNER_PYTHON "
                    "未注入, 探测候选路径..."
                )
                import shutil as _shutil
                import glob as _glob
                _runner_py: str | None = None
                _candidates: list[str] = []
                _candidates.append(
                    str(Path(__file__).resolve().parents[2] / "tools" / "python" / "python.exe"))
                _candidates += sorted(_glob.glob(r"C:\Python3*\python.exe"))
                _lad = os.environ.get("LOCALAPPDATA", "")
                if _lad:
                    _candidates += sorted(_glob.glob(
                        str(Path(_lad) / "Programs" / "Python" / "Python3*" / "python.exe")))
                # uv 管理的标准 CPython (非 Scripts trampoline)
                _roaming = os.environ.get("APPDATA", "")
                if _roaming:
                    _candidates += sorted(_glob.glob(
                        str(Path(_roaming) / "uv" / "python" / "cpython-*" / "python.exe")))
                if sys.executable and _is_std_cpython(sys.executable):
                    _candidates.insert(0, sys.executable)
                for _cand in _candidates:
                    if _cand and Path(_cand).is_file() and _is_std_cpython(_cand):
                        _runner_py = str(Path(_cand).resolve())
                        break
                if not _runner_py:
                    _which = _shutil.which("python") or _shutil.which("python3")
                    if _which and _is_std_cpython(_which):
                        _runner_py = str(Path(_which).resolve())
                if _runner_py:
                    sandbox_env["JIUWENBOX_RUNNER_PYTHON"] = _runner_py
            logger.info(
                "[sandbox_lifecycle] injected env: "
                "JIUWENBOX_VENV_DIR=%s, JIUWENBOX_BUNDLED_PYTHON=%s, "
                "JIUWENBOX_RUNNER_PYTHON=%s",
                sandbox_env.get("JIUWENBOX_VENV_DIR") or "<未注入>",
                sandbox_env.get("JIUWENBOX_BUNDLED_PYTHON") or "<未注入>",
                sandbox_env.get("JIUWENBOX_RUNNER_PYTHON") or "<未注入>",
                )
            ok = await self._jiuwenbox_runner.ensure_running(
                host=host,
                port=port,
                startup_mode="internal",
                policy_path=policy_path,
                extra_env=sandbox_env or None,
                timeout=180.0,
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
        """停止 WebSocket 服务端."""
        try:
            await self._stop_main_services()
        finally:
            await self._stop_personal_context_best_effort()

    async def _stop_main_services(self) -> None:
        """Run the unchanged AgentServer shutdown before optional PersonalContext cleanup."""
        global _startup_warmup_task

        async def _cancel_warmup_task(task: asyncio.Task[None] | None, label: str) -> None:
            if task is None or task.done():
                return
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("[AgentWebSocketServer] %s cancel failed: %s", label, exc)

        # 先取消后台预热, 避免在 server 关闭后仍在后台跑.
        warmup = _startup_warmup_task
        _startup_warmup_task = None
        await _cancel_warmup_task(warmup, "startup warmup")
        lag_task = self._loop_lag_task
        self._loop_lag_task = None
        await _cancel_warmup_task(lag_task, "loop lag monitor")
        had_server = self._server is not None
        if had_server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        from jiuwenswarm.server.runtime.session.kv_cache_product_hooks import (
            cancel_pending_tasks,
        )

        await cancel_pending_tasks()

        self._restore_runtime_push_host()
        if not is_enterprise():
            try:
                from jiuwenswarm.server.im.im_hosting.service import get_hosting_service

                await get_hosting_service().stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[AgentWebSocketServer] im hosting poll stop failed: %s", exc)
        if not had_server:
            return
        try:
            await self._jiuwenbox_runner.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[AgentWebSocketServer] jiuwenbox_runner.stop failed: %s", exc)
        logger.info("[AgentWebSocketServer] 已停止")

    # ---------- 连接处理 ----------

    async def _connection_handler(self, ws: Any) -> None:
        """处理单个 Gateway WebSocket 连接，同一连接可并发处理多个请求."""
        # 和客户端连接成功后, 触发agentserver启动成功的回调事件
        await self._trigger_agent_server_started_hook()

        remote = ws.remote_address
        logger.info("[AgentWebSocketServer] 新连接: %s", remote)

        send_lock = asyncio.Lock()
        # 每连接唯一订阅 id：短连接断开只清自己，不得抹掉仍存活的长连接（旧固定
        # gateway-ws 单槽曾导致 send_push 永久失败、chat.file 到不了前台）。
        # drop_on_stall=False：与 _GatewayWSPushSink「发送失败/变慢都不注销」一致。
        push_subscriber_id = make_ws_push_subscriber_id(ws)
        # Match the HTTP push-consumer contract. Relay/Node chat subscribers
        # cannot answer ACP/A2A reverse RPCs merely because they use WebSocket.
        request_headers = getattr(ws, "request_headers", None)
        if request_headers is None:
            request_headers = getattr(getattr(ws, "request", None), "headers", None)
        get_push_registry().register(
            push_subscriber_id,
            _GatewayWSPushSink(ws, send_lock),
            drop_on_stall=False,
            reverse_rpc_capable=(
                get_header_value(request_headers, "X-Jiuwen-Push-Consumer") == "gateway"
            ),
        )

        # 触发身份获取（写入当前连接 context；无 provider 时身份为 null，连接继续）。
        # 其后 asyncio.create_task(_handle_message) 自动 copy_context，身份传播到各请求任务。
        identity_token = None
        try:
            from jiuwenswarm.extensions.identity_provider import IdentityStore
            identity_token = await IdentityStore.fetch_and_store()
            identity = IdentityStore.get_identity()
            if identity is not None:
                logger.info(
                    "[AgentWebSocketServer] 身份信息已获取: user_id=%s domain_id=%s app_id=%s",
                    identity.user_id, identity.domain_id, identity.app_id,
                    extra={"user_visible": "progress"},
                )
            else:
                logger.debug("[AgentWebSocketServer] 未获取到身份信息（无 provider 或获取失败）",
                             extra={"user_visible": "progress"})
        except Exception as e:
            logger.warning("[AgentWebSocketServer] 身份获取异常: %s", e, extra={"user_visible": "progress"})

        # 发送 connection.ack 事件，通知 Gateway 服务端已就绪
        try:
            ack_frame = {
                "type": "event",
                "event": "connection.ack",
                "payload": {"status": "ready"},
            }
            await send_wire_payload(ws, ack_frame)
            logger.info("[AgentWebSocketServer] 已发送 connection.ack: %s", remote)
        except Exception as e:
            logger.warning("[AgentWebSocketServer] 发送 connection.ack 失败: %s", e)

        tasks: set[asyncio.Task] = set()

        try:
            async for raw in ws:
                async def _run(raw=raw):
                    try:
                        await self._handle_message(ws, raw, send_lock)
                    except WebSocketConnectionClosed as e:
                        logger.info("[AgentWebSocketServer] 连接已关闭: %s", e)
                    except Exception:
                        logger.exception("[AgentWebSocketServer] 处理消息失败")

                task = asyncio.create_task(_run())
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
            if identity_token is not None:
                try:
                    from jiuwenswarm.extensions.identity_provider import IdentityStore
                    IdentityStore.clear(identity_token)
                except Exception:
                    logger.exception("[AgentWebSocketServer] identity clear failed")
            # 只注销本连接的唯一订阅 id，保留其他仍存活 WS 的推送订阅。
            get_push_registry().unregister(push_subscriber_id)
            self._clear_ws_acp_client_capabilities(ws)
            connection_tasks = list(tasks)
            for task in connection_tasks:
                if not task.done():
                    task.cancel()
            # Gateway 进程退出/端口关闭时，必须先取消各 session 内流式生产者（SessionManager）
            # 并中止 DeepAgent 内层循环；否则仅等待 _handle_message 任务结束会一直阻塞到任务自然完成。
            try:
                await self._agent_manager.cancel_all_inflight_work(
                    reason=f"[gateway ws closed {remote}] ",
                )
            except Exception:
                logger.exception("[AgentWebSocketServer] cancel_all_inflight_work failed")
            # Stop scheduler on server shutdown
            try:
                await self._stop_scheduler()
            except Exception:
                logger.exception("[AgentWebSocketServer] scheduler stop failed")
            try:
                from jiuwenswarm.agents.harness.team import cancel_all_team_stream_tasks_across_managers

                await cancel_all_team_stream_tasks_across_managers(
                    reason=f"[gateway ws closed {remote}] ",
                )
            except Exception:
                logger.exception("[AgentWebSocketServer] team stream cancel failed")
            if connection_tasks:
                await asyncio.gather(*connection_tasks, return_exceptions=True)
            self._session_stream_tasks.clear()

    async def _handle_message(self, ws: Any, raw: str | bytes, send_lock: asyncio.Lock) -> None:
        """解析一条 JSON 请求并交给汇合点处理。

        解析规则在 ``server/wire_parse.py``（两个传输共用）
        """
        result = parse_inbound(raw)
        if not result.ok:
            try:
                async with send_lock:
                    await send_wire_payload(ws, result.error_wire)
            except WebSocketConnectionClosed as send_exc:
                logger.info(
                    "[AgentWebSocketServer] WebSocket 已关闭，解析错误未发送: %s",
                    format_ws_diagnostics(
                        result.log_context or {},
                        describe_ws_peer(ws),
                        describe_ws_exception(send_exc),
                    ),
                )
            return

        try:
            request = result.request
            # PersonalContext 请求在通用流水线之前拦截：其 wire 语义（host 持有、
            # runtime 开关联动）与表驱动分发无关，直接交给独立 handler。
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
            if request.req_method == ReqMethod.MCP_LIST:
                await self._handle_mcp_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_SHOW:
                await self._handle_mcp_show(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_CONNECT:
                await self._handle_mcp_connect(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_WAIT_AUTH:
                await self._handle_mcp_wait_auth(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_DISCONNECT:
                await self._handle_mcp_disconnect(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_REGISTER_CUSTOM:
                await self._handle_mcp_register_custom(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_DELETE_CUSTOM:
                await self._handle_mcp_delete_custom(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_SAVE_CREDENTIALS:
                await self._handle_mcp_save_credentials(ws, request, send_lock)
                return
            # 汇合点在server/pipeline.py
            ctx = RequestContext(
                request=request,
                sink=WSSink(ws, send_lock),
                connection_id=str(id(ws)),
                services=AgentServerServices(self),
            )
            await dispatch_parsed_request(ctx, request, peer=ws)

            if request.req_method in get_sandbox_config_req_methods():
                logger.info(
                    f"[AgentWebSocketServer] 处理 sandbox.config: request_id={request.request_id}",
                    extra={"user_visible": "progress"},
                )
                await self._handle_sandbox_config(ws, request, send_lock)
        except WebSocketConnectionClosed:
            # 连接关闭：维持原语义，交由 _run 记录，不再尝试回写
            raise
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

    async def _handle_sandbox_config(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """处理 sandbox.* 配置 E2A 请求 (officeAce 经 WS 控制沙箱开关/启动方式/文件/网络)."""
        from jiuwenswarm.server.sandbox_config_rpc import dispatch_sandbox_config_request
        from jiuwenswarm.common.schema.message import ReqMethod

        resp = dispatch_sandbox_config_request(request)
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

        # 沙箱开关变更后必须 reload_agent_config, 否则同 session 已活着的
        # ReActAgent 仍持有旧 SANDBOX sysop id (已被 teardown 从 resource_mgr
        # 移除), 后续工具调用拿到 None sysop 表现为无权限.
        # 新 session 不受影响: 它懒创建时直接读新 enabled 值造 LOCAL sysop.
        if resp.ok and request.req_method == ReqMethod.SANDBOX_ENABLED_SET:
            try:
                config_base = get_config()
                await self._agent_manager.reload_agents_config(config_base, None)
                logger.info(
                    "[AgentWebSocketServer] sandbox.enabled.set 后触发 reload_agents_config, "
                    "同 session ReActAgent 将切到新 sysop",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[AgentWebSocketServer] sandbox.enabled.set 后 reload_agents_config 失败: %s",
                    exc,
                )

    @staticmethod
    async def _trigger_before_ws_server_start_hook() -> None:
        """在首次启动之前触发扩展；未初始化 ExtensionRegistry 时跳过。"""
        from jiuwenswarm.extensions.registry import ExtensionRegistry
        from jiuwenswarm.common.utils import get_agent_skills_dir

        try:
            ctx = AgentWsServerStartHookContext(skills_dir=str(get_agent_skills_dir()))
            await ExtensionRegistry.get_instance().trigger(
                AgentServerHookEvents.BEFORE_WS_SERVER_START, ctx
            )
        except RuntimeError:
            logger.debug(
                "[AgentWebSocketServer] ExtensionRegistry unavailable, skip BEFORE_WS_SERVER_START"
            )

    @staticmethod
    async def _trigger_agent_server_started_hook() -> None:
        """在agentserver启动成功触发扩展；未初始化 ExtensionRegistry 时跳过。"""
        from jiuwenswarm.extensions.registry import ExtensionRegistry
        from jiuwenswarm.common.utils import get_agent_skills_dir

        try:
            registry = ExtensionRegistry.get_instance()
        except RuntimeError:
            return

        ctx = AgentWsServerStartHookContext(skills_dir=str(get_agent_skills_dir()))
        await registry.trigger(AgentServerHookEvents.AGENT_SERVER_STARTED, ctx)

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

    async def _prepare_session_switch_owner(
        self,
        *,
        channel_id: str,
        target_session_id: str,
        previous_session_id: str,
        params: dict[str, Any],
        reason: str,
    ) -> tuple[bool, str, Any, Any, Any]:
        """Resolve switch context and run product-owner prepare (team switch).

        Returns:
            ``(target_is_team, resolved_mode, context, team_manager, dispatch_signals)``.
            ``dispatch_signals`` may be ``None`` when KVC hooks are unavailable.
        """
        target_is_team = is_team_params(params)
        _, _, resolved_mode = resolve_agent_request_mode(
            params.get("mode", "agent.plan")
        )
        context = None
        dispatch_signals = None
        try:
            from jiuwenswarm.server.runtime.session.kv_cache_product_hooks import (
                dispatch_session_switch_signals,
                resolve_session_switch_context,
            )

            context = resolve_session_switch_context(
                target_session_id=target_session_id,
                previous_session_id=previous_session_id,
                params=params,
            )
            target_is_team = context.target_is_team
            resolved_mode = context.resolved_mode
            dispatch_signals = dispatch_session_switch_signals
        except Exception as exc:
            logger.warning(
                "[AgentWebSocketServer] session switch KVC context unavailable; "
                "preserving product lifecycle: target_session_id=%s error=%s",
                target_session_id,
                exc,
            )

        previous_is_team = bool(context and context.previous_is_team)
        team_manager = None
        if target_is_team or previous_is_team:
            from jiuwenswarm.agents.harness.team import get_team_manager

            team_manager = get_team_manager(channel_id)
            await team_manager.prepare_session_switch(
                target_session_id,
                previous_session_id=(
                    previous_session_id if previous_is_team else None
                ),
                reason=reason,
            )
        return target_is_team, resolved_mode, context, team_manager, dispatch_signals

    async def _dispatch_session_switch_kvc(
        self,
        *,
        channel_id: str,
        target_session_id: str,
        previous_session_id: str,
        reason: str,
        context: Any,
        team_manager: Any,
        dispatch_signals: Any,
    ) -> None:
        """Optional KVC signals after the product owner has prepared the switch."""
        if context is None or dispatch_signals is None:
            return
        await dispatch_signals(
            context=context,
            agent_manager=self._agent_manager,
            channel_id=channel_id,
            team_manager=team_manager,
            target_session_id=target_session_id,
            previous_session_id=previous_session_id,
            reason=reason,
        )

    async def _ensure_persistent_checkpointer_response(
        self,
        request: AgentRequest,
    ) -> AgentResponse | None:
        """Return an error response when persistent checkpoint storage is unavailable."""
        try:
            await ensure_interface_deep_and_checkpointer()
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

    @staticmethod
    def _team_binding_payload(binding: Any) -> dict[str, Any]:
        if hasattr(binding, "to_dict"):
            return binding.to_dict()
        if isinstance(binding, dict):
            return dict(binding)
        return {}

    @staticmethod
    def _create_team_binding_from_template(
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
        template_ids = {
            str(item.get("template_id") or "")
            for item in list_team_template_summaries(config_base)
        }
        if template_id not in template_ids:
            raise TeamBindingStoreError("template_id not found", code="NOT_FOUND")

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
                binding = cls._create_team_binding_from_template(
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
        """Create and bind a team before the first team chat without consuming its query."""
        if request.req_method != ReqMethod.CHAT_SEND:
            return None

        params = request.params if isinstance(request.params, dict) else {}
        if not isinstance(request.params, dict):
            request.params = params
        session_id = str(request.session_id or params.get("session_id") or "").strip()
        if not session_id:
            return None

        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
            update_session_metadata,
        )

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

        query = _request_query_text(request)
        if not query:
            return None

        async with self._session_team_binding_lock(session_id):
            metadata = get_session_metadata(session_id, cache_bust=True)
            existing_team_name = str(metadata.get("team_name") or "").strip()
            if existing_team_name:
                params.setdefault("team_name", existing_team_name)
                template_id = str(metadata.get("team_template_id") or "").strip()
                if template_id:
                    params.setdefault("team_template_id", template_id)
                return existing_team_name

            from jiuwenswarm.server.runtime.team_binding_store import get_team_binding_store
            from jiuwenswarm.server.runtime.team_entity_store import get_team_entity_store

            binding, _template = await self._create_generated_team_binding(
                description=query,
                config_base=get_config(),
            )
            binding_store = get_team_binding_store()
            entity_store = get_team_entity_store()
            try:
                binding = binding_store.bind_session(
                    team_name=binding.team_name,
                    session_id=session_id,
                )
                update_session_metadata(
                    session_id=session_id,
                    channel_id=request.channel_id or None,
                    user_content=query,
                    mode=canonical_mode,
                    team_name=binding.team_name,
                    team_template_id=binding.template_id,
                    touch_last_message_at=False,
                    sync_write=True,
                )
            except Exception:
                cleanup_errors: list[str] = []
                cleanup_steps = (
                    lambda: binding_store.unbind_session(
                        team_name=binding.team_name,
                        session_id=session_id,
                    ),
                    lambda: binding_store.delete(binding.team_name),
                    lambda: entity_store.delete_team_directory(binding.team_name),
                )
                for cleanup_step in cleanup_steps:
                    try:
                        cleanup_step()
                    except Exception as cleanup_exc:  # noqa: BLE001
                        cleanup_errors.append(str(cleanup_exc))
                if cleanup_errors:
                    logger.warning(
                        "[AgentWebSocketServer] auto team binding rollback incomplete: "
                        "session_id=%s team_name=%s errors=%s",
                        session_id,
                        binding.team_name,
                        cleanup_errors,
                    )
                raise

            params["team_name"] = binding.team_name
            params["team_template_id"] = binding.template_id
            request.metadata = dict(request.metadata or {})
            request.metadata["team_name"] = binding.team_name
            request.metadata["team_template_id"] = binding.template_id
            logger.info(
                "[AgentWebSocketServer] auto-created and bound team before chat: "
                "session_id=%s team_name=%s template_id=%s",
                session_id,
                binding.team_name,
                binding.template_id,
            )
            return binding

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
        from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

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
        from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStoreError
        from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStoreError

        params = request.params if isinstance(request.params, dict) else {}
        team_name = str(params.get("team_name") or "")
        template_id = str(params.get("template_id") or "").strip()
        config_base = get_config()
        try:
            binding = self._create_team_binding_from_template(
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
        except (TeamBindingStoreError, TeamEntityStoreError) as exc:
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
        except (TeamBindingStoreError, TeamEntityStoreError) as exc:
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
        except (TeamBindingStoreError, TeamEntityStoreError) as exc:
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
        """Delete a team and all team sessions that persist that team."""
        from openjiuwen.core.runner import Runner
        from jiuwenswarm.agents.harness.team import (
            stop_team_session_runtime_across_managers,
        )
        from jiuwenswarm.server.runtime.team_binding_store import (
            TeamBindingStoreError,
            get_team_binding_store,
        )
        from jiuwenswarm.server.runtime.team_entity_store import (
            TeamEntityStoreError,
            get_team_entity_store,
        )

        def delete_team_directory_best_effort(team_name: str) -> None:
            try:
                entity_store.delete_team_directory(team_name)
            except TeamEntityStoreError as exc:
                logger.warning(
                    "[AgentWebSocketServer] failed to delete local team directory; "
                    "continuing team delete: team_name=%s code=%s error=%s",
                    team_name,
                    getattr(exc, "code", "DELETE_FAILED"),
                    exc,
                )

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
                binding_store = get_team_binding_store()
                entity_store = get_team_entity_store()
                team_session_ids = await self._find_team_session_ids(team_name)
                if not team_session_ids:
                    binding = binding_store.get(team_name)
                    if binding is None and not entity_store.exists(team_name):
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=False,
                            payload={"error": "team not found", "code": "NOT_FOUND"},
                            metadata=request.metadata,
                        )
                    else:
                        delete_team_directory_best_effort(team_name)
                        binding_store.delete(team_name)
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=True,
                            payload={
                                "team_name": team_name,
                                "session_ids": [],
                                "deleted": True,
                            },
                            metadata=request.metadata,
                        )
                else:
                    checkpoint_resp = await self._ensure_persistent_checkpointer_response(request)
                    if checkpoint_resp is not None:
                        resp = checkpoint_resp
                    else:
                        from jiuwenswarm.agents.harness.team import (
                            kv_cache_hooks as team_kv_cache_hooks,
                        )

                        for team_session_id in team_session_ids:
                            await team_kv_cache_hooks.stop_runtime_before_terminal_delete(
                                stop_team_session_runtime_across_managers,
                                session_id=team_session_id,
                                reason="team.delete: ",
                            )

                        runtime_deleted = await Runner.delete_agent_team(
                            team_name=team_name,
                            session_ids=team_session_ids,
                            force=True,
                        )
                        if not runtime_deleted:
                            resp = AgentResponse(
                                request_id=request.request_id,
                                channel_id=request.channel_id,
                                ok=False,
                                payload={
                                    "error": "agent team runtime cleanup failed",
                                    "code": "DELETE_FAILED",
                                    "team_name": team_name,
                                    "deleted": False,
                                },
                                metadata=request.metadata,
                            )
                        else:
                            failed_session_ids: list[str] = []
                            for team_session_id in team_session_ids:
                                session_dir = get_agent_sessions_dir() / team_session_id
                                if session_dir.exists():
                                    try:
                                        shutil.rmtree(session_dir)
                                    except Exception as exc:
                                        logger.warning(
                                            "[AgentWebSocketServer] failed to delete local team session dir: "
                                            "session_id=%s error=%s",
                                            team_session_id,
                                            exc,
                                        )
                                        failed_session_ids.append(team_session_id)
                                        continue
                                remove_session_metadata_cache(team_session_id)

                            if failed_session_ids:
                                resp = AgentResponse(
                                    request_id=request.request_id,
                                    channel_id=request.channel_id,
                                    ok=False,
                                    payload={
                                        "error": "failed to delete local team session directories",
                                        "code": "DELETE_FAILED",
                                        "team_name": team_name,
                                        "failed_session_ids": failed_session_ids,
                                        "deleted": False,
                                    },
                                    metadata=request.metadata,
                                )
                            else:
                                # agent-core normally removes team_home; retry here because it logs and
                                # suppresses filesystem cleanup failures.
                                delete_team_directory_best_effort(team_name)
                                binding_store.delete(team_name)
                                resp = AgentResponse(
                                    request_id=request.request_id,
                                    channel_id=request.channel_id,
                                    ok=True,
                                    payload={
                                        "team_name": team_name,
                                        "session_ids": team_session_ids,
                                        "deleted": True,
                                    },
                                    metadata=request.metadata,
                                )
            except (TeamBindingStoreError, TeamEntityStoreError) as exc:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": str(exc), "code": getattr(exc, "code", "DELETE_FAILED")},
                    metadata=request.metadata,
                )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_session_delete(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """Delete a single session and its recoverable runtime state."""
        from openjiuwen.core.runner import Runner
        from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata
        from jiuwenswarm.agents.harness.team import get_team_manager

        params = request.params if isinstance(request.params, dict) else {}
        target = str(params.get("session_id") or "").strip()
        if not target:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "session_id is required", "code": "BAD_REQUEST"},
                metadata=request.metadata,
            )
        else:
            from jiuwenswarm.server.runtime.session.session_history import resolve_session_dir

            session_dir, invalid_reason = resolve_session_dir(
                target, sessions_root=get_agent_sessions_dir()
            )
            if session_dir is None:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": invalid_reason or "invalid session_id", "code": "BAD_REQUEST"},
                    metadata=request.metadata,
                )
            elif not session_dir.exists():
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": "session not found", "code": "NOT_FOUND"},
                    metadata=request.metadata,
                )
            elif not session_dir.is_dir():
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": "session is not a directory", "code": "BAD_REQUEST"},
                    metadata=request.metadata,
                )
            else:
                checkpoint_resp = await self._ensure_persistent_checkpointer_response(request)
                if checkpoint_resp is not None:
                    resp = checkpoint_resp
                else:
                    metadata = get_session_metadata(target)
                    is_team_session = self._is_team_metadata_mode(metadata)
                    team_name = str(metadata.get("team_name") or "").strip()
                    channel_id = str(metadata.get("channel_id") or request.channel_id or "").strip() or None
                    if not is_team_session:
                        from jiuwenswarm.server.runtime.session.kv_cache_product_hooks import (
                            evict_plan_session,
                        )

                        await evict_plan_session(
                            session_id=target,
                            agent_manager=self._agent_manager,
                            channel_id=channel_id,
                        )
                    try:
                        if is_team_session:
                            team_manager = get_team_manager(channel_id)
                            deleted = await team_manager.delete_session_runtime(
                                target,
                                reason="session.delete: ",
                            )
                        else:
                            await Runner.release(target)
                            deleted = True
                    except Exception as exc:
                        logger.warning(
                            "[AgentWebSocketServer] session.delete runtime cleanup failed: session_id=%s error=%s",
                            target,
                            exc,
                        )
                        deleted = False

                    if not deleted:
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=False,
                            payload={"error": "session runtime cleanup failed", "code": "DELETE_FAILED"},
                            metadata=request.metadata,
                        )
                    else:
                        shutil.rmtree(session_dir)
                        _plan_exited_sessions.discard(target)
                        _plan_active_sessions.discard(target)
                        remove_session_metadata_cache(target)
                        if is_team_session:
                            try:
                                from jiuwenswarm.server.runtime.team_binding_store import get_team_binding_store

                                get_team_binding_store().unbind_session(
                                    team_name=team_name or None,
                                    session_id=target,
                                )
                            except Exception as exc:  # noqa: BLE001
                                logger.warning(
                                    "[AgentWebSocketServer] failed to unbind deleted team session: "
                                    "session_id=%s team_name=%s error=%s",
                                    target,
                                    team_name,
                                    exc,
                                )
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=True,
                            payload={"session_id": target},
                            metadata=request.metadata,
                        )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _resolve_rewind_agent(
        self,
        channel_id: str,
        session_id: str | None = None,
    ) -> tuple[Any, Any] | None:
        """Return (deep_agent, react_agent) for rewind context rebuild.

        Prefer the live **session-scoped** DeepAgent used by chat.send.
        Root ``agent.get_instance()`` is a separate DeepAgent whose
        context_engine / ``_interaction_session`` are not the ones the next
        user turn will read — updating them leaves the model still seeing
        rewound turns.
        """
        sid = str(session_id or "").strip()
        agent = (
            self._agent_manager.get_agent_for_session_nowait(
                channel_id=channel_id or "default",
                session_id=sid,
            )
            if sid
            else None
        )
        if agent is None:
            agent = self._agent_manager.get_agent_nowait(
                channel_id=channel_id or "default"
            )
        if agent is None:
            return None

        deep_agent = None
        if sid:
            adapter = self._resolve_adapter(agent)
            if adapter is not None:
                # Already session-scoped (rare): use it directly.
                if getattr(adapter, "_is_session_scoped_adapter", False):
                    deep_agent = getattr(adapter, "_instance", None)
                else:
                    get_cached = getattr(adapter, "_get_cached_session_adapter", None)
                    if callable(get_cached):
                        session_adapter = get_cached(sid)
                        if session_adapter is not None:
                            deep_agent = getattr(session_adapter, "_instance", None)
                            if deep_agent is None:
                                logger.warning(
                                    "[AgentWS] rewind: cached session adapter has no "
                                    "instance for session_id=%s",
                                    sid,
                                )

        if deep_agent is None:
            # Fallback: no live session adapter yet (e.g. rewind before any chat
            # on this process). Checkpointer-only rebuild still helps cold start,
            # so build the root DeepAgent here if it has not been needed yet.
            deep_agent = await agent.ensure_instance()
            if deep_agent is not None and sid:
                logger.info(
                    "[AgentWS] rewind: no session-scoped DeepAgent for %s; "
                    "falling back to root instance",
                    sid,
                )

        if deep_agent is None:
            return None
        react_agent = deep_agent.react_agent
        if react_agent is None:
            return None
        return (deep_agent, react_agent)

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

    async def _handle_session_rewind_full(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock,
        restore_files: bool = False,
        compact: bool = False,
    ) -> None:
        """Full rewind: truncate history.json + context_engine + update checkpointer."""
        from jiuwenswarm.agents.harness.common.session_ops_service import (
            rewind_session,
            rewind_session_context,
        )

        params = request.params if isinstance(request.params, dict) else {}
        target_sid = str(params.get("session_id") or request.session_id or "").strip()
        turn_index = params.get("turn_index")
        compact_summary = params.get("compact_summary") if compact else None
        direction = str(params.get("direction") or "from").strip() if compact else "from"
        summarized_count = int(params.get("summarized_count", 0) or 0) if compact else 0

        if not target_sid or turn_index is None:
            wire = AgentWebSocketServer._send_error_response(
                ws, request, send_lock,
                "session_id and turn_index required", "BAD_REQUEST",
            )
            async with send_lock:
                await send_wire_payload(ws, wire)
            return

        try:
            turn_index = int(turn_index)
        except (ValueError, TypeError):
            wire = AgentWebSocketServer._send_error_response(
                ws, request, send_lock,
                "turn_index must be integer", "BAD_REQUEST",
            )
            async with send_lock:
                await send_wire_payload(ws, wire)
            return

        try:
            # Step 1: Optionally restore files first
            restore_result: dict[str, Any] = {}
            if restore_files:
                from jiuwenswarm.agents.harness.common.session_ops_service import restore_session_files
                restore_result = restore_session_files(session_id=target_sid, turn_index=turn_index)

            # Step 2: Truncate history.json (local file operation)
            # "up_to" direction: keep messages from turn_index onward, summarize the prefix.
            # compact_partial_session handles this correctly (rewind_session only supports
            # the "from" direction — keeping the prefix and truncating the tail).
            if compact and direction == "up_to":
                from jiuwenswarm.agents.harness.common.session_ops_service import compact_partial_session
                rewind_result = compact_partial_session(
                    session_id=target_sid,
                    turn_index=turn_index,
                    direction="up_to",
                    llm_summary=compact_summary,
                )
            else:
                rewind_result = rewind_session(session_id=target_sid, turn_index=turn_index)

            # Step 3: Truncate context_engine in-place + persist to checkpointer.
            # rewind_session_context reads the already-truncated history.json and
            # converts ALL records to context messages, so it naturally produces the
            # correct result for both "from" and "up_to" directions.
            context_ok = False
            pair = await self._resolve_rewind_agent(
                request.channel_id or "default",
                session_id=target_sid,
            )
            if pair is None:
                logger.warning(
                    "[AgentWS] session.rewind: no agent for context rebuild "
                    "(session_id=%s channel=%s); history truncated but model "
                    "context may still contain rewound turns",
                    target_sid,
                    request.channel_id,
                )
            else:
                deep_agent, _react_agent = pair
                try:
                    context_ok = await rewind_session_context(
                        deep_agent=deep_agent,
                        session_id=target_sid,
                        turn_index=turn_index,
                    )
                except Exception as exc:
                    logger.warning(
                        "[AgentWS] session.rewind context truncation failed: %s", exc,
                    )
                if not context_ok:
                    logger.warning(
                        "[AgentWS] session.rewind: history truncated but "
                        "rewind_context=false (session_id=%s)",
                        target_sid,
                    )

            payload = {**rewind_result, "rewind_context": context_ok}
            if restore_files:
                payload["restored_files"] = restore_result.get("restored_files", [])
                payload["deleted_files"] = restore_result.get("deleted_files", [])
                payload["restore_errors"] = restore_result.get("errors", [])

            # Step 4: For compact mode, append boundary + rewind_summary + compact_summary records.
            # compact_partial_session already writes these for "up_to", so only append for "from".
            if compact and direction == "from":
                import uuid as _uuid
                import time as _time
                from jiuwenswarm.server.runtime.session.session_history import append_history_record
                request_id = str(_uuid.uuid4())
                now = _time.time()

                short_text = (
                    f"Summarized {summarized_count} messages from this point."
                    if direction == "from"
                    else f"Summarized {summarized_count} messages up to this point."
                )

                append_history_record(
                    session_id=target_sid,
                    request_id=request_id,
                    channel_id=request.channel_id or "tui",
                    role="assistant",
                    event_type="context.compact_boundary",
                    content="Conversation compacted",
                    timestamp=now,
                    extra={
                        "compact_metadata": {
                            "trigger": "manual_rewind",
                            "direction": direction,
                            "turn_index": turn_index,
                            "summarized_messages": summarized_count,
                        },
                    },
                )

                append_history_record(
                    session_id=target_sid,
                    request_id=request_id,
                    channel_id=request.channel_id or "tui",
                    role="assistant",
                    event_type="context.rewind_summary",
                    content=short_text,
                    timestamp=now + 0.001,
                    extra={
                        "compact_metadata": {
                            "trigger": "manual_rewind",
                            "direction": direction,
                            "turn_index": turn_index,
                            "summarized_messages": summarized_count,
                        },
                        "is_compact_summary": True,
                    },
                )

                if isinstance(compact_summary, str) and compact_summary.strip():
                    append_history_record(
                        session_id=target_sid,
                        request_id=request_id,
                        channel_id=request.channel_id or "tui",
                        role="assistant",
                        event_type="context.compact_summary",
                        content=compact_summary.strip(),
                        timestamp=now + 0.002,
                        extra={
                            "compact_metadata": {
                                "trigger": "manual_rewind",
                                "direction": direction,
                                "turn_index": turn_index,
                                "summarized_messages": summarized_count,
                            },
                            "is_compact_summary": True,
                            "transcript_only": True,
                        },
                    )

                payload["summarized_messages"] = summarized_count

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
                metadata=request.metadata,
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "BAD_REQUEST"},
                metadata=request.metadata,
            )
        except Exception as exc:
            logger.exception("[AgentWS] session.rewind failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc)},
                metadata=request.metadata,
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_session_rewind_context(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Truncate history.json + in-memory context_engine for a session."""
        from jiuwenswarm.agents.harness.common.session_ops_service import (
            rewind_session,
            rewind_session_context,
        )

        params = request.params if isinstance(request.params, dict) else {}
        target_sid = str(params.get("session_id") or request.session_id or "").strip()
        turn_index = params.get("turn_index")

        if not target_sid or turn_index is None:
            wire = AgentWebSocketServer._send_error_response(
                ws, request, send_lock,
                "session_id and turn_index required", "BAD_REQUEST",
            )
            async with send_lock:
                await send_wire_payload(ws, wire)
            return

        try:
            turn_index = int(turn_index)
        except (ValueError, TypeError):
            wire = AgentWebSocketServer._send_error_response(
                ws, request, send_lock,
                "turn_index must be integer", "BAD_REQUEST",
            )
            async with send_lock:
                await send_wire_payload(ws, wire)
            return

        pair = await self._resolve_rewind_agent(
            request.channel_id or "default",
            session_id=target_sid,
        )
        if pair is None:
            wire = AgentWebSocketServer._send_error_response(
                ws, request, send_lock, "no agent instance available",
            )
            async with send_lock:
                await send_wire_payload(ws, wire)
            return
        deep_agent, _react_agent = pair

        try:
            # Truncate history.json first so rewind_session_context reads the
            # correct truncated state (the new implementation rebuilds context
            # from history.json on disk).
            rewind_result = rewind_session(session_id=target_sid, turn_index=turn_index)
            context_ok = await rewind_session_context(
                deep_agent=deep_agent,
                session_id=target_sid,
                turn_index=turn_index,
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={**rewind_result, "rewind_context": context_ok},
                metadata=request.metadata,
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "BAD_REQUEST"},
                metadata=request.metadata,
            )
        except Exception as exc:
            logger.exception("[AgentWS] session.rewind_context failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc)},
                metadata=request.metadata,
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_permissions_config(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """处理 permissions.* E2A 请求（与 Web ``register_method`` 同名 method）。"""
        from jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc import \
            dispatch_permissions_config_request

        resp = dispatch_permissions_config_request(request)

        # After any successful mutation (delete / update / set / create),
        # reload agent config so the PermissionInterruptRail picks up the
        # change immediately instead of waiting for the next tool call's
        # get_permissions_snapshot refresh.
        read_only_methods = {
            ReqMethod.PERMISSIONS_TOOLS_GET,
            ReqMethod.PERMISSIONS_RULES_GET,
            ReqMethod.PERMISSIONS_APPROVAL_OVERRIDES_GET,
        }
        if resp.ok and request.req_method not in read_only_methods:
            # 后台异步重载: 不阻塞权限 RPC 回包(避免 reload 慢导致 AgentServer
            # request timed out)。reload_agents_config 内部有 _reload_lock 串行化
            # + fingerprint 去重,fire-and-forget 安全。
            reload_task = asyncio.create_task(
                self._agent_manager.reload_agents_config(get_config(), None)
            )
            _background_permission_reload_tasks.add(reload_task)
            reload_task.add_done_callback(_background_permission_reload_tasks.discard)
            reload_task.add_done_callback(_log_permission_reload_failure)

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_history_get(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        params = request.params if isinstance(request.params, dict) else {}
        session_id = params.get("session_id")
        page_idx = params.get("page_idx")
        data = self.get_conversation_history(session_id=session_id, page_idx=page_idx)
        if data is None:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "invalid page_idx or session history not found"},
            )
        else:
            # 非流式整页塞进单个 wire 帧（AgentResponse.payload=data），没法像流式那样
            # 按 channel_id 分片流——这里所有通道统一走 _sanitize_history_record_for_wire
            # 把每条 record 裁剪到 16KB + 64KB collapse 之内，保证 wire 帧有界。
            # 流式路径在 _handle_history_get_stream 里按 channel_id 分流（web 走 split，
            # 其他走 sanitize），与此处无关。
            if isinstance(data.get("messages"), list):
                data["messages"] = [
                    _sanitize_history_record_for_wire(record)
                    for record in data["messages"]
                    if isinstance(record, dict)
                ]
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=data,
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)


    async def _handle_proactive_tick(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """Handle proactive.tick request from CronScheduler.

        This is called by Gateway's CronScheduler to trigger a recommendation tick.
        Respects cooldown and daily limits.
        """
        if self._proactive_engine is None:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "ProactiveEngine not initialized"},
            )
        else:
            try:
                # Extract target_channel from params
                params = request.params or {}
                target_channel = params.get("target_channel")

                # Run the tick (respects cooldown and daily limits)
                success = await self._proactive_engine.tick_now(target_channel=target_channel)

                status = "tick_executed" if success else "no_recommendation"
                last_tick = self._proactive_engine.last_tick_at
                if last_tick > 0:
                    status = f"{status} (last_tick_at={last_tick:.0f})"

                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"status": status, "success": success},
                )
            except Exception as e:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": str(e)},
                )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_team_snapshot(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from jiuwenswarm.agents.harness.team import get_team_manager
        from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import (
            TeamMonitorHandler,
        )
        from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(params.get("session_id") or request.session_id or "").strip()
        channel_id = request.channel_id or "web"
        empty_payload = {"members": [], "tasks": [], "team_id": None}

        team_manager = get_team_manager(channel_id)
        monitor_handler = team_manager.get_monitor_handler(session_id) if session_id else None

        snapshot: dict[str, Any] | None = None
        source = "empty"
        if monitor_handler is not None and monitor_handler.is_running:
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

        # History restore often hits this RPC after the monitor has stopped, OR
        # while a live handler is still registered but already returns a truthy
        # empty board ({tasks: [], members: [], team_id: ...}). `if not snapshot`
        # alone would skip DB in that case and leave the frontend with no
        # title/content. Fall back whenever live has no tasks.
        needs_db = snapshot is None or not _snapshot_tasks(snapshot)
        if needs_db and session_id:
            team_name = str(params.get("team_name") or "").strip()
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

        payload = snapshot or empty_payload
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

    async def _handle_command_workflows(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """Handle command.workflows RPC — list summaries or get one workflow detail."""
        from jiuwenswarm.agents.harness.team import get_team_manager

        session_id = request.session_id or ""
        channel_id = request.channel_id or "web"
        params = request.params if isinstance(request.params, dict) else {}
        action = str(params.get("action") or "list").strip().lower()
        workflow_id = params.get("workflow_id") or params.get("workflow_run_id")
        wf_id_log = workflow_id.strip() if isinstance(workflow_id, str) else workflow_id

        logger.info(
            "[WF_DBG] command.workflows req channel_id=%s session_id=%s request_id=%s action=%s workflow_id=%s",
            channel_id,
            session_id,
            request.request_id,
            action,
            wf_id_log,
        )

        team_manager = get_team_manager(channel_id)
        workflow_handler = team_manager.get_workflow_handler(session_id)
        source = "live" if workflow_handler is not None else "checkpoint"
        detail_raw_bytes: int | None = None

        if workflow_handler is None:
            # No live handler (runtime not active / torn down by cancel-stop).
            # The snapshot is a read-only pull and must not depend on runtime
            # liveness — fall back to the persisted checkpoint so historical /
            # terminal workflow runs remain queryable after the team session
            # is cancelled or stopped.
            try:
                from jiuwenswarm.server.runtime.agent_adapter.team_helpers import (
                    restore_workflow_runs,
                )

                restored = restore_workflow_runs(session_id)
                workflows = (
                    [run.to_workflow_run_dict() for run in restored.values()]
                    if restored
                    else []
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[WF_DBG] command.workflows checkpoint_restore_failed session_id=%s error=%s",
                    session_id,
                    exc,
                )
                workflows = []
        else:
            try:
                workflows = workflow_handler.get_workflow_snapshot()
            except Exception as e:
                logger.warning(
                    "[WF_DBG] command.workflows snapshot_failed session_id=%s error=%s",
                    session_id,
                    e,
                )
                workflows = []

        source_count = len(workflows)
        source_bytes = sum(_json_wire_size(item) for item in workflows if isinstance(item, dict))

        if action == "get":
            if not isinstance(workflow_id, str) or not workflow_id.strip():
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=channel_id,
                    ok=False,
                    payload={"error": "workflow_id is required for action=get"},
                )
            else:
                target_id = workflow_id.strip()
                match = next(
                    (item for item in workflows if isinstance(item, dict) and item.get("id") == target_id),
                    None,
                )
                if match is None:
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=channel_id,
                        ok=False,
                        payload={"error": f"workflow not found: {target_id}"},
                    )
                else:
                    detail_raw_bytes = _json_wire_size(match)
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=channel_id,
                        ok=True,
                        payload=_build_workflow_detail_payload(match, session_id=session_id),
                    )
        elif action == "get_human_prompt":
            agent_id = params.get("agent_id")
            correlation_id = params.get("correlation_id")
            agent_id_str = agent_id.strip() if isinstance(agent_id, str) and agent_id.strip() else None
            corr_id_str = (
                correlation_id.strip()
                if isinstance(correlation_id, str) and correlation_id.strip()
                else None
            )
            if not isinstance(workflow_id, str) or not workflow_id.strip():
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=channel_id,
                    ok=False,
                    payload={"error": "workflow_id is required for action=get_human_prompt"},
                )
            elif not agent_id_str and not corr_id_str:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=channel_id,
                    ok=False,
                    payload={"error": "agent_id or correlation_id is required for action=get_human_prompt"},
                )
            else:
                target_id = workflow_id.strip()
                match = next(
                    (item for item in workflows if isinstance(item, dict) and item.get("id") == target_id),
                    None,
                )
                if match is None:
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=channel_id,
                        ok=False,
                        payload={"error": f"workflow not found: {target_id}"},
                    )
                else:
                    prompt_payload = _build_workflow_human_prompt_payload(
                        match,
                        session_id=session_id,
                        agent_id=agent_id_str,
                        correlation_id=corr_id_str,
                    )
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=channel_id,
                        ok="error" not in prompt_payload,
                        payload=prompt_payload,
                    )
        else:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=channel_id,
                ok=True,
                payload=_build_workflow_list_payload(workflows, session_id=session_id),
            )

        payload = resp.payload if isinstance(resp.payload, dict) else {}
        payload_bytes = _json_wire_size(payload)
        truncated = bool(payload.get("truncated")) if isinstance(payload, dict) else False
        included = len(payload.get("workflows", [])) if payload.get("action") == "list" else None
        error = payload.get("error") if isinstance(payload, dict) and not resp.ok else None
        log_level = logging.WARNING if (not resp.ok or truncated) else logging.INFO
        if action == "list":
            logger.log(
                log_level,
                "[WF_DBG] command.workflows res ok=%s action=list source=%s count=%d source_bytes=%d "
                "payload_bytes=%d included=%d/%d truncated=%s error=%s",
                resp.ok,
                source,
                source_count,
                source_bytes,
                payload_bytes,
                included or 0,
                source_count,
                truncated,
                error,
            )
        else:
            prompt_len = None
            if action == "get_human_prompt" and isinstance(payload, dict):
                human_prompt = payload.get("human_prompt")
                if isinstance(human_prompt, str):
                    prompt_len = len(human_prompt.encode("utf-8"))
            logger.log(
                log_level,
                "[WF_DBG] command.workflows res ok=%s action=%s source=%s workflow_id=%s "
                "raw_bytes=%s payload_bytes=%d truncated=%s prompt_len=%s error=%s",
                resp.ok,
                action,
                source,
                wf_id_log,
                detail_raw_bytes,
                payload_bytes,
                truncated,
                prompt_len,
                error,
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

    async def _handle_history_get_stream(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        params = request.params if isinstance(request.params, dict) else {}
        session_id = params.get("session_id")
        page_idx = params.get("page_idx")
        data = self.get_conversation_history(session_id=session_id, page_idx=page_idx)
        if data is None:
            err_chunk = AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={
                    "event_type": "chat.error",
                    "error": "invalid page_idx or session history not found",
                },
                is_complete=True,
            )
            wire = encode_agent_chunk_for_wire(
                err_chunk,
                response_id=request.request_id,
                sequence=0,
            )
            async with send_lock:
                await send_wire_payload(ws, wire)
            return

        messages = data.get("messages", [])
        total_pages = data.get("total_pages")
        page = data.get("page_idx")
        sequence = 0
        # 仅 web 通道走分片流（前端 HistoryRecordReassembler 重组）。
        # 其他通道（tui/acp/...）不认 _part 字段，走旧 _sanitize_history_record_for_wire
        # 单帧 + collapse 路径，维持现状，零回归。
        use_split = request.channel_id == "web"
        if isinstance(messages, list):
            for item in messages:
                if use_split:
                    chunks_for_record = split_history_record_for_stream(item)
                else:
                    chunks_for_record = [_sanitize_history_record_for_wire(item)]
                for chunk_record in chunks_for_record:
                    chunk = AgentResponseChunk(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        payload={
                            "event_type": "history.message",
                            "message": chunk_record,
                            "session_id": str(session_id or ""),
                            "total_pages": total_pages,
                            "page_idx": page,
                        },
                        is_complete=False,
                    )
                    wire = encode_agent_chunk_for_wire(
                        chunk,
                        response_id=request.request_id,
                        sequence=sequence,
                    )
                    sequence += 1
                    sent_chunk = False
                    async with send_lock:
                        sent_chunk = await send_wire_payload(ws, wire)
                    if not sent_chunk:
                        logger.warning(
                            "[AgentWebSocketServer] history 流式响应因 chunk 超限而停止: "
                            "request_id=%s sequence=%s",
                            request.request_id,
                            sequence,
                        )
                        return

        next_seq = sequence

        # Session open / refresh: push full todo snapshot before history "done"
        # so the frontend todo panel restores without reading workspace files.
        # Only page 1 — pagination must not re-flash the panel.
        if page_idx == 1 and isinstance(session_id, str) and session_id.strip():
            todos = load_todo_snapshot_for_frontend(session_id)
            todo_chunk = AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={
                    "event_type": "todo.updated",
                    "todos": todos,
                    "session_id": session_id.strip(),
                },
                is_complete=False,
            )
            wire_todo = encode_agent_chunk_for_wire(
                todo_chunk,
                response_id=request.request_id,
                sequence=next_seq,
            )
            sent_todo = False
            async with send_lock:
                sent_todo = await send_wire_payload(ws, wire_todo)
            if not sent_todo:
                # chat timeline still finishes; log so oversized snapshots are visible.
                logger.warning(
                    "[AgentWebSocketServer] history todo.updated snapshot send failed "
                    "(oversized or replaced): request_id=%s session_id=%s seq=%s "
                    "todo_count=%s",
                    request.request_id,
                    session_id.strip(),
                    next_seq,
                    len(todos),
                )
            next_seq += 1

        done_chunk = AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload={
                "event_type": "history.message",
                "status": "done",
                "session_id": str(session_id or ""),
                "total_pages": total_pages,
                "page_idx": page,
            },
            is_complete=True,
        )
        wire_done = encode_agent_chunk_for_wire(
            done_chunk,
            response_id=request.request_id,
            sequence=next_seq,
        )
        async with send_lock:
            await send_wire_payload(ws, wire_done)

    async def _handle_command_add_dir(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            params = request.params or {}
            directory_path = params.get("path")
            remember = params.get("remember", False)
            persist: dict[str, Any]
            if directory_path is None or (
                    isinstance(directory_path, str) and not directory_path.strip()
            ):
                persist = {"ok": False, "error": "path is required"}
            else:
                persist = persist_cli_trusted_directory(str(directory_path))
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=bool(persist.get("ok", False)),
                payload={
                    "path": directory_path,
                    "remember": remember,
                    "persist": persist,
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.add_dir failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "error": str(e),
                    "code": "BAD_REQUEST" if isinstance(e, ValueError) else "SESSION_CREATE_FAILED",
                },
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_command_chrome(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.chrome failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_command_compact(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            session_id = request.session_id or "default"
            params = request.params or {}

            channel_id = request.channel_id or "default"
            mode, sub_mode, _ = resolve_agent_request_mode(params.get("mode", "agent"))
            agent_mode = "agent" if mode == "auto_harness" else mode
            agent = await self._agent_manager.get_agent(
                channel_id=channel_id,
                mode=agent_mode,
                project_dir=resolve_request_project_dir(request),
                sub_mode=sub_mode,
            )

            if agent is None:
                raise ValueError("Failed to get agent")

            result_data = await agent.compress_context(session_id=session_id, return_state=True)

            result = result_data.get("result")
            stats = result_data.get("stats")
            state = result_data.get("state") if isinstance(result_data.get("state"), dict) else {}
            summary = str(
                result_data.get("compact_summary")
                or state.get("compact_summary")
                or result_data.get("summary")
                or ""
            ).strip()

            if result == "compressed" and stats:
                before_tokens = stats.get("raw_total_tokens", 0)
                after_tokens = stats.get("total_tokens", 0)
                if before_tokens > 0:
                    rate = round((before_tokens - after_tokens) / before_tokens * 100, 1)
                else:
                    rate = 0
                stats_summary = (
                    f"\u2713 Context compacted: {after_tokens / 1000:.1f}K/"
                    f"{before_tokens / 1000:.1f}K tokens ({rate:.1f}% saved)"
                )

                if summary:
                    append_compact_history_records(
                        session_id=session_id,
                        request_id=request.request_id,
                        channel_id=channel_id,
                        summary=summary,
                        timestamp=_dt.datetime.now().timestamp(),
                        trigger="manual",
                        stats=stats,
                        mode=params.get("mode", "agent"),
                    )
                    compression_state_payload: dict[str, Any] = {
                        **state,
                        "event_type": "context.compression_state",
                        "status": state.get("status") or "completed",
                        "phase": state.get("phase") or "active_compress",
                        "processor": state.get("processor") or _extract_compact_summary_processor(summary),
                        "before": state.get("before") or {"tokens": before_tokens},
                        "after": state.get("after") or {"tokens": after_tokens},
                        "saved": state.get("saved") or {
                            "tokens": before_tokens - after_tokens,
                            "percent": rate,
                        },
                        "summary": stats_summary,
                        "compact_summary": summary,
                    }
                    await self.send_push({
                        "channel_id": channel_id,
                        "session_id": session_id,
                        "payload": compression_state_payload,
                    })

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "result": result,
                    "stats": stats,
                    **({"summary": summary} if summary else {}),
                    **({"compact_summary": summary} if summary else {}),
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.compact failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_command_compact_partial(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            session_id = request.session_id or "default"
            params = request.params or {}
            turn_index = int(params.get("turn_index", 0))
            direction = str(params.get("direction") or "from").strip()

            channel_id = request.channel_id or "default"
            mode, sub_mode, _ = resolve_agent_request_mode(params.get("mode", "agent"))
            agent_mode = "agent" if mode == "auto_harness" else mode
            agent = await self._agent_manager.get_agent(
                channel_id=channel_id,
                mode=agent_mode,
                project_dir=resolve_request_project_dir(request),
                sub_mode=sub_mode,
            )

            if agent is None:
                raise ValueError("Failed to get agent")

            result_data = await agent.compact_partial(
                session_id=session_id,
                turn_index=turn_index,
                direction=direction,
            )

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=result_data,
            )
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, asyncio.CancelledError)):
                raise
            logger.exception("[AgentWebSocketServer] command.compact_partial failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "status": "failed",
                    "error": str(e),
                },
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_command_context(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            session_id = request.session_id or "default"
            params = request.params or {}

            channel_id = request.channel_id or "default"
            mode, sub_mode, _ = resolve_agent_request_mode(params.get("mode", "agent"))
            agent_mode = "agent" if mode == "auto_harness" else mode
            agent = await self._agent_manager.get_agent(
                channel_id=channel_id,
                mode=agent_mode,
                project_dir=resolve_request_project_dir(request),
                sub_mode=sub_mode,
            )

            if agent is None:
                raise ValueError("Failed to get agent")

            result_data = await agent.get_context_usage(session_id=session_id)

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=result_data,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.context failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_command_recap(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """处理 /recap 命令：生成会话快速回顾（read-only，不修改历史）"""
        try:
            session_id = request.session_id or "default"
            params = request.params or {}
            channel_id = request.channel_id or "default"
            mode, sub_mode, canonical_mode = resolve_agent_request_mode(
                params.get("mode", "agent")
            )
            agent_mode = "agent" if mode == "auto_harness" else mode

            agent = await self._agent_manager.get_agent(
                channel_id=channel_id,
                mode=agent_mode,
                project_dir=resolve_request_project_dir(request),
                sub_mode=sub_mode,
            )

            if agent is None:
                raise ValueError("Failed to get agent")

            result_data = await agent.generate_recap(
                session_id=session_id,
                current_mode=canonical_mode,
            )

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=result_data,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.recap failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "status": "failed",
                    "error": str(e),
                },
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_command_btw(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """处理 /btw 命令：独立、无工具、单轮 LLM 侧问题查询。

        - 获取当前会话上下文（最近消息）
        - 用隔离的 LLM 查询回答问题
        - 不修改对话历史
        - 不使用任何工具（纯文本回答）
        - 仅单轮（无后续 token 消耗）
        """
        try:
            session_id = request.session_id or "default"
            params = request.params or {}
            channel_id = request.channel_id or "default"
            question = (params.get("question") or "").strip()

            logger.info(
                "[AgentWebSocketServer] command.btw received: session_id=%s question=%s",
                session_id,
                question[:100] if question else "",
            )

            if not question:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"status": "failed", "error": "Question is required"},
                )
                wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
                async with send_lock:
                    await send_wire_payload(ws, wire)
                return

            mode, sub_mode, _ = resolve_agent_request_mode(params.get("mode", "agent"))
            agent_mode = "agent" if mode == "auto_harness" else mode

            agent = self._agent_manager.get_agent_for_session_nowait(
                channel_id=channel_id,
                session_id=session_id,
            )
            if agent is None:
                agent = await self._agent_manager.get_agent(
                    channel_id=channel_id,
                    mode=agent_mode,
                    project_dir=resolve_request_project_dir(request),
                    sub_mode=sub_mode,
                )

            if agent is None:
                raise ValueError("Failed to get agent")

            result_data = await agent.generate_btw_answer(
                session_id=session_id,
                question=question,
            )

            logger.info(
                "[AgentWebSocketServer] command.btw result: status=%s",
                result_data.get("status"),
            )

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=result_data,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.btw failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "status": "failed",
                    "error": str(e),
                },
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_command_diff(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from jiuwenswarm.server.runtime.session.git_diff_status import get_session_extra_history_roots
        from jiuwenswarm.server.utils.diff_service import get_diff_service

        try:
            session_id = request.session_id or "default"
            project_dir = resolve_request_project_dir(request)
            extra_history_roots = get_session_extra_history_roots(session_id)
            diff_service = get_diff_service()
            turns, git_diff = await asyncio.gather(
                asyncio.to_thread(
                    diff_service.get_turn_diffs,
                    session_id,
                    project_dir,
                    extra_history_roots=extra_history_roots,
                ),
                asyncio.to_thread(diff_service.get_git_diff, project_dir),
            )

            logger.info(
                "[AgentWebSocketServer] command.diff response: session_id=%s turns=%s git_diff=%s project_dir=%s",
                session_id,
                len(turns),
                git_diff is not None,
                project_dir,
            )

            payload: dict[str, Any] = {
                "type": "list",
                "turns": turns,
            }
            if git_diff is not None:
                payload["gitDiff"] = git_diff

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] command.diff failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_command_simplify(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """处理 /simplify 命令：组装代码精简审查 prompt 并返回（由前端作为消息发送给 Agent）。

        prompt 指导 Agent 分三阶段完成
        1) 识别改动（git diff）
        2) 三维度审查（复用 / 质量 / 效率）—— 子 Agent 并行审查为可选优化手段
        3) 聚合发现并直接修复
        """
        try:
            params = request.params or {}
            target = str(params.get("target", "")).strip()

            prompt = _build_simplify_prompt(target)

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"prompt": prompt},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.simplify failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_command_model(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            params = request.params or {}
            action = params.get("action")

            if action == "add_model":
                target = str(params.get("target", "")).strip()
                logger.info("[command.model] add_model: target=%s", target)
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"type": "model_added", "name": target},
                )

            elif action == "switch_model":
                target = str(params.get("model", "")).strip()
                env_updates = params.get("env_updates", {})
                logger.info(
                    "[command.model] switch_model: target=%s, env_updates=%s",
                    target,
                    mask_sensitive(env_updates),
                )

                if not env_updates:
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=False,
                        payload={"error": "No env_updates provided"},
                    )
                elif _is_env_api_base_placeholder(env_updates):
                    api_base_val = str(env_updates.get("API_BASE", ""))
                    logger.warning(
                        "[command.model] switch_model rejected: API_BASE is a placeholder domain: %s",
                        api_base_val,
                    )
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=False,
                        payload={
                            "error": f"API_BASE '{api_base_val}' 指向占位域名，无法实际提供服务，请配置有效的 API 地址",
                        },
                    )
                else:
                    for k, v in env_updates.items():
                        os.environ[k] = v
                    logger.info("[command.model] os.environ 已更新, MODEL_NAME=%s", os.getenv("MODEL_NAME", "unknown"))

                    try:
                        from jiuwenswarm.agents.harness.common.memory.config import clear_config_cache
                        clear_config_cache()
                        logger.info("[command.model] config cache 已清除")
                    except Exception as e:
                        logger.debug("[command.model] clear_config_cache skipped: %s", e)

                    try:
                        await self._agent_manager.reload_agents_config(None, env_updates)
                        logger.info("[command.model] agent config 已重载")
                    except Exception as e:
                        logger.debug("[command.model] reload_agents_config skipped: %s", e)

                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=True,
                        payload={
                            "current": os.getenv("MODEL_NAME", "unknown"),
                            "requested": target,
                            "type": "switched",
                            "applied": True,
                        },
                    )
                    logger.info("[command.model] 切换完成: current=%s", os.getenv("MODEL_NAME", "unknown"))

            else:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"current": os.getenv("MODEL_NAME", "unknown"), "available": ["default-model"]},
                )

        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.model failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    @staticmethod
    def _mask_sensitive_fields(payload: Any) -> Any:
        if isinstance(payload, dict):
            masked: dict[str, Any] = {}
            for key, value in payload.items():
                key_text = str(key).lower()
                value_text = value.lower() if isinstance(value, str) else ""
                key_sensitive = any(
                    token in key_text for token in ("api_key", "token", "authorization", "secret")
                )
                value_sensitive = any(token in value_text for token in ("bearer ", "api-key ", "secret-"))
                if (key_sensitive or value_sensitive) and not isinstance(value, (dict, list)):
                    masked[key] = "***"
                else:
                    masked[key] = AgentWebSocketServer._mask_sensitive_fields(value)
            return masked
        if isinstance(payload, list):
            return [AgentWebSocketServer._mask_sensitive_fields(item) for item in payload]
        return payload

    @staticmethod
    async def _pre_check_mcp_server(server_payload: dict[str, Any]) -> tuple[bool, str]:
        """Try a temporary connection to verify the MCP server is reachable.

        Uses ``logging.disable(CRITICAL)`` to silence the SDK's verbose
        "Failed to parse JSONRPC message" tracebacks and wraps everything
        in tight timeouts so a broken server cannot block the caller.

        Returns ``(ok, message)``.
        """
        import logging as _logging
        from openjiuwen.core.foundation.tool import McpServerConfig
        from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

        name = server_payload.get("name", "")
        transport = server_payload.get("transport", "")

        # Build McpServerConfig (same logic as _fetch_mcp_tools_from_config)
        payload: dict[str, Any] = {"server_name": name, "client_type": transport}
        if transport == "stdio":
            command = server_payload.get("command", "")
            if not command:
                return True, "skipped: no command"
            # stdio 预检查改为纯静态校验,静态校验零 spawn、零 anyio。
            if not shutil.which(command):
                return False, f"{name} (stdio) pre-check failed: command not found in PATH: {command}"
            raw_args = server_payload.get("args") or []
            if isinstance(raw_args, list):
                for arg in raw_args:
                    if not isinstance(arg, str):
                        continue
                    looks_like_path = (
                        arg.startswith(("/", "./", "../", "~"))
                        or arg.endswith((".js", ".mjs", ".cjs", ".json", ".py", ".sh"))
                    )
                    if looks_like_path and not Path(arg).expanduser().exists():
                        return False, f"{name} (stdio) pre-check failed: file not found: {arg}"
            return True, f"{name} (stdio) pre-check passed (static)"
        else:
            url = server_payload.get("url", "")
            if not url:
                return True, "skipped: no url"
            payload["server_path"] = url
            params = {}
            if isinstance(server_payload.get("headers"), dict):
                params["headers"] = {str(k): str(v) for k, v in server_payload["headers"].items()}
            if params:
                payload["params"] = params

        cfg = McpServerConfig(**payload)
        client = ToolMgr._create_client(cfg)
        _logging.disable(_logging.CRITICAL)
        try:
            connected = await asyncio.wait_for(client.connect(), timeout=15.0)
            if not connected:
                return False, f"{name} ({transport}) pre-check failed: connection refused"
            return True, f"{name} ({transport}) pre-check passed"
        except asyncio.TimeoutError:
            return False, f"{name} ({transport}) pre-check failed: connection timed out"
        except Exception as exc:
            return False, f"{name} ({transport}) pre-check failed: {exc}"
        finally:
            _logging.disable(_logging.NOTSET)
            try:
                await asyncio.wait_for(client.disconnect(), timeout=5.0)
            except Exception as exc:  # noqa: BLE001
                # disconnect runs anyio task-group teardown which raises an
                # ExceptionGroup (Exception subclass) on SSE/HTTP streamable
                # clients. The pre-check already returns a (ok, msg) tuple, so
                # just swallow the disconnect error.
                logger.debug(
                    "[mcp] pre-check disconnect for %s failed: %r", name, exc,
                )

    @staticmethod
    async def _pre_check_mcp_http_auth(
        server_payload: dict[str, Any]
    ) -> tuple[bool, str]:
        """Config-time HTTP probe: reject bad auth (401/403), timeouts, and
        unreachable hosts before writing config.yaml. Delegates to
        ``preflight_mcp_server_reachable`` (shared with cold-start) so both
        gates stay identical. See that function for the anyio-corruption
        rationale.
        """
        from jiuwenswarm.common.mcp_config import (
            build_mcp_server_config,
            preflight_mcp_server_reachable,
        )

        name = str(server_payload.get("name", "") or "").strip()
        transport = str(server_payload.get("transport", "") or "").strip().lower()
        cfg = build_mcp_server_config(server_payload, server_id_scope="jiuwenswarm")
        if cfg is None:
            return False, f"{name} ({transport}) pre-check failed: invalid config entry"
        ok, reason = await preflight_mcp_server_reachable(cfg)
        if ok:
            return True, f"{name} ({transport}) pre-check passed: {reason}"
        return False, f"{name} ({transport}) pre-check failed: {reason}"

    @staticmethod
    async def _fetch_mcp_tools_from_config(entry: dict[str, Any]) -> list[dict[str, Any]]:
        """Create a temporary MCP connection from config entry and list tools.

        Thin delegate to the shared :func:`fetch_mcp_tools_via_temp_connection`
        so the gateway-layer ``mcp.show`` and the agent-layer command.mcp paths
        use one implementation (placeholder resolve, anyio exception-group
        coercion, disconnect cleanup all live in one place)."""
        from jiuwenswarm.common.mcp_config import fetch_mcp_tools_via_temp_connection
        return await fetch_mcp_tools_via_temp_connection(entry)

    async def _handle_mcp_list(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.list`` RPC: list marketplace MCPs (read-only).

        filter: builtin(预置目录) | local(已连接预置 + 全部自定义)。兜底 builtin。
        web 不转发此入口（网关本地已处理），保留只为语义对齐。
        """
        try:
            from jiuwenswarm.server.runtime.mcp.registry import (
                list_marketplace_mcps,
            )
            params = request.params or {}
            filter_val = str(params.get("filter") or "builtin").strip().lower() or "builtin"
            if filter_val not in ("builtin", "local"):
                filter_val = "builtin"
            items = await asyncio.to_thread(list_marketplace_mcps, filter_val)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"type": "list", "items": items},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] mcp.list failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_mcp_show(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.show`` RPC: return one MCP detail with tools."""
        try:
            from jiuwenswarm.server.runtime.mcp.registry import get_mcp
            params = request.params or {}
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("mcp name is required")
            item = get_mcp(name)
            if item is None:
                raise KeyError(f"mcp '{name}' not found")
            # Connected MCP but ToolMgr returned no tools: fall back to a
            # temporary connection. Shared with the gateway-layer show so the
            # fallback logic lives in one place (fill_mcp_tools_fallback).
            from jiuwenswarm.common.mcp_config import fill_mcp_tools_fallback
            await fill_mcp_tools_fallback(item, name)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"type": "detail", "item": item},
            )
        except KeyError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "show_failed", "error": str(exc), "code": "MCP_NOT_FOUND"},
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "bad_request", "error": str(exc), "code": "MCP_BAD_REQUEST"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] mcp.show failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_mcp_connect(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.connect`` RPC: install a marketplace MCP.

        Form A/B upsert config + hot-reload. Form C (CLI) runs CliDriver;
        when an auth step needs user action it returns an ``auth_required``
        sentinel with the extracted ``auth_url``. The frontend opens that URL
        in the user's browser (the CLI binary may not auto-open, and the
        backend may be headless/remote), then sends ``mcp.wait_auth`` which
        holds-open polling ``complete_cli_auth`` until OAuth completes.
        """
        try:
            from jiuwenswarm.server.runtime.mcp.registry import connect_mcp
            params = request.params or {}
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("mcp name is required")
            item = await asyncio.to_thread(connect_mcp, name)
            if isinstance(item, dict) and item.get("auth_required"):
                # CLI form: connect_mcp started the CLI auth command and
                # extracted the auth_url. Return it now so the frontend can
                # open the browser (the CLI binary may not auto-open, and the
                # backend may be headless/remote — the frontend's browser is
                # the reliable place to open the auth page). The frontend then
                # sends mcp.wait_auth to hold-open poll until OAuth completes.
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"type": "auth_required", **self._mask_sensitive_fields(item)},
                )
            elif isinstance(item, dict) and item.get("credentials_required"):
                # Form B missing token: surface a prompt instead of reloading —
                # there is no config entry to reload until tokens are provisioned.
                # NOTE: item fields are metadata (required_tokens list,
                # credential_kind), not secrets — pass through unmasked. The
                # generic _mask_sensitive_fields keys on 'token' substrings and
                # would replace required_tokens with '***', breaking the
                # frontend's array methods on that list.
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"type": "credentials_required", **item},
                )
            else:
                # Connect-time live-connect probe: not just register the entry,
                # but confirm the MCP is actually usable before reporting
                # "connected". For server-bearing types (stdio / remote-mcp /
                # hybrid-cli's mcp.json subcommand) this spawns the stdio
                # subprocess + MCP initialize handshake, or does a real HTTP
                # connect — so npx first-install cost and handshake failures
                # surface HERE (the user waits on connect) instead of silently
                # degrading to "no tools" on the first chat message. A
                # successful probe leaves the connection in the process-global
                # Runner.resource_mgr cache, which reconcile reuses (no
                # duplicate spawn on chat.send). skill-only / pure-CLI MCPs have
                # no server entry — the probe returns (True, "") and they
                # surface via bundled skills.
                probe_ok, probe_reason = await self._agent_manager.probe_mcp_live_connection(name)
                if not probe_ok:
                    try:
                        # Roll back: marketplace → remove record+skills; custom
                        # → flip to registered (keep definition for edit/retry,
                        # do NOT delete).
                        from jiuwenswarm.server.runtime.mcp.registry import (
                            rollback_failed_connect,
                        )
                        rollback_failed_connect(name)
                    except Exception as rollback_exc:  # noqa: BLE001
                        logger.warning(
                            "[mcp] rollback failed-probe entry '%s' failed: %s",
                            name, rollback_exc,
                        )
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=False,
                        payload={
                            "type": "connect_failed",
                            "error": probe_reason or "MCP live-connect probe failed",
                            "code": "MCP_UNREACHABLE",
                            "name": name,
                        },
                    )
                else:
                    # Probe succeeded (or no server to probe) — flip
                    # connecting → connected so the MCP is selectable per
                    # session, sync token env for skill-only bundled scripts,
                    # and return. The MCP is NOT loaded into a specific agent
                    # session here: session-level enable is driven by the
                    # ``mcp`` field on chat.send (see reconcile_session_mcp).
                    try:
                        from jiuwenswarm.server.runtime.mcp.state_store import (
                            set_mcp_state,
                        )
                        set_mcp_state(name, state="connected")
                    except Exception as flip_exc:  # noqa: BLE001
                        logger.warning(
                            "[mcp] flip connecting→connected after connect failed for '%s': %s",
                            name, flip_exc,
                        )
                    # Skill-only connectors' bundled scripts read tokens from
                    # os.environ; sync the just-provisioned token into the agent
                    # process env so BashTool inherits it. No-op for MCP-type
                    # connectors (their tokens resolve via McpServerConfig).
                    try:
                        self._agent_manager.sync_mcp_credentials()
                    except Exception as sync_exc:  # noqa: BLE001
                        logger.warning("[mcp] sync_mcp_credentials after connect failed: %s", sync_exc)
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=True,
                        payload={
                            "type": "connected",
                            "name": name,
                            "applied": True,
                            "item": self._mask_sensitive_fields(item),
                        },
                    )
        except KeyError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "connect_failed", "error": str(exc), "code": "MCP_NOT_FOUND"},
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "bad_request", "error": str(exc), "code": "MCP_BAD_REQUEST"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] mcp.connect failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_mcp_wait_auth(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.wait_auth`` RPC: hold-open poll for CLI OAuth completion.

        After ``mcp.connect`` returned ``auth_required`` (the frontend opened
        the browser when ``auth_url`` was non-empty), the frontend sends this
        RPC which polls :func:`complete_cli_auth` (via :meth:`_await_cli_auth`)
        until the user finishes OAuth in the browser, then returns the single
        ``connected``/``auth_failed`` response. Holds the RPC open for minutes;
        the frontend shows a "connecting…" spinner while waiting.
        """
        try:
            params = request.params or {}
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("mcp name is required")
            step_index = int(params.get("step_index", 0) or 0)
            result = await self._await_cli_auth(name, step_index)
            if result.get("type") == "auth_failed":
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"type": "connect_failed", "name": name,
                             "error": str(result.get("error", "")), "code": "MCP_AUTH_FAILED"},
                )
            else:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload=result,
                )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "bad_request", "error": str(exc), "code": "MCP_BAD_REQUEST"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] mcp.wait_auth failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_mcp_disconnect(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.disconnect`` RPC: remove from config + reload."""
        try:
            from jiuwenswarm.server.runtime.mcp.registry import disconnect_mcp
            params = request.params or {}
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("mcp name is required")
            removed = await asyncio.to_thread(disconnect_mcp, name)
            applied = True
            error_message = ""
            try:
                # Phase-2: targeted unregister (no full reload). SSE/HTTP MCP
                # removal can raise "Attempted to exit cancel scope in a
                # different task" (openjiuwen closes the client TaskGroup from
                # the wrong task) — that propagates as CancelledError and tears
                # down this handler before it can reply. Shield + timeout so the
                # state.json change (already done above) still lands and the
                # frontend gets a response; the MCP server process is torn down
                # by the agent's next reload even if this call fails.
                await asyncio.wait_for(
                    asyncio.shield(self._agent_manager.apply_mcp_change(name, "remove")),
                    timeout=15.0,
                )
            except asyncio.TimeoutError:
                applied = False
                error_message = "MCP unregister timed out (state updated; server will clear on next reload)"
                logger.warning("[mcp] apply_mcp_change after disconnect timed out for '%s'", name)
            except asyncio.CancelledError:
                # apply_mcp_change runs inside asyncio.shield, so an inner
                # SSE cancel-scope error cannot surface here as a
                # CancelledError (shield absorbs it). The only way this
                # except fires is the *outer* request being cancelled (ws
                # disconnect) — propagate it so the request unwinds cleanly
                # instead of dragging through clear/refresh and sending a
                # response to a dead socket.
                logger.warning(
                    "[mcp] apply_mcp_change after disconnect CancelledError for '%s'", name
                )
                raise
            except Exception as reload_exc:  # noqa: BLE001
                applied = False
                error_message = str(reload_exc)
                logger.warning("[mcp] apply_mcp_change after disconnect failed: %s", reload_exc)
            # Clear this MCP's token env vars from the agent process so a
            # disconnected skill-only MCP's token doesn't linger.
            try:
                self._agent_manager.clear_mcp_credentials(name)
            except Exception as clear_exc:  # noqa: BLE001
                logger.warning("[mcp] clear_mcp_credentials after disconnect failed: %s", clear_exc)
            # disconnect uninstalled the bundled skills + unregistered the dir;
            # reload SkillUseRail so the agent stops seeing those skills.
            try:
                await self._agent_manager.refresh_skill_rails()
            except Exception as skill_exc:  # noqa: BLE001
                logger.warning("[mcp] refresh_skill_rails after disconnect failed: %s", skill_exc)
            payload = {
                "type": "disconnected",
                "name": name,
                "applied": applied,
                "item": self._mask_sensitive_fields(removed),
            }
            if error_message:
                payload["error"] = error_message
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
        except KeyError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "disconnect_failed", "error": str(exc), "code": "MCP_NOT_FOUND"},
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "bad_request", "error": str(exc), "code": "MCP_BAD_REQUEST"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] mcp.disconnect failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _finalize_cli_auth(self, name: str, item: dict[str, Any]) -> dict[str, Any]:
        """Run the post-auth side-effects for a CLI MCP connect.

        Auth is done; skills were installed during registry ``_finalize_cli``.
        Syncs MCP tokens into os.environ so bundled skill scripts (BashTool
        inherits os.environ) see their token env vars, then promotes
        connecting → connected. The MCP is NOT loaded into the agent here —
        session-level enable is driven by the ``mcp`` field on chat.send (see
        reconcile_session_mcp). Returns a payload dict for the push signal.
        """
        try:
            from jiuwenswarm.server.runtime.mcp.state_store import (
                set_mcp_state,
            )
            set_mcp_state(name, state="connected")
        except Exception as flip_exc:  # noqa: BLE001
            logger.warning(
                "[mcp] flip connecting→connected after CLI auth failed for '%s': %s",
                name, flip_exc,
            )
        try:
            self._agent_manager.sync_mcp_credentials()
        except Exception as sync_exc:  # noqa: BLE001
            logger.warning("[mcp] sync_mcp_credentials after CLI auth failed: %s", sync_exc)
        return {
            "type": "connected",
            "name": name,
            "applied": True,
            "item": self._mask_sensitive_fields(item),
        }

    async def _await_cli_auth(
        self, name: str, step_index: int,
        *, max_attempts: int = 200, delay: float = 3.0,
    ) -> dict[str, Any]:
        """Poll a CLI MCP's auth status until authenticated, returning the result.

        Called inline by :meth:`_handle_mcp_connect` after ``connect_mcp``
        returns an ``auth_required`` sentinel (the CLI process already opened
        the browser). This holds the RPC open — the frontend shows a
        "connecting…" spinner — and loops :func:`complete_cli_auth` until the
        user finishes OAuth, then runs the post-auth side-effects and returns
        a ``connected`` payload (or ``auth_failed`` on timeout/error) for the
        handler to send as the single RPC response. No push channel needed.

        Multi-step CLIs (e.g. feishu: config init → auth login): when a step
        completes and the next needs user action, ``complete_cli_auth`` returns
        ``auth_required=True`` with the new ``step_index``; we adopt it as
        ``cur_step`` so the next poll queries the new step — otherwise the
        poller would re-query the old step forever (a real dead-loop on
        multi-step CLIs).

        Only CLI MCPs reach here (form A/B/D return ``auth_required=False`` or
        ``credentials_required`` from :func:`connect_mcp`; only
        ``itype == "cli"`` calls ``_connect_cli`` which can return the
        ``auth_required`` sentinel). No form A/B/D path triggers this loop.
        """
        from jiuwenswarm.server.runtime.mcp.registry import complete_cli_auth

        cur_step = max(0, int(step_index))
        last_output = ""
        try:
            for attempt in range(max_attempts):
                item = await asyncio.to_thread(complete_cli_auth, name, cur_step)
                if not isinstance(item, dict):
                    raise ValueError(f"complete_cli_auth returned non-dict: {item!r}")
                if item.get("auth_required"):
                    # Still pending — either the user hasn't finished in the
                    # browser, or a step just completed and the next step needs
                    # action. Adopt the authoritative step_index so we don't
                    # loop on a stale step (dead-loop guard).
                    new_step = item.get("step_index")
                    if isinstance(new_step, (int, float)):
                        cur_step = max(cur_step, int(new_step))
                    last_output = str(item.get("output") or item.get("matched") or "")[:300]
                    logger.debug(
                        "[mcp] _await_cli_auth '%s' still pending (attempt %d/%d, step %d): %s",
                        name, attempt + 1, max_attempts, cur_step, last_output[:200],
                    )
                    await asyncio.sleep(delay)
                    continue
                # Authenticated — finalize and return the connected payload.
                return await self._finalize_cli_auth(name, item)
            # Exhausted retries (~10 min) — return a failure so the handler can
            # surface it. Include the last status output so a misaligned
            # statusMatch (e.g. dingtalk's CLI status JSON not matching
            # cli.json's statusMatch keys) is diagnosable from the error.
            logger.warning(
                "[mcp] _await_cli_auth '%s' timed out after %d attempts (~%.0f min). "
                "last status output: %s",
                name, max_attempts, max_attempts * delay / 60, last_output,
            )
            timeout_error = "Authorization timed out. Retry connect."
            if last_output:
                timeout_error = f"{timeout_error} (last status: {last_output})"
            return {"type": "auth_failed", "name": name, "error": timeout_error}
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] _await_cli_auth '%s' failed: %s", name, exc)
            return {"type": "auth_failed", "name": name, "error": str(exc)}

    async def _handle_mcp_register_custom(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.register_custom`` RPC: write a user-defined MCP server config.

        Only persists the definition to state.json (state=registered) and returns
        immediately; the frontend follows up with ``mcp.connect`` to actually
        activate it. Editing an already-connected custom MCP removes the old live
        instance so the new config takes effect on connect.
        """
        try:
            from jiuwenswarm.server.runtime.mcp.registry import register_custom_mcp
            params = request.params or {}
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("mcp name is required")
            config = {k: v for k, v in params.items() if k != "name"}
            entry = await asyncio.to_thread(register_custom_mcp, name, config)
            was_connected = bool(entry.pop("was_connected", False))
            # Edit of a connected instance: remove the old live server so the
            # new config (just persisted as state=registered) takes effect on
            # the frontend's follow-up mcp.connect. Failure here is non-fatal —
            # the old instance also drops on the next agent reload.
            if was_connected:
                try:
                    await self._agent_manager.apply_mcp_change(name, "remove")
                except Exception as reload_exc:  # noqa: BLE001
                    logger.warning(
                        "[mcp] remove old instance after register_custom '%s' failed: %s",
                        name, reload_exc,
                    )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "type": "registered",
                    "name": name,
                    "item": self._mask_sensitive_fields(entry),
                },
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "bad_request", "error": str(exc), "code": "MCP_BAD_REQUEST"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] mcp.register_custom failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_mcp_delete_custom(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.delete_custom``: terminal teardown of a custom MCP."""
        try:
            from jiuwenswarm.server.runtime.mcp.registry import delete_custom_mcp
            params = request.params or {}
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("mcp name is required")
            # Clear env vars first — delete_custom_mcp wipes the credential
            # file + state record, and after that _mcp_env_keys returns [] so
            # the token would leak in os.environ. Clearing BEFORE the wipe
            # means the CredentialStore still has the keys to read.
            try:
                self._agent_manager.clear_mcp_credentials(name)
            except Exception as clear_exc:  # noqa: BLE001
                logger.warning("[mcp] clear_mcp_credentials before delete failed: %s", clear_exc)
            removed = await asyncio.to_thread(delete_custom_mcp, name)
            was_connected = bool(removed.get("was_connected", False))
            applied = True
            error_message = ""
            if was_connected:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(self._agent_manager.apply_mcp_change(name, "remove")),
                        timeout=15.0,
                    )
                except asyncio.TimeoutError:
                    applied = False
                    error_message = "MCP unregister timed out (state deleted; server will clear on next reload)"
                    logger.warning("[mcp] apply_mcp_change after delete timed out for '%s'", name)
                except asyncio.CancelledError:
                    logger.warning("[mcp] apply_mcp_change after delete CancelledError for '%s'", name)
                    raise  # outer ws-disconnect — don't reply to a dead socket
                except Exception as reload_exc:  # noqa: BLE001
                    applied = False
                    error_message = str(reload_exc)
                    logger.warning("[mcp] apply_mcp_change after delete failed: %s", reload_exc)
            try:
                await self._agent_manager.refresh_skill_rails()
            except Exception as skill_exc:  # noqa: BLE001
                logger.warning("[mcp] refresh_skill_rails after delete failed: %s", skill_exc)
            payload = {
                "type": "deleted",
                "name": name,
                "applied": applied,
                "item": self._mask_sensitive_fields(removed),
            }
            if error_message:
                payload["error"] = error_message
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
        except KeyError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "delete_failed", "error": str(exc), "code": "MCP_NOT_FOUND"},
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "bad_request", "error": str(exc), "code": "MCP_BAD_REQUEST"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] mcp.delete_custom failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_mcp_save_credentials(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.save_credentials`` RPC: persist user-supplied tokens.

        Form B connectors (tianyancha/gildata/gmail/jira...) declare ``${VAR}``
        placeholders in mcp.json; the user fills the values via a frontend
        prompt, which calls this RPC. Tokens are stored in the local
        CredentialStore (never returned in the response). Does NOT reload —
        the caller follows up with ``mcp.connect`` to actually register.
        """
        try:
            from jiuwenswarm.server.runtime.mcp.registry import save_mcp_credentials
            params = request.params or {}
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("mcp name is required")
            tokens = params.get("tokens") or {}
            if not isinstance(tokens, dict) or not tokens:
                raise ValueError("tokens (non-empty dict) is required")
            result = await asyncio.to_thread(save_mcp_credentials, name, tokens)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"type": "credentials_saved", "name": name, "saved_keys": result.get("saved_keys", [])},
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "bad_request", "error": str(exc), "code": "MCP_BAD_REQUEST"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] mcp.save_credentials failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_command_sandbox(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """处理 ``/sandbox`` 命令.

        子命令通过 ``params["sub"]`` 路由:
        - ``status`` / ``enable`` / ``disable``
        - ``exclude.add`` / ``exclude.remove`` / ``exclude.list``
        - ``files.allow`` / ``files.deny`` / ``files.list``

        ``enable``/``disable`` 走 ``agent_manager.recreate_agent`` (重建 sys_operation 类型);
        其他写动作通过 ``adapter.apply_sandbox_runtime_patch()`` 立即热更,
        不重建 agent.

        当 ``sandbox.type=yuanrong`` 时仅允许 ``status`` (裸 ``/sandbox`` 查看
        enabled/executor/mounts); 任意子指令一律拒绝。
        """
        params = request.params or {}
        sub = str(params.get("sub", "status")).strip().lower() or "status"
        channel_id = request.channel_id or "default"
        try:
            # 平台守卫: ``/sandbox`` 全家桶仅在 Linux 上可用。 放在 try 内部是
            # 故意的, 让 ValueError 命中下方 ``except ValueError`` 分支转成
            # ``SANDBOX_BAD_REQUEST`` 回执, 跟其它入参校验失败的处理一致。
            _require_sandbox_supported()
            endpoint = get_sandbox_endpoint()
            sandbox_type = str(endpoint.get("type") or "").strip().lower()
            if sandbox_type == "yuanrong":
                if sub != "status":
                    raise ValueError(
                        "sandbox.type=yuanrong: only /sandbox (view config) is "
                        "supported; subcommands are disabled"
                    )
                payload = build_yuanrong_sandbox_status_view()
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload=payload,
                )
            else:
                validate_sandbox_files_runtime(get_sandbox_runtime().get("files"))
                if sub == "status":
                    payload = {"runtime": get_sandbox_runtime()}
                elif sub == "enable":
                    payload = await self._handle_sandbox_enable(channel_id)
                elif sub == "disable":
                    payload = await self._handle_sandbox_disable(channel_id)
                elif sub == "exclude.add":
                    payload = await self._handle_sandbox_exclude_add(channel_id, params)
                elif sub == "exclude.remove":
                    payload = await self._handle_sandbox_exclude_remove(channel_id, params)
                elif sub == "exclude.list":
                    payload = {
                        "excluded_commands": list(
                            get_sandbox_runtime().get("excluded_commands") or []
                        )
                    }
                elif sub == "files.allow":
                    payload = await self._handle_sandbox_files_set(
                        channel_id, params, bucket="allow"
                    )
                elif sub == "files.deny":
                    payload = await self._handle_sandbox_files_set(
                        channel_id, params, bucket="deny"
                    )
                elif sub == "files.remove":
                    payload = await self._handle_sandbox_files_remove(channel_id, params)
                elif sub == "files.list":
                    payload = {"files": dict(get_sandbox_runtime().get("files") or {})}
                else:
                    raise ValueError(f"unknown sub: {sub!r}")
                self._attach_effective_sandbox_files(payload, channel_id, params)
                await self._attach_landlock_status(payload)
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload=payload,
                )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "SANDBOX_BAD_REQUEST"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.sandbox failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "SANDBOX_INTERNAL"},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_sandbox_enable(self, channel_id: str) -> dict[str, Any]:
        # 1. 解析 sandbox endpoint: 优先 config.yaml::sandbox.url/type, 缺省走本地 jiuwenbox.
        # ``get_sandbox_endpoint`` 已经把 startup_mode / policy_file 的归一化值一并返回:
        # - startup_mode 缺省/非法 → "internal"
        # - policy_file 缺省 → "" (此处再回落到 DEFAULT_SANDBOX_POLICY_FILE)
        endpoint = get_sandbox_endpoint()
        url = endpoint.get("url") or "http://127.0.0.1:8321"
        sandbox_type = endpoint.get("type") or "jiuwenbox"

        # startup_mode:
        # - internal: agent-server 通过 JiuwenBoxRunner 拉起 jiuwenbox (默认行为);
        # - external: 用户自己启动 jiuwenbox (例如需要 sudo + network.mode: isolated),
        #   本侧只做健康检查, 不可达直接报错并提示如何手动启动。
        startup_mode = endpoint.get("startup_mode") or DEFAULT_SANDBOX_STARTUP_MODE

        # policy_file:
        # - 仅文件名 → 在 jiuwenbox/configs 下查找; 含路径 / 绝对路径 → 整路径使用;
        # - 未配置 → 回落到 DEFAULT_SANDBOX_POLICY_FILE (即 code-agent-policy.yaml),
        #   并在下方与 url/type 一起写回 config.yaml, 让重启后无需再走 fallback 路径。
        raw_policy = endpoint.get("policy_file") or ""
        effective_policy_file = raw_policy or DEFAULT_SANDBOX_POLICY_FILE
        policy_path = resolve_sandbox_policy_path(effective_policy_file)
        if policy_path is None:
            raise RuntimeError(
                f"sandbox.policy_file={effective_policy_file!r} 无法解析: "
                f"仅给出文件名时需能定位到 jiuwenbox/configs 目录, "
                f"否则请在 config.yaml::sandbox.policy_file 里配置绝对路径。",
            )
        if not policy_path.is_file():
            raise RuntimeError(
                f"sandbox policy 文件不存在: {policy_path} "
                f"(原始配置 sandbox.policy_file="
                f"{raw_policy or f'<default:{DEFAULT_SANDBOX_POLICY_FILE}>'!r})",
            )

        # 2. 解析 host:port 并 (internal 模式下) 完成端口分配。
        # external 模式: 直接用配置里的 url, 由用户保证 jiuwenbox 监听在此处。
        # internal 模式: 期望端口被占就换一个随机空闲端口, 不去探测占用方是谁。
        host, preferred_port = self._parse_sandbox_host_port(url)
        if startup_mode == "internal":
            port = self._allocate_internal_jiuwenbox_port(host, preferred_port)
            if port != preferred_port:
                # 端口换过, 同步刷新 url 以便后续落盘 / 透传给前端
                url = f"http://{host}:{port}"
                logger.info(
                    "[command.sandbox] jiuwenbox effective url changed to %s "
                    "(preferred port %d was busy)",
                    url,
                    preferred_port,
                )
        else:
            port = preferred_port

        # 3. 启动 / 健康检查本地 jiuwenbox; 失败直接报错
        ok = await self._jiuwenbox_runner.ensure_running(
            host=host,
            port=port,
            startup_mode=startup_mode,
            policy_path=policy_path,
        )
        if not ok:
            if startup_mode == "external":
                raise RuntimeError(
                    f"jiuwenbox 未在 {host}:{port} 监听 (sandbox.startup_mode=external); "
                    f"请在另一终端先启动 jiuwenbox-server, 例如:\n"
                    f"  sudo -E .venv/bin/python -m uvicorn jiuwenbox.server.app:app "
                    f"--host {host} --port {port}\n"
                    f"  (JIUWENBOX_POLICY_PATH={policy_path})"
                )
            stderr_tail = self._jiuwenbox_runner.get_stderr_tail(20)
            hint = "\n--- jiuwenbox stderr (tail) ---\n" + stderr_tail if stderr_tail else (
                " (no stderr captured; jiuwenbox / uvicorn 可能未安装)"
            )
            raise RuntimeError(
                f"jiuwenbox 启动或健康检查失败 ({host}:{port}){hint}"
            )

        # 4. 把 endpoint 写回 config.yaml, 保证 agent 重建 / agent-server 重启后能直接读到。
        # url 此时已是端口分配后的最终值; startup_mode / policy_file / preserve_file_sharing_mode 一并落盘。
        preserve_mode = resolve_preserve_file_sharing_mode_default()
        try:
            update_sandbox_endpoint(
                url,
                sandbox_type,
                startup_mode=startup_mode,
                policy_file=effective_policy_file,
                preserve_file_sharing_mode=preserve_mode,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[command.sandbox] persist sandbox endpoint failed: %s", exc)

        runtime = update_sandbox_runtime({"enabled": True})
        await self._agent_manager.recreate_agent(channel_id, immediate=True)

        return {
            "runtime": runtime,
            "endpoint": {
                "url": url,
                "type": sandbox_type,
                "preserve_file_sharing_mode": preserve_mode,
                "startup_mode": startup_mode,
                "policy_file": effective_policy_file,
            },
            "jiuwenbox": {
                "host": host,
                "port": port,
                "ready": True,
                "startup_mode": startup_mode,
                "policy_path": str(policy_path),
            },
            "agent_recreated": True
        }

    async def _handle_sandbox_disable(self, channel_id: str) -> dict[str, Any]:
        runtime = update_sandbox_runtime({"enabled": False})
        await self._agent_manager.recreate_agent(channel_id, immediate=True)

        # 记录关闭前的端点用于回执 (external 模式下 runner 没拥有进程, 会是 None)。
        owned_endpoint = self._jiuwenbox_runner.get_owned_endpoint()
        jiuwenbox_stopped = False
        if owned_endpoint is not None:
            try:
                await self._jiuwenbox_runner.stop()
                jiuwenbox_stopped = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[AgentWebSocketServer] /sandbox disable: jiuwenbox stop failed: %s",
                    exc,
                )
        else:
            logger.debug(
                "[AgentWebSocketServer] /sandbox disable: no owned jiuwenbox to stop "
                "(external startup_mode or never started)"
            )

        payload: dict[str, Any] = {
            "runtime": runtime,
            "agent_recreated": True,
            "jiuwenbox_stopped": jiuwenbox_stopped,
        }
        if owned_endpoint is not None:
            host, port = owned_endpoint
            payload["jiuwenbox"] = {"host": host, "port": port, "ready": False}
        return payload

    async def _handle_sandbox_exclude_add(
        self, channel_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        pattern = str(params.get("pattern") or "").strip()
        if not pattern:
            raise ValueError("pattern is required")
        current = get_sandbox_runtime()
        patterns = list(current.get("excluded_commands") or [])
        if pattern in patterns:
            raise ValueError(
                f"excluded_commands already contains {pattern!r}; "
                "use a different pattern or remove it first"
            )
        patterns.append(pattern)
        runtime = update_sandbox_runtime({"excluded_commands": patterns})
        await self._apply_sandbox_runtime_patch(channel_id, runtime, files_changed=False)
        return {"runtime": runtime}

    async def _handle_sandbox_exclude_remove(
        self, channel_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        pattern = str(params.get("pattern") or "").strip()
        if not pattern:
            raise ValueError("pattern is required")
        current = get_sandbox_runtime()
        existing = list(current.get("excluded_commands") or [])
        if pattern not in existing:
            raise ValueError(
                f"excluded_commands does not contain {pattern!r}; "
                "nothing to remove"
            )
        patterns = [p for p in existing if p != pattern]
        runtime = update_sandbox_runtime({"excluded_commands": patterns})
        await self._apply_sandbox_runtime_patch(channel_id, runtime, files_changed=False)
        return {"runtime": runtime}

    def _dry_run_files_policy(
        self,
        channel_id: str,
        params: dict[str, Any],
        files: dict[str, Any],
    ) -> None:
        project_dir = self._resolve_active_project_dir(channel_id, params)
        is_code_agent = self._resolve_active_is_code_agent(channel_id)
        try:
            build_filesystem_policy(
                files,
                project_dir=project_dir,
                is_code_agent=is_code_agent,
                startup_mode=get_sandbox_startup_mode(),
            )
        except FileNotFoundError as exc:
            raise ValueError(str(exc)) from exc

    async def _handle_sandbox_files_set(
        self, channel_id: str, params: dict[str, Any], *, bucket: str
    ) -> dict[str, Any]:
        _reject_extra_sandbox_files_params(params)
        path = str(params.get("path") or "").strip()
        if not path:
            raise ValueError("path is required")
        # 把 path 展开成 absolute resolved 形式, 让 ``./foo`` / ``~/data`` /
        # 含 ``..`` 之类写法在入口就被归一化到稳定路径, 避免后续 stat / 入库
        # / 比较行为依赖 jiuwenswarm server 当前 cwd; 见
        # :func:`_canonicalize_sandbox_files_path` 的文档说明。
        canonical = _canonicalize_sandbox_files_path(path)
        if canonical != path:
            logger.info(
                "[sandbox] files %s: canonicalize path %r -> %r",
                bucket, path, canonical,
            )
            path = canonical
        # 拒绝把"自动配置且不可变"的路径 (intrinsic AGENT.md / HEARTBEAT.md /...
        # / daily_memory / 项目目录 / jiuwenswarm config.yaml) 再次写进
        # config.yaml::sandbox.files。 它们由 sysop_builder 在每次
        # build_filesystem_policy 时按需重建; 让用户能 add 只会污染配置, 而且
        # 若一个路径同时在 auto-allow 和用户-deny 里 (反之亦然), 实际行为难以
        # 预期, 不如直接在入口阻断。``params`` 透传给 ``_resolve_active_
        # project_dir`` 以便 TUI 通过 ``trusted_dirs`` / ``cwd`` 显式声明的
        # 项目目录也参与 auto 路径的判定。
        project_dir = self._resolve_active_project_dir(channel_id, params)
        is_code_agent = self._resolve_active_is_code_agent(channel_id)
        match = find_auto_managed_match(
            path,
            project_dir=project_dir,
            is_code_agent=is_code_agent,
            startup_mode=get_sandbox_startup_mode(),
        )
        if match is not None:
            matched_bucket, canonical = match
            raise ValueError(
                f"path is auto-managed (always in {matched_bucket}): {canonical}; "
                f"cannot add via /sandbox files {bucket}"
            )
        current = get_sandbox_runtime()
        files = dict(current.get("files") or {})
        files.setdefault("allow", [])
        files.setdefault("deny", [])
        # 1) 同 bucket 内已经存在等价条目 → 直接报错, 不做 "先删后加" 的隐式覆盖。
        target_list: list[Any] = list(files.get(bucket) or [])
        for existing in target_list:
            if _file_entry_matches_path(existing, path):
                raise ValueError(
                    f"sandbox.files.{bucket} already contains {path!r}; "
                    f"use `/sandbox files remove {path}` first if you want to change it"
                )
        # 2) 反方向 bucket 已经登记了同一条 → allow / deny 在 Landlock 层语义直接
        #    冲突, 拒绝。 用户得先把它从对侧 ``remove`` 掉再加, 显式表达 "我要
        #    切换权限方向" 的意图。
        opposite_bucket = "deny" if bucket == "allow" else "allow"
        for existing in files.get(opposite_bucket) or []:
            if _file_entry_matches_path(existing, path):
                raise ValueError(
                    f"sandbox.files.{opposite_bucket} already contains {path!r}; "
                    f"cannot add the same path to {bucket}. "
                    f"`/sandbox files remove {path}` first if you want to flip it"
                )
        nested_error = find_nested_files_conflict(path, bucket, files)
        if nested_error is not None:
            raise ValueError(nested_error)
        entry: dict[str, Any] = {"path": path}
        target_list.append(entry)
        files[bucket] = target_list
        # 在写盘前做一次 dry-run, 防止后续 build_filesystem_policy 抛错时,
        # yaml 已经被更新成一份永远 build 不出 policy 的中间态 (见
        # :meth:`_dry_run_files_policy` 的文档说明)。
        self._dry_run_files_policy(channel_id, params, files)
        runtime = update_sandbox_runtime({"files": files})
        await self._apply_sandbox_runtime_patch(channel_id, runtime, files_changed=True)
        return {"runtime": runtime}

    async def _handle_sandbox_files_remove(
        self, channel_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        _reject_extra_sandbox_files_params(params)
        path = str(params.get("path") or "").strip()
        if not path:
            raise ValueError("path is required")
        # 与 _handle_sandbox_files_set 保持同一份 canonicalize, 让 ``remove
        # ./foo`` 能命中以 absolute 形式入库的 entry; 兼容旧 yaml 残留写法的
        # 兜底由 :func:`_file_entry_matches_path` 双侧 canonicalize 比较负责。
        canonical = _canonicalize_sandbox_files_path(path)
        if canonical != path:
            logger.info(
                "[sandbox] files remove: canonicalize path %r -> %r",
                path, canonical,
            )
            path = canonical
        # 同 _handle_sandbox_files_set: auto-managed 条目由 sysop_builder 在
        # 每次 build_filesystem_policy 时重建, 用户不能也不必通过 /sandbox 删除
        # 它们。如果旧版本 config.yaml 里残留了这些路径, 提示用户直接改 yaml,
        # 而不是让 /sandbox 默默地把同一个 auto-managed 名字从用户配置里抹掉
        # ——后者会让用户误以为他/她真的把 sandbox 自动条目摘掉了。
        project_dir = self._resolve_active_project_dir(channel_id, params)
        is_code_agent = self._resolve_active_is_code_agent(channel_id)
        match = find_auto_managed_match(
            path,
            project_dir=project_dir,
            is_code_agent=is_code_agent,
            startup_mode=get_sandbox_startup_mode(),
        )
        if match is not None:
            matched_bucket, canonical = match
            raise ValueError(
                f"path is auto-managed (always in {matched_bucket}): {canonical}; "
                f"cannot remove via /sandbox files remove"
            )
        current = get_sandbox_runtime()
        files = dict(current.get("files") or {})
        files.setdefault("allow", [])
        files.setdefault("deny", [])
        matched_buckets: list[str] = []
        for bucket in ("allow", "deny"):
            kept: list[Any] = []
            removed = False
            for entry in files.get(bucket) or []:
                if _file_entry_matches_path(entry, path):
                    removed = True
                    continue
                kept.append(entry)
            if removed:
                matched_buckets.append(bucket)
                files[bucket] = kept
        if not matched_buckets:
            raise ValueError(
                f"sandbox.files has no entry for {path!r}; nothing to remove"
            )
        # 与 _handle_sandbox_files_set 对齐: 在写盘前 dry-run, 避免 build 失败
        # 时 yaml 已被写成 build 不出 policy 的死局 (见 :meth:`_dry_run_files
        # _policy` 的文档说明)。
        self._dry_run_files_policy(channel_id, params, files)
        runtime = update_sandbox_runtime({"files": files})
        await self._apply_sandbox_runtime_patch(channel_id, runtime, files_changed=True)
        return {"runtime": runtime}

    def _resolve_active_project_dir(
        self, channel_id: str, params: dict[str, Any] | None = None
    ) -> str | None:
        """Resolve the user project dir for the current ``/sandbox`` view.

        Lookup order, falling through on empty/missing:

        1. ``params["project_dir"]`` -- stable client project identity.
        2. ``adapter._project_dir`` / ``adapter._instance_overrides``.
        3. ``params["cwd"]`` -- legacy/dynamic fallback.
        4. ``params["trusted_dirs"][0]`` -- final compatibility fallback.

        Returns ``None`` only when none of the above yield a usable path; we
        deliberately do NOT fall back to ``Path.cwd()`` of the agent-server
        process because that's typically ``~/.jiuwenswarm`` and would
        mislabel the displayed ``files.allow_write`` entry.
        """
        if isinstance(params, dict):
            project_dir = params.get("project_dir")
            if isinstance(project_dir, str) and project_dir.strip():
                return project_dir.strip()
        try:
            agent = self._agent_manager.get_agent_nowait(channel_id)
        except Exception as exc:
            logger.info("[command.sandbox] get_agent_nowait failed: %s", exc)
            return None
        adapter = self._resolve_adapter(agent)
        if adapter is None:
            return None
        direct = getattr(adapter, "_project_dir", None)
        if direct:
            return str(direct)
        overrides = getattr(adapter, "_instance_overrides", None)
        if isinstance(overrides, dict):
            value = overrides.get("project_dir")
            if value:
                return str(value)
        if isinstance(params, dict):
            cwd_value = params.get("cwd")
            if isinstance(cwd_value, str) and cwd_value.strip():
                return cwd_value.strip()
            trusted_dirs = params.get("trusted_dirs")
            if isinstance(trusted_dirs, (list, tuple)) and trusted_dirs:
                first = str(trusted_dirs[0]).strip()
                if first:
                    return first
        return None

    def _resolve_active_is_code_agent(self, channel_id: str) -> bool:
        """Look up whether ``channel_id``'s adapter is the code-agent flavor.

        Mirrors :meth:`_resolve_active_project_dir`'s adapter lookup so the
        three sandbox call sites (``_dry_run_files_policy``,
        ``_handle_sandbox_files_set`` / ``_remove``'s ``find_auto_managed_
        match``, ``_attach_effective_sandbox_files``'s
        ``list_effective_sandbox_files``) all hand the same flag into
        ``sysop_builder``. Without this, the dry-run / display side would
        always assume non-code-agent and mismatch the actual mount layout
        a Code adapter produces at sandbox-start time (project_dir vs
        ``get_agent_workspace_dir``).

        Returns ``False`` on any failure path (no agent, no adapter, attr
        absent) — that matches the base class default and keeps the dry-run
        / display strictly aligned with what :class:`JiuWenSwarmDeepAdapter`
        emits when ``_is_code_agent`` was never set.
        """
        try:
            agent = self._agent_manager.get_agent_nowait(channel_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[command.sandbox] is_code_agent lookup: get_agent_nowait failed: %s", exc)
            return False
        adapter = self._resolve_adapter(agent)
        if adapter is None:
            return False
        return bool(getattr(adapter, "_is_code_agent", False))

    @staticmethod
    def _effective_files_from_adapter(adapter: Any) -> dict[str, list[dict[str, str]]] | None:
        """Read effective sandbox file mounts from the adapter's active sysop card."""
        card = getattr(adapter, "_sys_operation_card", None)
        if card is None:
            return None
        gateway_config = getattr(card, "gateway_config", None)
        launcher = getattr(gateway_config, "launcher_config", None) if gateway_config else None
        extra_params = getattr(launcher, "extra_params", None) if launcher else None
        if not isinstance(extra_params, dict):
            return None
        policy = extra_params.get("policy")
        if not isinstance(policy, dict):
            return None
        return effective_files_from_policy(policy)

    def _attach_effective_sandbox_files(
        self,
        payload: dict[str, Any],
        channel_id: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        """Inject ``effective_files`` into the ``/sandbox`` response payload.

        Prefer the filesystem policy cached on the active adapter's sysop card
        (same payload jiuwenbox uses at exec time). Fall back to a fresh build
        when no matching agent/sysop exists yet.
        """
        try:
            project_dir = self._resolve_active_project_dir(channel_id, params)
            adapter = None
            try:
                agent = self._agent_manager.get_agent_nowait(
                    channel_id,
                    project_dir=project_dir,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[command.sandbox] get_agent_nowait failed: %s", exc)
                agent = None
            if agent is not None:
                adapter = self._resolve_adapter(agent)
            if adapter is not None:
                adapter_project_dir = getattr(adapter, "_project_dir", None)
                if (
                    project_dir
                    and adapter_project_dir
                    and str(adapter_project_dir) != str(project_dir)
                ):
                    logger.warning(
                        "[command.sandbox] project_dir mismatch for effective_files: "
                        "client=%r adapter=%r",
                        project_dir,
                        adapter_project_dir,
                    )
                cached = self._effective_files_from_adapter(adapter)
                if cached is not None:
                    payload["effective_files"] = cached
                    return

            files_runtime: dict[str, Any] | None = None
            runtime = payload.get("runtime")
            if isinstance(runtime, dict):
                rt_files = runtime.get("files")
                if isinstance(rt_files, dict):
                    files_runtime = rt_files
            if files_runtime is None:
                files_in_payload = payload.get("files")
                if isinstance(files_in_payload, dict):
                    files_runtime = files_in_payload
            if files_runtime is None:
                files_runtime = get_sandbox_runtime().get("files") or {}
            is_code_agent = self._resolve_active_is_code_agent(channel_id)
            payload["effective_files"] = list_effective_sandbox_files(
                files_runtime,
                project_dir=project_dir,
                is_code_agent=is_code_agent,
                startup_mode=get_sandbox_startup_mode(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[command.sandbox] attach effective_files failed: %s", exc)

    @staticmethod
    def _read_landlock_compatibility(policy_path: Path | None) -> str:
        if policy_path is None or not policy_path.is_file():
            return "best_effort"
        try:
            import yaml

            data = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                landlock = data.get("landlock")
                if isinstance(landlock, dict):
                    compat = landlock.get("compatibility")
                    if isinstance(compat, str) and compat.strip():
                        return compat.strip()
        except Exception as exc:
            logger.debug("[command.sandbox] read landlock compatibility failed: %s", exc)
        return "best_effort"

    async def _attach_landlock_status(self, payload: dict[str, Any]) -> None:
        """Attach jiuwenbox Landlock capability summary to sandbox responses."""
        try:
            endpoint = get_sandbox_endpoint()
            jb = payload.get("jiuwenbox")
            if isinstance(jb, dict) and jb.get("host") and jb.get("port"):
                host = str(jb["host"])
                port = int(jb["port"])
            else:
                url = endpoint.get("url") or "http://127.0.0.1:8321"
                host, port = self._parse_sandbox_host_port(url)

            health = await self._jiuwenbox_runner.fetch_health(host, port)
            landlock_supported = bool(health.get("landlock_supported")) if health else False

            policy_file = endpoint.get("policy_file") or DEFAULT_SANDBOX_POLICY_FILE
            policy_path = resolve_sandbox_policy_path(policy_file)
            compatibility = self._read_landlock_compatibility(policy_path)

            payload["landlock"] = {
                "supported": landlock_supported,
                "compatibility": compatibility,
            }
        except Exception as exc:
            logger.warning("[command.sandbox] attach landlock status failed: %s", exc)

    async def _apply_sandbox_runtime_patch(
        self, channel_id: str, runtime: dict[str, Any], *, files_changed: bool
    ) -> None:
        agent = self._agent_manager.get_agent_nowait(channel_id)
        adapter = self._resolve_adapter(agent)
        if adapter is None or not hasattr(adapter, "apply_sandbox_runtime_patch"):
            return
        try:
            await adapter.apply_sandbox_runtime_patch(runtime, files_changed=files_changed)
        except (FileNotFoundError, ValueError) as exc:
            raise ValueError(str(exc)) from exc
        except Exception as exc:
            logger.warning("[command.sandbox] apply_sandbox_runtime_patch failed: %s", exc)

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

    @staticmethod
    def _tenant_pool() -> TenantAgentPool:
        return TenantAgentPool.get_instance()

    async def send_push(self, msg) -> int:
        """AgentServer 主动向 Gateway 推送消息。

        payload 格式与 AgentResponse.payload 一致，
        可含 event_type 等字段供 Gateway 转为 Message 派发到 Channel。

        扇出给 :class:`PushRegistry` 里的全部订阅者：Gateway WS 连接与经
        ``GET /api/v1/events/stream`` 订阅的 HTTP 客户端都在其中，推送只有这一条
        路径。有订阅者即送达，一个都没有则记 warning。

        Returns:
            成功送达的订阅者数量。``0`` 表示未投递（无订阅者 / 编码失败 /
            过滤后无人匹配 / sink 全部失败）。调用方（如 ``send_file_to_user``）
            应用此返回值判定成败，勿再把「仅打了 warning」当成发送成功。
        """
        registry = get_push_registry()
        response_kind = str(msg.get("response_kind") or "").strip()
        is_reverse_rpc = response_kind == E2A_RESPONSE_KIND_ACP_OUTPUT_REQUEST
        if (
            not registry.reverse_rpc_ready()
            if is_reverse_rpc
            else registry.subscriber_count() == 0
        ):
            # 一个去处都没有：保持原有告警与早退（连 wire 都不构造）。
            logger.warning(
                "[AgentWebSocketServer] send_push 失败: 无活跃 Gateway 连接 "
                "session_id=%s request_id=%s event_type=%s",
                msg.get("session_id", ""),
                msg.get("request_id", ""),
                (msg.get("payload") or {}).get("event_type", "")
                if isinstance(msg.get("payload"), dict)
                else "",
            )
            return 0

        try:
            wire = build_server_push_wire(msg)
        except Exception as e:
            logger.warning("[AgentWebSocketServer] send_push 失败: %s", e)
            return 0

        delivered = (
            await registry.push_reverse_rpc(wire)
            if is_reverse_rpc
            else await registry.push(wire)
        )

        if delivered == 0:
            # 两种情况都会落到这里：内容过大被降级成错误帧（sink 返回 False），
            # 以及订阅者按 session/channel 过滤后无人匹配。
            logger.warning(
                "[AgentWebSocketServer] send_push 无订阅者成功接收"
                "（内容过大降级为错误帧，或订阅者过滤后无人匹配）: channel_id=%s",
                msg.get("channel_id", ""),
            )
            return 0

        if response_kind:
            logger.info(
                "[AgentWebSocketServer] send_push response_kind wire sent: "
                "channel_id=%s kind=%s delivered=%d",
                msg.get("channel_id", ""),
                response_kind,
                delivered,
            )
        else:
            logger.info(
                "[AgentWebSocketServer] send_push 已发送(E2A wire): channel_id=%s delivered=%d",
                msg.get("channel_id", ""),
                delivered,
            )
        return delivered

    def get_agent(self):
        """获取 default agent 实例（向后兼容）."""
        return self._agent_manager.get_agent_nowait()

    def get_agent_manager(self) -> AgentManager:
        """获取 AgentManager 实例."""
        return self._agent_manager

    async def handle_acp_tool_response_for_test(
            self,
            ws: Any,
            request: AgentRequest,
            send_lock: asyncio.Lock,
    ) -> None:
        """Public test helper that delegates to ACP tool-response handling."""
        # 只有 handlers.bootstrap 需要延迟导入（模块级导入会成环）；
        # RequestContext / AgentServerServices / WSSink 模块级已导入，重复导入会遮蔽外层名字。
        from jiuwenswarm.server.handlers.bootstrap import handle_acp_tool_response

        await handle_acp_tool_response(
            RequestContext(
                request=request,
                sink=WSSink(ws, send_lock),
                connection_id=str(id(ws)),
                services=AgentServerServices(self),
            )
        )

    def is_working(self) -> bool:
        """返回 Agent 是否正在工作.

        用于沙箱保活校验。

        Returns:
            bool: 是否正在工作
        """
        return self._agent_manager.is_working()

    def _build_model_cache(self) -> None:
        """Build model cache from jiuwenswarm config.yaml (reuse interface_deep logic)."""
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

        config = get_config()

        # Use the same model building method as interface_deep
        build_model_from_entry = getattr(JiuWenSwarmDeepAdapter, '_build_model_from_entry')

        # Build from models.defaults list
        for entry in get_default_models(config):
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
