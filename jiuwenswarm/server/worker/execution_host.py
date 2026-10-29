# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from jiuwenswarm.common.schema.agent import AgentRequest

from typing import Protocol
from jiuwenswarm.server.worker.execution_requests import ExecutionRequests

from jiuwenswarm.server.management.facade import ManagementFacade

import logging
from jiuwenswarm.server.runtime.agent_manager import AgentManager
from jiuwenswarm.runtime import AgentRuntime
from typing import Any
from jiuwenswarm.common.config import DEFAULT_SANDBOX_POLICY_FILE
from jiuwenswarm.agents.harness.code.rails.heartbeat.runtime import HeartbeatRailRuntime
from typing import Optional
from jiuwenswarm.runtime.plan import PlanModeController
from jiuwenswarm.server.runtime.session.session_message_store import (
    SessionMessageRecord,
)
from jiuwenswarm.server.runtime.session.session_message_service import (
    SessionMessageService,
)
from jiuwenswarm.server.runtime.session.session_message_store import SessionMessageStore
import asyncio
from jiuwenswarm.agents.harness.common.tools.acp_output_tools import (
    get_acp_output_manager,
)
from jiuwenswarm.common.utils import get_agent_root_dir
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.config import get_sandbox_endpoint
from jiuwenswarm.common.config import get_sandbox_startup_mode_explicit
from jiuwenswarm.runtime.host_services import install_runtime_push_handler
from jiuwenswarm.agents.harness.common.auto_harness import reset_harness_packages_state
from jiuwenswarm.common.config import resolve_sandbox_api_token
from jiuwenswarm.common.config import resolve_sandbox_policy_path
from jiuwenswarm.runtime.host_services import restore_runtime_push_handler
from jiuwenswarm.common.config import sync_sandbox_api_token_environ
import sys
import time
from jiuwenswarm.common.config import update_sandbox_endpoint
from jiuwenswarm.common.config import update_sandbox_runtime
from jiuwenswarm.server.worker.execution_services import (
    HostServices,
    ProductionHostServices,
)

logger = logging.getLogger("jiuwenswarm.server.agent_ws_server")
_PUSH_NO_GATEWAY_WARN_INTERVAL_S = 30.0


async def _reuse_server_runtime_dependencies() -> None:
    """Compatibility Runtime wrappers borrow dependencies owned by this host."""


class EventSink(Protocol):
    """Resident event delivery boundary, independent of a Front connection."""

    async def send(self, message: str | bytes) -> None:
        """Record and deliver an encoded event."""


class WorkerExecutionHost(ExecutionRequests, ManagementFacade):
    """Resident Runtime owner; Front owns all socket listening and reconnects."""

    @property
    def services(self) -> HostServices:
        """Host-owned integrations; lazy default also supports legacy fixtures."""
        value = getattr(self, "_host_services", None)
        if value is None:
            value = ProductionHostServices()
            self._host_services = value
        return value

    @property
    def _tokenizer_service(self) -> Any:
        if getattr(self, "_tokenizer_service_instance", None) is None:
            from jiuwenswarm.server.runtime.tokenizer_service import TokenizerService

            self._tokenizer_service_instance = TokenizerService()
        return self._tokenizer_service_instance

    @_tokenizer_service.setter
    def _tokenizer_service(self, service: Any) -> None:
        self._tokenizer_service_instance = service

    @property
    def _jiuwenbox_runner(self) -> Any:
        if getattr(self, "_jiuwenbox_runner_instance", None) is None:
            from jiuwenswarm.server.sandbox.jiuwenbox_runner import JiuwenBoxRunner

            self._jiuwenbox_runner_instance = JiuwenBoxRunner.instance()
        return self._jiuwenbox_runner_instance

    @_jiuwenbox_runner.setter
    def _jiuwenbox_runner(self, runner: Any) -> None:
        self._jiuwenbox_runner_instance = runner

    @property
    def _personal_context_host(self) -> Any:
        if getattr(self, "_personal_context_host_instance", None) is None:
            from jiuwenswarm.server.personal_context import PersonalContextHostAPI
            from jiuwenswarm.common.utils import get_user_workspace_dir

            self._personal_context_host_instance = PersonalContextHostAPI(
                home=get_user_workspace_dir() / ".personal_context",
            )
        return self._personal_context_host_instance

    @_personal_context_host.setter
    def _personal_context_host(self, host: Any) -> None:
        self._personal_context_host_instance = host

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 18000,
        *,
        services: HostServices | None = None,
        plan_controller: PlanModeController | None = None,
    ) -> None:
        self._host_services = (
            services if services is not None else ProductionHostServices()
        )
        self._host = host
        self._port = port
        self._server: Any = None
        # 当前 Gateway 连接，用于 send_push 主动推送
        self._current_ws: Any = None
        self._current_send_lock: asyncio.Lock | None = None
        # send_push 无连接告警限流：首条立即告警，之后按窗口汇总
        self._push_no_gateway_suppressed = 0
        self._push_no_gateway_warned_at: float | None = None
        self._acp_client_capabilities_by_ws: dict[int, dict[str, Any]] = {}
        # AgentServer and the process CLI share this transport-independent Runtime.
        # Keep the manager alias for legacy transport handlers.
        self._plan_controller = (
            plan_controller if plan_controller is not None else PlanModeController()
        )
        self._runtime = self._build_runtime(plan_controller=self._plan_controller)
        self._agent_manager = self._runtime.agent_manager
        self._runtime_push_handler = None
        self._previous_runtime_push_handler = None
        self._runtime_services_started = False
        self._group_service = None
        # RSI 服务域分发句柄（懒加载，见 _get_rsi_handlers）
        self._rsi_handlers = None
        # Trajectory diagnosis host (lazy, see _get_diagnosis_host)
        self._diagnosis_host: Any = None
        # Trajectory read host (lazy, see _get_trajectory_host)
        self._trajectory_host: Any = None
        # Topic recommendation host (lazy, see _recommendation_host_instance)
        self._recommendation_host: Any = None
        # Optional production Provider injection point.  The concrete class is
        # supplied by the composition root once it is available.
        self._rsi_harness_provider: Any = None
        self._heartbeat_runtime = HeartbeatRailRuntime(self)
        self._runtime.set_admission_controller(self._heartbeat_runtime.admission)
        self._runtime.set_session_delete_lifecycle(self._heartbeat_runtime)
        self._agent_manager.set_heartbeat_service(self._heartbeat_runtime)
        self._install_session_message_service()
        from jiuwenswarm.server.worker.management_adapters import LazyAdapterRegistry
    
        self._adapter_registry = LazyAdapterRegistry(self)
        self._tokenizer_service_instance = None
        # Tokenizer downloads are best-effort background work. Context creation
        # is local-only and falls back to string length while these tasks run.
        self._tokenizer_warmup_tasks: set[asyncio.Task] = set()
        self._owned_background_tasks: set[asyncio.Task] = set()
        self._background_stopping = False
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
        self._expert_stream_requests: dict[asyncio.Task, AgentRequest] = {}
        # Scheduler service instance (for scheduled auto_harness tasks)
        self._scheduler_service: Any = None
        self._scheduler_agent: Any = None
        # Model cache for scheduled task execution (same approach as interface_deep)
        self._model_cache: dict[str, Any] = {}
        self._default_model: Optional[Any] = None
        # 本地 jiuwenbox 子进程管理器 (lazy 启动, 在 /sandbox enable 时 ensure_running)
        self._jiuwenbox_runner_instance = None
        # AgentServer 内唯一持有的进程内 PersonalContext Host；Context Rail 使用同一固定目录。
        self._personal_context_host_instance = None
        self._personal_context_start_task: asyncio.Task[None] | None = None
        self._sandbox_start_task: asyncio.Task[None] | None = None
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
        self._on_runtime_capabilities: Any = None
        self._on_execution_min_ready: Any = None
        self._execution_minimum_observed = False
        self._preparation_registry: Any = None
        self._startup_preparation_tokens: set = set()
        self.proactive_preparation_task: asyncio.Task | None = None
        get_acp_output_manager().set_send_push_callback(
            self._schedule_acp_push
        )

    def spawn_background(self, coroutine: Any, *, name: str) -> asyncio.Task:
        """Keep finite management/notification work with this resource owner."""
        if getattr(self, "_background_stopping", False):
            coroutine.close()
            raise RuntimeError("execution host is stopping")
        tasks = getattr(self, "_owned_background_tasks", None)
        if tasks is None:
            tasks = self._owned_background_tasks = set()
        task = asyncio.create_task(coroutine, name=name)
        tasks.add(task)

        def completed(value: asyncio.Task) -> None:
            tasks.discard(value)
            if not value.cancelled() and (error := value.exception()) is not None:
                logger.warning("[Worker] background task %s failed: %s", name, type(error).__name__)

        task.add_done_callback(completed)
        return task

    def _schedule_acp_push(self, message: dict[str, Any]) -> Any:
        if getattr(self, "_background_stopping", False):
            return None
        return self.spawn_background(self.send_push(message), name="acp-output-push")

    async def _close_background_tasks(self) -> None:
        self._background_stopping = True
        tasks = [task for task in getattr(self, "_owned_background_tasks", ())
                 if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

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
        on_capabilities: Any = None,
        on_execution_min_ready: Any = None,
    ) -> None:
        """Front readiness callbacks. ``on_ready`` is Agent Runtime start, not attach.
    
        Retryable warmup errors use ``on_warmup_retry`` (stay ``RUNTIME_WARMING``).
        ``on_failed`` is only for unrecoverable startup that stops retrying.
        """
        self._on_runtime_ready = on_ready
        self._on_runtime_warmup_retry = on_warmup_retry
        self._on_runtime_failed = on_failed
        self._on_runtime_capabilities = on_capabilities
        self._on_execution_min_ready = on_execution_min_ready

    def observe_execution_minimum_ready(self) -> None:
        """The first request's model, safety Rails and context are prepared.

        Runtime.start only releases legacy dispatch admission. This observation
        comes from the prepared execution entry, never from a queue ACK, and
        avoids predicting the first request's mode with a spare Agent.
        """
        if self._execution_minimum_observed:
            return
        self._execution_minimum_observed = True
        preparation = getattr(self, "_preparation_registry", None)
        if preparation is not None:
            for token in getattr(self, "_startup_preparation_tokens", set()):
                preparation.resume_background(token)
            self._startup_preparation_tokens = set()
        callback = self._on_execution_min_ready
        if callable(callback):
            callback()

    def set_rsi_harness_provider(self, provider: Any) -> None:
        """Install the production ``HarnessProvider`` at the RSI seam."""
        self._rsi_harness_provider = provider
        handlers = self._rsi_handlers
        if handlers is not None:
            handlers.context.register_harness_provider(provider)

    @staticmethod
    def _ws_capabilities_key(ws: Any) -> int:
        return id(ws)

    def _set_ws_acp_client_capabilities(
        self, ws: Any, capabilities: dict[str, Any] | None
    ) -> None:
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

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    def _schedule_tokenizer_warmup(
        self,
        config: dict[str, Any] | None,
        *,
        reason: str,
    ) -> asyncio.Task[bool]:
        """Schedule a non-blocking tokenizer warm-up task.
    
        The service-level lock deduplicates overlapping resolutions. Keeping
        each task until completion lets a reload submit its newest config
        without cancelling an in-flight worker-thread download.
        """
    
        async def _run() -> bool:
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
                return not result.get("failed", 0)
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
                return False
    
        task = asyncio.create_task(_run(), name=f"tokenizer-warmup:{reason}")
        self._tokenizer_warmup_tasks.add(task)
        task.add_done_callback(self._tokenizer_warmup_tasks.discard)
        return task

    @staticmethod
    async def _start_symphony_recovery() -> bool:
        """Recover Flow candidates without affecting AgentServer availability."""

        try:
            # Disabled Flow still supports Skill discovery through the toolkit;
            # it does not need the heavy recovery service at process startup.
            from jiuwenswarm.symphony.config import (
                evolution_flow_enabled,
                load_symphony_config,
            )

            config = await asyncio.to_thread(load_symphony_config)
            if not config.enabled or not evolution_flow_enabled(config):
                return True
            import importlib

            service_module = await asyncio.to_thread(
                importlib.import_module, "jiuwenswarm.symphony.service"
            )
            get_swarm_symphony_service = service_module.get_swarm_symphony_service

            await get_swarm_symphony_service().start()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AgentWebSocketServer] Symphony Flow recovery failed: %s",
                exc,
            )
            return False

    def bind_gateway_transport(self, ws: Any, send_lock: asyncio.Lock) -> None:
        """Bind the resident event sink without activating mailbox execution."""
        self.bind_event_sink(ws, send_lock)

    def bind_event_sink(self, ws: EventSink, send_lock: asyncio.Lock) -> None:
        """Attach delivery independently of the lifetime of Runtime execution."""
        self._current_ws = ws
        self._current_send_lock = send_lock

    async def attach_gateway_connection(
        self,
        ws: Any,
        send_lock: asyncio.Lock,
    ) -> None:
        """Publish the Front-owned Gateway socket for send_push / ACP caps."""
        self.bind_gateway_transport(ws, send_lock)
        self._reset_push_no_gateway_warning()
        await self._session_message_service.set_available(True)
        try:
            from jiuwenswarm.observability.gateway_hints import trajectory_gateway_hint_bridge
    
            trajectory_gateway_hint_bridge.notify_available()
        except Exception:
            logger.debug("[AgentWebSocketServer] trajectory hint wakeup failed", exc_info=True)

    async def _bind_transport(self) -> None:
        raise ValueError("WorkerExecutionHost does not own a listen socket")

    async def start(self, *, bind_transport: bool = False) -> None:
        """Start execution resources; listening remains exclusively with Front."""
        if (
            bind_transport
            and type(self)._bind_transport is WorkerExecutionHost._bind_transport
        ):
            raise ValueError("WorkerExecutionHost does not own a listen socket")
        if self._runtime.closed:
            raise RuntimeError("WorkerExecutionHost has been closed")
        if self._runtime_services_started:
            logger.warning("[AgentWebSocketServer] Runtime 服务已在运行")
            return

        owner = self._kv_cache_application_owner
        await owner.activate_from_config()

        from jiuwenswarm.server.worker.preparation import PreparationRegistry

        self._preparation_registry = PreparationRegistry(
            on_change=getattr(self, "_on_runtime_capabilities", None),
            background_concurrency=1,
        )
        preparation = self._preparation_registry
        preparation.bind_current_loop()
        self._execution_minimum_observed = False

        from jiuwenswarm.server.runtime.session.session_archive import (
            SessionArchiveService,
        )
        self._archive_service = SessionArchiveService(self._execution_runtime())
        preparation.register("archive", self._archive_service.start_recovery)

        # Reset harness package state to native on service startup
        reset_harness_packages_state()

        if bind_transport:
            await self._bind_transport()
        else:
            self._server = None
        self._runtime_services_started = True
        await self._start_recommendation_host()
        self._runtime_push_handler = self.send_push
        self._previous_runtime_push_handler = install_runtime_push_handler(
            self._runtime_push_handler
        )
        logger.info("[AgentWebSocketServer] 已启动: ws://%s:%s", self._host, self._port)

        # Skill inventory and Hub catalog reads have their own lazy paths.
        # Publication recovery is required before a publish query, not before
        # ordinary execution. Keep its capability pending until actual use.
        preparation.register("assets", self._start_asset_services, warm=False)

        # The port is already listening. Remote tokenizer downloads must not
        # delay startup; ContextEngine is local-only and uses string fallback
        # if this task has not completed when the first context is created.
        async def _prepare_tokenizers() -> bool:
            return await self._schedule_tokenizer_warmup(get_config(), reason="startup")

        preparation.register("tokenizers", _prepare_tokenizers)

        # 端口已 listen, 后台预热 checkpointer, 不阻塞启动与握手.
        # _checkpointer_warmup_task 供 shutdown 时 cancel, 避免任务悬挂.
        async def _start_runtime() -> None:
            retry_delay = 1.0
            while True:
                try:
                    phase_started = time.monotonic()
                    await self._runtime.start()
                    logger.info(
                        "[Worker] preparation stage=runtime_start duration_ms=%.3f",
                        (time.monotonic() - phase_started) * 1000,
                    )
                    phase_started = time.monotonic()
                    await self._heartbeat_runtime.start()
                    logger.info(
                        "[Worker] preparation stage=heartbeat_start duration_ms=%.3f",
                        (time.monotonic() - phase_started) * 1000,
                    )
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
                # Optional imports, downloads and recovery may start only after
                # the required Runtime/Heartbeat initialization succeeded.
                preparation.mark_minimum_ready()
                return

        self._checkpointer_warmup_task = asyncio.create_task(
            _start_runtime(), name="runtime-start"
        )

        async def _warmup_mcp_connections() -> bool:
            try:
                from jiuwenswarm.common.mcp_config import prewarm_connected_mcps

                return await prewarm_connected_mcps(report_status=True) is not False
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[AgentWebSocketServer] MCP prewarm failed "
                    "(will lazy-connect on first chat): %s",
                    exc,
                )
                return False

        # 端口已 listen, 后台预热 connected MCP 的进程级连接缓存, 不阻塞启动与握手.
        preparation.register(
            "mcp", _warmup_mcp_connections, imports=("jiuwenswarm.common.mcp_config",)
        )
        self._mcp_prewarm_task = preparation.start("mcp")
        preparation.register(
            "personal_context", self._start_personal_context_best_effort
        )
        self._personal_context_start_task = preparation.start("personal_context")
        preparation.register(
            "symphony",
            self._start_symphony_recovery,
            imports=("jiuwenswarm.symphony.config",),
        )
        # 登录模型的 id_token 一小时过期：在用且快过期的，请Gateway续期后推回来

        async def _start_login_refresh() -> None:
            from jiuwenswarm.common.auth.login_credentials import run_refresh_requests
            from jiuwenswarm.common.auth.remote_config import warm_up_in_background

            warm_up_in_background()
            self._login_credential_refresh_task = asyncio.create_task(
                run_refresh_requests(self.send_push),
                name="login-credential-refresh",
            )

        preparation.register(
            "login_refresh",
            _start_login_refresh,
            imports=(
                "jiuwenswarm.common.auth.login_credentials",
                "jiuwenswarm.common.auth.remote_config",
            ),
        )
        # WS 监听已经开放, 现在按 config.yaml::sandbox 的 runtime.enabled +
        # startup_mode 决定要不要自动把 jiuwenbox 子进程也拉起来。失败不阻塞
        # 启动 (用户依然可以在 TUI 里跑 /sandbox enable 重试)。
        preparation.register("sandbox", self._bootstrap_internal_jiuwenbox)
        self._sandbox_start_task = preparation.start("sandbox")

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
            coro = warm_image_modality_cache(
                get_config(), reason=reason, report_status=True
            )
        self._image_modality_refresh_task = asyncio.create_task(
            coro, name=f"image-modality-warmup-{reason}"
        )

    async def _start_personal_context_best_effort(self) -> bool:
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
            return False
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
            return False
        if rail_sync_cancelled is not None:
            raise rail_sync_cancelled
        return True

    async def _bootstrap_internal_jiuwenbox(self) -> bool | None:
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
            # 非 Linux 平台直接跳过 auto-start: jiuwenbox 依赖 bwrap / Landlock /
            # 命名空间, Windows / macOS 起不来; 即便 spawn 成功后续 /sandbox 命
            # 令也会被 :func:`_require_sandbox_supported` 拒掉, 留着只会浪费一
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
                return False
    
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
                return False
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
                return False
    
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
                "[AgentWebSocketServer] jiuwenbox auto-started at %s (policy=%s)",
                url,
                policy_path,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "[AgentWebSocketServer] jiuwenbox auto-start raised an "
                "unexpected error; skipping (用户可在 TUI 里 /sandbox enable 重试)"
            )
            return False

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
            context = self._personal_context_host_instance
            if context is not None:
                await context.stop()
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
        """Close this one-shot execution owner without acquiring new resources.

        Rejected Runtime close retains its owner so an unfinished provision can
        still be finalized before shutdown is retried.
        """
        try:
            await self._stop_main_services()
        finally:
            await self._stop_personal_context_best_effort()

    async def _stop_main_services(self) -> None:
        """Stop AgentServer-owned services before optional host cleanup."""
        await self._stop_recommendation_host()
        await self._close_background_tasks()
        preparation = getattr(self, "_preparation_registry", None)
        if preparation is not None:
            await preparation.close()
            self._preparation_registry = None
        proactive = getattr(self, "_proactive_engine", None)
        if proactive is not None:
            close = getattr(proactive, "close", None)
            if callable(close):
                await close()
            self._proactive_engine = None
        sandbox_start = getattr(self, "_sandbox_start_task", None)
        self._sandbox_start_task = None
        if sandbox_start is not None:
            sandbox_start.cancel()
            await asyncio.gather(sandbox_start, return_exceptions=True)
        diagnosis_host = getattr(self, "_diagnosis_host", None)
        if diagnosis_host is not None:
            # A restart builds a fresh host so the shutdown flag does not linger.
            self._diagnosis_host = None
            try:
                await diagnosis_host.shutdown()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[AgentWebSocketServer] diagnosis shutdown failed: %s", exc)
        task = getattr(self, "_asset_start_task", None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._asset_start_task = None
        api = getattr(self, "_asset_publish_api", None)
        if api is not None:
            await api.close()
            self._asset_publish_api = None
        from jiuwenswarm.server.runtime.marketplace.hub_catalog_cache import (
            close_hub_catalog_cache,
        )
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
        from jiuwenswarm.agents.harness.common.recommendation.prompt_suggestion import (
            forks,
        )

        # Shutdown must keep going: a failing fork teardown may not strand the services below.
        try:
            await forks.shutdown()
        except Exception:  # noqa: BLE001
            logger.warning("[AgentWebSocketServer] forks.shutdown() failed", exc_info=True)
        session_message_service = getattr(self, "_session_message_service", None)
        if session_message_service is not None:
            await session_message_service.stop()
        await self._stop_scheduler()
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
                logger.warning(
                    "[AgentWebSocketServer] checkpointer warmup cancel failed: %s", exc
                )
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
                logger.warning(
                    "[AgentWebSocketServer] MCP prewarm cancel failed: %s", exc
                )
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
                    "[AgentWebSocketServer] image modality refresh cancel failed: %s",
                    exc,
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
            # Worker owners are one-shot. A rejected close retains the exact
            # Runtime so outstanding provision leases can still be finalized.
            # Never allocate a replacement Runtime while shutting down.
            if runtime_close_completed or closing_runtime.closed:
                self._runtime_services_started = False
    
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
            if self._jiuwenbox_runner_instance is not None:
                await self._jiuwenbox_runner_instance.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AgentWebSocketServer] jiuwenbox_runner.stop failed: %s", exc
            )
        if runtime_close_error is not None and (
            not isinstance(runtime_close_error, Exception) or not closing_runtime.closed
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
            logger.warning(
                "KVC application suspend failed; continue shutdown", exc_info=True
            )

    def _build_runtime(
        self,
        *,
        plan_controller: Any,
        agent_manager: Any | None = None,
        reuse_dependencies: bool = False,
    ) -> AgentRuntime:
        """Build every AgentServer Runtime through one product composition root."""
        from jiuwenswarm.runtime.session_lifecycle import RuntimeParticipantRegistry
        from jiuwenswarm.runtime.team_controller import process_team_controller
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
            team_execution_controller=process_team_controller(),
        )

    def _execution_runtime(self) -> AgentRuntime:
        runtime = getattr(self, "_runtime", None)
        manager = getattr(self, "_agent_manager", None)
        if runtime is None or runtime.agent_manager is not manager:
            runtime = self._build_runtime(
                agent_manager=manager,
                plan_controller=self._plan_controller,
                reuse_dependencies=True,
            )
            self._runtime = runtime
            runtime.set_session_message_service(
                getattr(self, "_session_message_service", None)
            )
        return runtime

    async def send_push(self, msg) -> bool:
        """AgentServer 主动向 Gateway 推送消息。
    
        payload 格式与 AgentResponse.payload 一致，
        可含 event_type 等字段供 Gateway 转为 Message 派发到 Channel。
        """
        if self._current_ws is None or self._current_send_lock is None:
            self._warn_push_without_gateway()
            return False
    
        try:
            payload = msg.get("payload") if isinstance(msg, dict) else None
            if isinstance(payload, dict) and payload.get("event_type") == "chat.error":
                # 集群在没有进行中的请求时（自主轮次）失败，走的是这条推送
                msg = {**msg, "payload": dict(payload)}
                self._annotate_model_error(msg["payload"], msg.get("session_id"))
            wire = self.services.push_payload(msg)
            async with self._current_send_lock:
                sent_original = await self.services.send(self._current_ws, wire)
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

    def reset_model_cache(self) -> None:
        """清空模型缓存,下次 _resolve_model 触发懒重建。

        供 Zen 免费模型就绪回调使用:预热异步化后首个请求可能早于 Zen 拉取
        完成构建不含 Zen 条目的缓存(一次性、不自动重建),就绪后清空即可让
        重建带上 Zen 免费模型及占位符默认模型的 Zen 兜底。
        """
        if self._model_cache:
            self._model_cache.clear()
        self._default_model = None

    def _get_diagnosis_host(self) -> Any:
        """Lazily build the trajectory diagnosis host once."""
        if getattr(self, "_diagnosis_host", None) is None:
            from jiuwenswarm.server.diagnosis.host import DiagnosisHost

            self._diagnosis_host = DiagnosisHost(
                get_agent_manager=lambda: self._agent_manager,
                send_push=self.send_push,
            )
        return self._diagnosis_host

    async def _handle_diagnosis_request(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Answer one ``diagnosis.*`` request proxied by the Gateway."""
        from jiuwenswarm.common.schema.agent import AgentResponse

        try:
            resp = await self._get_diagnosis_host().handle(request)
        except Exception as exc:
            logger.exception("[AgentServer] diagnosis request failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "INTERNAL_ERROR"},
            )

        wire = self.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await self.services.send(ws, wire)

    def _reset_push_no_gateway_warning(self) -> None:
        suppressed = getattr(self, "_push_no_gateway_suppressed", 0)
        if suppressed:
            logger.info(
                "[AgentWebSocketServer] Gateway 连接已恢复，此前 %d 条 send_push 失败告警已省略",
                suppressed,
            )
        self._push_no_gateway_suppressed = 0
        self._push_no_gateway_warned_at = None

    def _warn_push_without_gateway(self) -> None:
        """无活跃 Gateway 连接时限流告警，避免生产者重试刷屏。"""
        now = time.monotonic()
        warned_at = getattr(self, "_push_no_gateway_warned_at", None)
        suppressed = getattr(self, "_push_no_gateway_suppressed", 0)
        if warned_at is not None and now - warned_at < _PUSH_NO_GATEWAY_WARN_INTERVAL_S:
            self._push_no_gateway_suppressed = suppressed + 1
            return
        self._push_no_gateway_warned_at = now
        self._push_no_gateway_suppressed = 0
        if suppressed:
            logger.warning(
                "[AgentWebSocketServer] send_push 失败: 无活跃 Gateway 连接"
                "（过去 %.0fs 内另有 %d 条同类失败已省略）",
                _PUSH_NO_GATEWAY_WARN_INTERVAL_S,
                suppressed,
            )
        else:
            logger.warning(
                "[AgentWebSocketServer] send_push 失败: 无活跃 Gateway 连接"
            )

    def get_group_service(self):
        """Return the lazily initialized group management service."""
        from jiuwenswarm.server.runtime.group_service import GroupService

        service = getattr(self, "_group_service", None)
        if service is None:
            service = self._group_service = GroupService(self._execution_runtime())
        return service
