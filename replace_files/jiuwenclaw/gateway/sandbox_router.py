from __future__ import annotations

import asyncio
import os
import secrets
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from jiuwenclaw.sandbox.sandbox_client import SandboxClient, SandboxConfig
from jiuwenclaw.sandbox.claw_api_key import get_claw_api_key
from jiuwenclaw.sandbox.sandbox_routing_settings import SandboxRoutingSettings
from jiuwenclaw.sandbox.sandbox_dcs_store import SandboxDcsStore
from jiuwenclaw.sandbox.sandbox_init_data import upload_sandbox_init_data
from jiuwenclaw.sandbox.sandbox_routing_dcs_store import (
    SandboxRoutingDcsStore,
    get_gateway_instance_id,
    sandbox_adopt_existing_enabled,
)
from jiuwenclaw.sandbox.sandbox_log_dcs_store import SandboxLogDcsStore
from jiuwenclaw.sandbox.workspace_dcs_store import WorkspaceDcsStore, WorkspaceRecord
from jiuwenclaw.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenclaw.e2a.models import E2AEnvelope
from jiuwenclaw.gateway.agent_client import AgentServerClient
from jiuwenclaw.gateway.open_ability_client import OpenAbilityWebSocketClient
from jiuwenclaw.sandbox.open_ability import (
    OpenAbilityConfig,
    OpenAbilityEndpoint,
    OpenAbilityReconnectTimeoutError,
    build_openability_ws_uri,
    format_openability_endpoint,
)
from jiuwenclaw.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenclaw.schema.message import ReqMethod
from jiuwenclaw.utils import logger
from jiuwenclaw.log import interface_info

_WORKSPACE_SKIP_METHODS = frozenset({
    ReqMethod.SKILLDEV_BATCH_UPLOAD.value,
    ReqMethod.SKILLDEV_BATCH_DOWNLOAD.value,
    ReqMethod.CHAT_CANCEL.value,
    ReqMethod.SKILLDEV_CANCEL.value,
})
_OPENABILITY_RECONNECT_DROP_METHODS = frozenset({
    ReqMethod.CHAT_CANCEL.value,
    ReqMethod.SKILLDEV_CANCEL.value,
})
_VIBESKILL_CHANNEL_ID = "vibeskill"
_SYSTEM_SESSION_ID_PREFIXES = ("__", "heartbeat_", "cron")
_SANDBOX_BACKUP_ENABLE_ENV = "SANDBOX_BACKUP_ENABLE"
_SANDBOX_BACKUP_PERIOD_SECONDS_ENV = "SANDBOX_BACKUP_PERIOD_SECONDS"
_SANDBOX_BACKUP_DEFAULT_PERIOD_SECONDS = 600.0
def _env_bool(name: str, *, default: bool) -> bool:
    raw = str(os.environ.get(name, "")).strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _env_float(name: str, *, default: float) -> float:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, using default %.1f", name, raw, default)
        return default


def _resolve_sandbox_max_limit(
    override: int | None,
    configured: int,
    *,
    param_name: str,
) -> int:
    if override is None:
        return configured
    if override <= 0:
        raise ValueError(f"{param_name} must be greater than 0, got {override}")
    return override


def _tracked_session_ids_from_metadata(metadata: dict[str, Any]) -> list[str]:
    """Session IDs associated with a runtime (multi-session backup/terminate context)."""
    raw = metadata.get("session_ids")
    if isinstance(raw, set):
        ids = [str(sid).strip() for sid in raw if str(sid).strip()]
    elif isinstance(raw, (list, tuple)):
        ids = [str(sid).strip() for sid in raw if str(sid).strip()]
    else:
        ids = []
    if ids:
        return sorted(ids)
    sid = str(metadata.get("session_id") or "").strip()
    return [sid] if sid else []


def _create_query_url_obs() -> Any:
    from jiuwenclaw.gateway.query_url_obs import QueryUrlOSMS

    return QueryUrlOSMS()


class SandboxStatus(str, Enum):
    INITIALIZING = "initializing"
    BUSY = "busy"
    IDLE = "idle"
    TERMINATING = "terminating"
    TERMINATED = "terminated"


@dataclass
class SandboxRuntime:
    routing_key: str
    sandbox_id: str
    agent_client: AgentServerClient
    status: SandboxStatus = SandboxStatus.INITIALIZING
    task_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)
    _duration_refresh_lock: asyncio.Lock | None = field(default=None, repr=False, compare=False)

    def duration_refresh_lock(self) -> asyncio.Lock:
        if self._duration_refresh_lock is None:
            self._duration_refresh_lock = asyncio.Lock()
        return self._duration_refresh_lock


class SandboxRouterAgentClient(AgentServerClient):
    def __init__(
        self,
        *,
        max_sandboxes: int | None = None,
        queue_max_size: int | None = None,
        queue_timeout_seconds: float | None = None,
        idle_timeout_seconds: float | None = None,
        idle_check_interval_seconds: float | None = None,
        adopt_existing: bool | None = None,
        gateway_instance_id: str | None = None,
        workspace_dcs_store: WorkspaceDcsStore | None = None,
        sandbox_log_dcs_store: SandboxLogDcsStore | None = None,
    ) -> None:
        settings = SandboxRoutingSettings.from_env()
        self._sandbox_client: SandboxClient | None = None
        self._dcs_store: SandboxDcsStore | None = None
        self._routing_dcs_store: SandboxRoutingDcsStore | None = None
        self._workspace_dcs_store = workspace_dcs_store
        self._sandbox_log_dcs_store = sandbox_log_dcs_store
        self._open_ability_config: OpenAbilityConfig | None = None
        self._max_sandboxes = _resolve_sandbox_max_limit(
            max_sandboxes,
            settings.max_sandboxes,
            param_name="max_sandboxes",
        )
        self._queue_enabled = True
        self._queue_max_size = max(
            1,
            int(queue_max_size if queue_max_size is not None else settings.queue_max_size),
        )
        self._queue_timeout_seconds = float(
            queue_timeout_seconds
            if queue_timeout_seconds is not None
            else settings.queue_timeout_seconds
        )
        self._idle_timeout_seconds = float(
            idle_timeout_seconds
            if idle_timeout_seconds is not None
            else settings.idle_timeout_seconds
        )
        self._idle_check_interval_seconds = float(
            idle_check_interval_seconds
            if idle_check_interval_seconds is not None
            else settings.idle_check_interval_seconds
        )
        self._link_heartbeat_enabled = settings.link_heartbeat_enabled
        self._link_heartbeat_timeout_seconds = float(
            settings.link_heartbeat_timeout_seconds
        )
        self._link_heartbeat_check_interval_seconds = float(
            settings.link_heartbeat_check_interval_seconds
        )
        self._link_heartbeat_task: asyncio.Task | None = None
        self._backup_enabled = _env_bool(_SANDBOX_BACKUP_ENABLE_ENV, default=True)
        self._backup_period_seconds = max(
            1.0,
            _env_float(
                _SANDBOX_BACKUP_PERIOD_SECONDS_ENV,
                default=_SANDBOX_BACKUP_DEFAULT_PERIOD_SECONDS,
            ),
        )
        self._backup_task: asyncio.Task | None = None
        self._adopt_existing_enabled = (
            sandbox_adopt_existing_enabled()
            if adopt_existing is None
            else bool(adopt_existing)
        )
        self._gateway_instance_id = (
            str(gateway_instance_id or "").strip() or get_gateway_instance_id()
        )
        self._runtimes: dict[str, SandboxRuntime] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._pool_lock = asyncio.Lock()
        self._waiters: deque[asyncio.Future[None]] = deque()
        self._reconnect_waiters: dict[str, deque[asyncio.Future[None]]] = {}
        self._creating_count = 0
        self._server_config: dict[str, Any] = {}
        self._server_env: dict[str, str] | None = None
        self._idle_task: asyncio.Task | None = None
        self._closed = False
        # AgentServer 主动推送（含 chat/skilldev ask_user_question）的下行处理器；
        # 每条 OA 物理连接独立持有，需在创建 client 时传递。
        self._on_server_push: (
            Callable[[dict[str, Any]], Awaitable[None]] | None
        ) = None
        self._on_openability_connection_lost: (
            Callable[[dict[str, Any]], Awaitable[None]] | None
        ) = None
        self._on_openability_reconnected: (
            Callable[[dict[str, Any]], Awaitable[None]] | None
        ) = None
        self._on_openability_reconnect_failed: (
            Callable[[dict[str, Any]], Awaitable[None]] | None
        ) = None

    async def connect(self, uri: str) -> None:
        self._ensure_idle_task()

    async def disconnect(self) -> None:
        self._closed = True
        if self._link_heartbeat_task is not None:
            self._link_heartbeat_task.cancel()
            try:
                await self._link_heartbeat_task
            except asyncio.CancelledError:
                pass
            self._link_heartbeat_task = None
        if self._backup_task is not None:
            self._backup_task.cancel()
            try:
                await self._backup_task
            except asyncio.CancelledError:
                pass
            self._backup_task = None
        if self._idle_task is not None:
            self._idle_task.cancel()
            try:
                await self._idle_task
            except asyncio.CancelledError:
                pass
        for routing_key in list(self._runtimes):
            await self._terminate_runtime(routing_key)
        if self._sandbox_client is not None:
            await self._sandbox_client.close()
        if self._dcs_store is not None:
            await self._dcs_store.close()
        if self._routing_dcs_store is not None:
            await self._routing_dcs_store.close()
        if self._workspace_dcs_store is not None:
            await self._workspace_dcs_store.close()
        if self._sandbox_log_dcs_store is not None:
            await self._sandbox_log_dcs_store.close()

    def set_or_update_server_config(
        self,
        *,
        config: dict[str, Any],
        env: dict[str, str] | None = None,
    ) -> None:
        self._server_config = dict(config or {})
        self._server_env = dict(env) if env else None

    def set_server_push_handler(
        self,
        handler: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> None:
        """注册 AgentServer ``send_push`` 下行处理器，并下发到已建好的所有 OA client。

        MessageHandler 仅在 __init__ 时给 router 调用一次；之后每条 OA 物理连接
        在 ``_connect_open_ability_client`` 中创建时统一沿用当前 handler。
        """
        self._on_server_push = handler
        for runtime in self._runtimes.values():
            ac = runtime.agent_client
            if hasattr(ac, "set_server_push_handler"):
                ac.set_server_push_handler(handler)

    def set_openability_connection_lost_handler(
        self,
        handler: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> None:
        self._on_openability_connection_lost = handler

    def set_openability_reconnected_handler(
        self,
        handler: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> None:
        self._on_openability_reconnected = handler

    def set_openability_reconnect_failed_handler(
        self,
        handler: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> None:
        self._on_openability_reconnect_failed = handler

    def is_openability_reconnecting(
        self,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        try:
            routing_key = self._routing_key(user_id, session_id)
        except ValueError:
            return False
        runtime = self._runtimes.get(routing_key)
        return bool(
            runtime is not None
            and runtime.status != SandboxStatus.TERMINATED
            and self._runtime_needs_openability_refresh(runtime)
        )

    async def record_link_heartbeat(
        self,
        sandbox_id: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """AgentServer 经 OA 上报链路探活；更新对应 runtime 的最后心跳时间。"""
        sid = str(sandbox_id or "").strip()
        if not sid:
            return
        now = time.time()
        async with self._pool_lock:
            for runtime in self._runtimes.values():
                if runtime.sandbox_id != sid:
                    continue
                runtime.metadata["last_link_heartbeat_at"] = now
                if payload:
                    runtime.metadata["last_link_heartbeat_payload"] = dict(payload)
                logger.info(
                    "Recorded AgentServer link heartbeat: sandbox_id=%s",
                    sid,
                )
                return
        logger.info(
            "Ignored AgentServer link heartbeat for unknown sandbox_id=%s",
            sid,
        )

    async def _handle_inbound_link_heartbeat(self, wire: dict[str, Any]) -> None:
        from jiuwenclaw.e2a.link_heartbeat import (
            extract_link_heartbeat_payload,
            extract_link_heartbeat_sandbox_id,
        )

        sandbox_id = extract_link_heartbeat_sandbox_id(wire)
        if not sandbox_id:
            return
        await self.record_link_heartbeat(
            sandbox_id,
            extract_link_heartbeat_payload(wire),
        )

    async def _probe_link_return_path(self, runtime: SandboxRuntime) -> bool:
        return await self._probe_link_return_path_for_client(
            runtime.sandbox_id,
            runtime.routing_key,
            runtime.agent_client,
            runtime.metadata,
        )

    async def _probe_link_return_path_for_client(
        self,
        sandbox_id: str,
        routing_key: str,
        agent_client: AgentServerClient,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Gateway→OA→AgentServer 轻量 ping，激活 OA 反向路由以接收 link heartbeat。"""
        from jiuwenclaw.e2a.constants import (
            AGENTSERVER_LINK_HEARTBEAT_CHANNEL,
            AGENTSERVER_LINK_PING_METHOD,
        )

        request_id = f"link-ping-{format(int(time.time() * 1000), 'x')}_{secrets.token_hex(3)}"
        envelope = e2a_from_agent_fields(
            request_id=request_id,
            channel_id=AGENTSERVER_LINK_HEARTBEAT_CHANNEL,
            session_id=AGENTSERVER_LINK_HEARTBEAT_CHANNEL,
            req_method=AGENTSERVER_LINK_PING_METHOD,
            params={"sandbox_id": sandbox_id},
        )
        try:
            resp = await asyncio.wait_for(
                agent_client.send_request(envelope),
                timeout=5.0,
            )
            ok = bool(resp.ok)
            if ok:
                logger.info(
                    "Link return path probe OK: sandbox_id=%s request_id=%s",
                    sandbox_id,
                    request_id,
                )
                if metadata is not None:
                    metadata["last_link_probe_at"] = time.time()
            else:
                logger.warning(
                    "Link return path probe rejected: sandbox_id=%s request_id=%s ok=%s",
                    sandbox_id,
                    request_id,
                    resp.ok,
                )
            return ok
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Link return path probe failed: sandbox_id=%s request_id=%s error=%s",
                sandbox_id,
                request_id,
                exc,
            )
            return False

    async def send_request(self, envelope: E2AEnvelope) -> AgentResponse:
        try:
            await self._wait_for_openability_reconnect_buffer(envelope)
            runtime = await self._acquire_runtime(envelope)
        except ValueError as exc:
            return self._routing_error_response(envelope, str(exc))
        self._track_session_for_runtime(runtime, envelope)
        try:
            await self._ensure_workspace_restored(runtime, envelope)
            return await runtime.agent_client.send_request(envelope)
        finally:
            await self._mark_task_done(runtime)

    async def send_request_stream(self, envelope: E2AEnvelope) -> AsyncIterator[AgentResponseChunk]:
        try:
            await self._wait_for_openability_reconnect_buffer(envelope)
            runtime = await self._acquire_runtime(envelope)
        except ValueError as exc:
            yield self._routing_error_chunk(envelope, str(exc))
            return
        self._track_session_for_runtime(runtime, envelope)
        try:
            await self._ensure_workspace_restored(runtime, envelope)
            async for chunk in runtime.agent_client.send_request_stream(envelope):
                yield chunk
        finally:
            await self._mark_task_done(runtime)

    def _routing_key(self, user_id: str | None, session_id: str | None) -> str:
        uid = str(user_id or "").strip()
        if uid:
            return f"vibeskill:user:{uid}"
        sid = str(session_id or "").strip()
        if sid:
            return f"vibeskill:session:{sid}"
        raise ValueError("user_id or session_id is required for sandbox routing")

    def _routing_error_response(self, envelope: E2AEnvelope, message: str) -> AgentResponse:
        return AgentResponse(
            request_id=str(envelope.request_id),
            channel_id=str(envelope.channel or ""),
            ok=False,
            payload={"error": message},
        )

    def _routing_error_chunk(self, envelope: E2AEnvelope, message: str) -> AgentResponseChunk:
        return AgentResponseChunk(
            request_id=str(envelope.request_id),
            channel_id=str(envelope.channel or ""),
            payload={"error": message},
            is_complete=True,
        )

    async def _acquire_runtime(self, envelope: E2AEnvelope) -> SandboxRuntime:
        routing_key = self._routing_key(envelope.user_id, envelope.session_id)
        return await self._acquire_runtime_for_key(
            routing_key,
            user_id=envelope.user_id,
            session_id=envelope.session_id,
        )

    def _openability_reconnect_buffer_timeout_seconds(self) -> float:
        return max(
            self._queue_timeout_seconds,
            self._get_open_ability_config().reconnect_timeout_seconds,
        )

    @staticmethod
    def _should_drop_during_openability_reconnect(envelope: E2AEnvelope) -> bool:
        method = str(envelope.method or "").strip()
        return method in _OPENABILITY_RECONNECT_DROP_METHODS

    async def _wait_for_openability_reconnect_buffer(self, envelope: E2AEnvelope) -> None:
        routing_key = self._routing_key(envelope.user_id, envelope.session_id)
        while True:
            waiter: asyncio.Future[None] | None = None
            async with self._pool_lock:
                runtime = self._runtimes.get(routing_key)
                if runtime is None or not self._runtime_needs_openability_refresh(runtime):
                    return
                if self._should_drop_during_openability_reconnect(envelope):
                    logger.info(
                        "Dropping interrupt request during OA reconnect: "
                        "routing_key=%s sandbox_id=%s session_id=%s request_id=%s method=%s",
                        routing_key,
                        runtime.sandbox_id,
                        envelope.session_id,
                        envelope.request_id,
                        envelope.method,
                    )
                    raise RuntimeError(
                        "OpenAbility reconnecting; dropping interrupt request"
                    )
                waiters = self._reconnect_waiters.setdefault(routing_key, deque())
                if len(waiters) >= self._queue_max_size:
                    raise RuntimeError("sandbox reconnect buffer is full")
                waiter = asyncio.get_running_loop().create_future()
                waiters.append(waiter)
            try:
                assert waiter is not None
                buffer_timeout = self._openability_reconnect_buffer_timeout_seconds()
                await asyncio.wait_for(waiter, timeout=buffer_timeout)
                return
            except asyncio.TimeoutError as exc:
                async with self._pool_lock:
                    waiters = self._reconnect_waiters.get(routing_key)
                    if waiters is not None:
                        try:
                            assert waiter is not None
                            waiters.remove(waiter)
                        except ValueError:
                            pass
                        if not waiters:
                            self._reconnect_waiters.pop(routing_key, None)
                raise RuntimeError("sandbox reconnect wait timed out") from exc

    async def _flush_reconnect_waiters(self, routing_key: str) -> None:
        async with self._pool_lock:
            waiters = self._reconnect_waiters.pop(routing_key, deque())
        while waiters:
            waiter = waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)

    async def _acquire_runtime_for_key(
        self,
        routing_key: str,
        *,
        user_id: str | None,
        session_id: str | None,
    ) -> SandboxRuntime:
        self._ensure_idle_task()
        lock = self._locks.setdefault(routing_key, asyncio.Lock())
        async with lock:
            while True:
                runtime_to_refresh: SandboxRuntime | None = None
                async with self._pool_lock:
                    runtime = self._runtimes.get(routing_key)
                    if runtime is None:
                        break
                    if runtime.status != SandboxStatus.TERMINATING and not self._runtime_needs_openability_refresh(runtime):
                        self._mark_task_start_unlocked(runtime)
                        return runtime
                    if runtime.status != SandboxStatus.TERMINATING:
                        runtime_to_refresh = runtime
                if runtime_to_refresh is not None:
                    refreshed = await self._refresh_runtime_open_ability(
                        runtime_to_refresh,
                        reason="acquire-refresh",
                    )
                    if refreshed:
                        continue
                    await self._drop_runtime_for_reconnect(runtime_to_refresh)
                    continue
                await asyncio.sleep(0.01)
            await self._wait_for_capacity(routing_key)
            async with self._pool_lock:
                self._creating_count += 1
            try:
                adopted = await self._try_adopt_runtime_from_dcs(
                    routing_key,
                    user_id=user_id,
                    session_id=session_id,
                )
                if adopted is not None:
                    return adopted
                return await self._create_and_bind_sandbox(
                    routing_key,
                    user_id=user_id,
                    session_id=session_id,
                )
            finally:
                async with self._pool_lock:
                    self._creating_count = max(0, self._creating_count - 1)
                self._notify_next_waiter()

    async def _try_adopt_runtime_from_dcs(
        self,
        routing_key: str,
        *,
        user_id: str | None,
        session_id: str | None,
    ) -> SandboxRuntime | None:
        if not self._adopt_existing_enabled:
            return None
        record = await self._get_routing_dcs_store().get_routing(routing_key)
        if record is None:
            return None
        endpoint = await self._get_dcs_store().get_openability_endpoint(record.sandbox_id)
        if endpoint is None:
            logger.warning(
                "Stale sandbox routing mapping (no OA endpoint): sandbox_id=%s session_id=%s",
                record.sandbox_id,
                session_id or "",
            )
            await self._delete_routing_mapping(
                routing_key, sandbox_id=record.sandbox_id
            )
            return None
        interface_info.mark_current(interface_info.TimingPoint.OA_ENDPOINT_OBTAINED)
        logger.info(
            "Adopting existing sandbox from DCS: sandbox_id=%s session_id=%s "
            "prior_gateway=%s",
            record.sandbox_id,
            session_id or "",
            record.gateway_id,
        )
        runtime = await self._build_runtime_from_sandbox(
            routing_key,
            sandbox_id=record.sandbox_id,
            user_id=user_id,
            session_id=session_id,
            adopted=True,
            duration_anchor_at=record.updated_at,
        )
        await self._maybe_refresh_sandbox_duration(runtime)
        return runtime

    async def _create_and_bind_sandbox(
        self,
        routing_key: str,
        *,
        user_id: str | None,
        session_id: str | None,
    ) -> SandboxRuntime:
        sandbox_id: str | None = None
        dcs_registered = False
        routing_claimed = False
        try:
            sandbox_client = self._get_sandbox_client()
            interface_info.mark_current(
                interface_info.TimingPoint.SANDBOX_CREATE_STARTED
            )
            result = await sandbox_client.create_sandbox()
            if not result.success:
                raise RuntimeError(result.error or "create_sandbox failed")
            sandbox_id = str(result.output or "").strip()
            if not sandbox_id:
                raise RuntimeError("create_sandbox returned empty sandbox_id")
            interface_info.mark_current(
                interface_info.TimingPoint.SANDBOX_CREATE_SUCCEEDED
            )

            if self._adopt_existing_enabled:
                routing_claimed = await self._get_routing_dcs_store().set_routing_nx(
                    routing_key,
                    sandbox_id=sandbox_id,
                    gateway_id=self._gateway_instance_id,
                )
                if not routing_claimed:
                    logger.info(
                        "Lost routing NX race; adopting peer sandbox: "
                        "sandbox_id=%s session_id=%s",
                        sandbox_id,
                        session_id or "",
                    )
                    try:
                        await sandbox_client.delete_sandbox(sandbox_id)
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "Failed to delete sandbox after losing routing NX: "
                            "sandbox_id=%s session_id=%s",
                            sandbox_id,
                            session_id or "",
                        )
                    adopted = await self._try_adopt_runtime_from_dcs(
                        routing_key,
                        user_id=user_id,
                        session_id=session_id,
                    )
                    if adopted is not None:
                        return adopted
                    raise RuntimeError(
                        "routing NX lost but no adoptable sandbox in DCS"
                    )

            registration = await self._register_sandbox_record(
                sandbox_id,
                user_id=str(user_id or session_id or "").strip(),
            )
            dcs_registered = bool(registration.get("api_key"))
            metadata = {
                "routing_key": routing_key,
                "user_id": user_id,
                "session_id": session_id,
                **registration,
            }
            runtime = await self._build_runtime_from_sandbox(
                routing_key,
                sandbox_id=sandbox_id,
                user_id=user_id,
                session_id=session_id,
                metadata_extra=metadata,
                agent_client=await self._connect_open_ability_client(
                    sandbox_id, routing_key, metadata
                ),
            )
            return runtime
        except Exception:
            if sandbox_id and routing_claimed:
                await self._delete_routing_mapping(
                    routing_key, sandbox_id=sandbox_id
                )
            if sandbox_id:
                if dcs_registered:
                    await self._delete_sandbox_dcs_record(sandbox_id)
                try:
                    await self._get_sandbox_client().delete_sandbox(sandbox_id)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Failed to cleanup sandbox after runtime create failure: "
                        "sandbox_id=%s session_id=%s",
                        sandbox_id,
                        session_id or "",
                    )
            raise

    async def _build_runtime_from_sandbox(
        self,
        routing_key: str,
        *,
        sandbox_id: str,
        user_id: str | None,
        session_id: str | None,
        metadata_extra: dict[str, Any] | None = None,
        agent_client: AgentServerClient | None = None,
        adopted: bool = False,
        duration_anchor_at: float | None = None,
    ) -> SandboxRuntime:
        metadata: dict[str, Any] = {
            "routing_key": routing_key,
            "user_id": user_id,
            "session_id": session_id,
            "sandbox_id": sandbox_id,
        }
        if adopted:
            metadata["adopted"] = True
        if metadata_extra:
            metadata.update(metadata_extra)
        client = agent_client
        if client is None:
            client = await self._connect_open_ability_client(sandbox_id, routing_key, metadata)
        if self._server_config:
            client.set_or_update_server_config(config=self._server_config, env=self._server_env)
        now = time.time()
        runtime = SandboxRuntime(
            routing_key=routing_key,
            sandbox_id=sandbox_id,
            agent_client=client,
            status=SandboxStatus.BUSY,
            task_count=1,
            created_at=now,
            last_active_at=now,
            expires_at=self._compute_expires_at(anchor_at=duration_anchor_at),
            metadata=metadata,
        )
        async with self._pool_lock:
            self._runtimes[routing_key] = runtime
        return runtime

    async def _wait_for_capacity(self, routing_key: str) -> None:
        while True:
            waiter: asyncio.Future[None] | None = None
            async with self._pool_lock:
                if self._has_capacity_unlocked(routing_key):
                    return
                idle_routing_key = self._first_idle_runtime_key_unlocked()
                if idle_routing_key is None:
                    if not self._queue_enabled:
                        raise RuntimeError("sandbox capacity reached")
                    if len(self._waiters) >= self._queue_max_size:
                        raise RuntimeError("sandbox routing queue is full")
                    loop = asyncio.get_running_loop()
                    waiter = loop.create_future()
                    self._waiters.append(waiter)
            if idle_routing_key is not None:
                await self._terminate_runtime(idle_routing_key)
                continue
            try:
                await asyncio.wait_for(waiter, timeout=self._queue_timeout_seconds)
            except asyncio.TimeoutError as exc:
                async with self._pool_lock:
                    try:
                        self._waiters.remove(waiter)
                    except ValueError:
                        pass
                raise RuntimeError("sandbox routing queue wait timed out") from exc

    def _has_capacity_unlocked(self, routing_key: str) -> bool:
        runtime = self._runtimes.get(routing_key)
        if runtime is not None and runtime.status != SandboxStatus.TERMINATING:
            return True
        if self._max_sandboxes <= 0:
            return True
        return len(self._runtimes) + self._creating_count < self._max_sandboxes

    def _mark_task_start_unlocked(self, runtime: SandboxRuntime) -> None:
        runtime.task_count += 1
        runtime.status = SandboxStatus.BUSY
        runtime.last_active_at = time.time()

    async def _mark_task_done(self, runtime: SandboxRuntime) -> None:
        should_cleanup = False
        async with self._pool_lock:
            runtime.task_count = max(0, runtime.task_count - 1)
            runtime.last_active_at = time.time()
            runtime.status = SandboxStatus.IDLE if runtime.task_count == 0 else SandboxStatus.BUSY
            should_cleanup = runtime.status == SandboxStatus.IDLE and any(
                not waiter.done() for waiter in self._waiters
            )
        if should_cleanup:
            await self._terminate_runtime(runtime.routing_key)

    def _first_idle_runtime_key_unlocked(self) -> str | None:
        for routing_key, runtime in self._runtimes.items():
            if self._runtime_needs_openability_refresh(runtime):
                continue
            if runtime.status == SandboxStatus.IDLE and runtime.task_count == 0:
                return routing_key
        return None

    def _ensure_idle_task(self) -> None:
        if self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._idle_task is None:
            self._idle_task = loop.create_task(
                self._idle_cleanup_loop(),
                name="sandbox-router-idle-cleanup",
            )
        if self._link_heartbeat_enabled and self._link_heartbeat_task is None:
            self._link_heartbeat_task = loop.create_task(
                self._link_heartbeat_watch_loop(),
                name="sandbox-router-link-heartbeat-watch",
            )
        if self._backup_enabled and self._backup_task is None:
            self._backup_task = loop.create_task(
                self._periodic_backup_loop(),
                name="sandbox-router-periodic-backup",
            )

    async def _link_heartbeat_watch_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(self._link_heartbeat_check_interval_seconds)
            for routing_key, runtime in list(self._runtimes.items()):
                if runtime.status in {
                    SandboxStatus.TERMINATING,
                    SandboxStatus.TERMINATED,
                }:
                    continue
                if await self._is_link_heartbeat_stale(runtime):
                    await self._handle_stale_link_heartbeat(runtime)

    async def _is_link_heartbeat_stale(self, runtime: SandboxRuntime) -> bool:
        if not self._link_heartbeat_enabled:
            return False
        if self._runtime_needs_openability_refresh(runtime):
            return False
        now = time.time()
        last = runtime.metadata.get("last_link_heartbeat_at")
        if last is not None:
            return now - float(last) >= self._link_heartbeat_timeout_seconds
        anchor = runtime.metadata.get("openability_connected_at")
        if anchor is None:
            anchor = runtime.created_at
        return now - float(anchor) >= self._link_heartbeat_timeout_seconds

    async def _handle_stale_link_heartbeat(self, runtime: SandboxRuntime) -> None:
        logger.warning(
            "AgentServer link heartbeat timeout: sandbox_id=%s timeout=%.1fs last=%s",
            runtime.sandbox_id,
            self._link_heartbeat_timeout_seconds,
            runtime.metadata.get("last_link_heartbeat_at"),
        )
        if await self._probe_link_return_path(runtime):
            runtime.metadata["openability_connected_at"] = time.time()
            logger.info(
                "Recovered link return path via probe after heartbeat timeout: "
                "sandbox_id=%s",
                runtime.sandbox_id,
            )
            return
        await self._handle_open_ability_connection_lost(
            sandbox_id=runtime.sandbox_id,
            routing_key=runtime.routing_key,
            agent_client=runtime.agent_client,
            payload={
                "event": "agentserver.link.heartbeat.timeout",
                "sandbox_id": runtime.sandbox_id,
                "timeout_seconds": self._link_heartbeat_timeout_seconds,
                "last_link_heartbeat_at": runtime.metadata.get("last_link_heartbeat_at"),
            },
        )

    async def _idle_cleanup_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(self._idle_check_interval_seconds)
            now = time.time()
            for routing_key, runtime in list(self._runtimes.items()):
                if runtime.status == SandboxStatus.TERMINATING:
                    continue
                if self._runtime_needs_openability_refresh(runtime):
                    logger.debug(
                        "Skipping idle sandbox reclaim during OA reconnect: "
                        "sandbox_id=%s",
                        runtime.sandbox_id,
                    )
                    continue
                await self._maybe_refresh_sandbox_duration(runtime)
                if runtime.task_count == 0 and now - runtime.last_active_at >= self._idle_timeout_seconds:
                    await self._terminate_runtime(routing_key)

    async def _terminate_runtime(self, routing_key: str) -> None:
        async with self._pool_lock:
            runtime = self._runtimes.get(routing_key)
            if runtime is None or runtime.status == SandboxStatus.TERMINATING:
                return
            runtime.status = SandboxStatus.TERMINATING
        sandbox_id = runtime.sandbox_id
        tracked_session_ids = _tracked_session_ids_from_metadata(runtime.metadata)
        try:
            await self._backup_workspaces_before_terminate(runtime)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Workspace backup failed before sandbox terminate: "
                "sandbox_id=%s session_ids=%s",
                sandbox_id,
                tracked_session_ids,
            )
        try:
            await self._disconnect_agent_client(sandbox_id, runtime.agent_client)
        finally:
            try:
                if await self._delete_remote_sandbox(sandbox_id):
                    await self._delete_sandbox_dcs_record(sandbox_id)
                    await self._delete_routing_mapping(
                        routing_key, sandbox_id=sandbox_id
                    )
                else:
                    logger.warning(
                        "Skipped DCS/routing cleanup after failed sandbox delete: "
                        "sandbox_id=%s session_ids=%s",
                        sandbox_id,
                        tracked_session_ids,
                    )
            finally:
                async with self._pool_lock:
                    runtime.status = SandboxStatus.TERMINATED
                    self._runtimes.pop(routing_key, None)
                self._notify_next_waiter()

    async def _delete_remote_sandbox(self, sandbox_id: str) -> bool:
        """Delete the remote sandbox. Return False on failure so DCS metadata can be retained."""
        try:
            result = await self._get_sandbox_client().delete_sandbox(sandbox_id)
            if not result.success:
                logger.error(
                    "delete_sandbox failed: sandbox_id=%s error=%s",
                    sandbox_id,
                    result.error,
                )
                return False
            return True
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to delete remote sandbox: sandbox_id=%s", sandbox_id
            )
            return False

    async def _register_sandbox_record(
        self,
        sandbox_id: str,
        *,
        user_id: str,
    ) -> dict[str, str]:
        store = self._get_dcs_store()
        api_key = get_claw_api_key()
        record = await store.save_sandbox(sandbox_id, api_key=api_key)
        interface_info.mark_current(interface_info.TimingPoint.SANDBOX_DCS_WRITTEN)
        await self._upload_sandbox_init_data(sandbox_id, api_key, user_id=user_id)
        interface_info.mark_current(
            interface_info.TimingPoint.SANDBOX_API_KEY_UPLOADED
        )
        return {
            "sandbox_id": record.sandbox_id,
            "api_key": api_key,
            "api_key_sha256": record.api_key_sha256,
        }

    async def _upload_sandbox_init_data(
        self,
        sandbox_id: str,
        api_key: str,
        *,
        user_id: str,
    ) -> None:
        sandbox_client = self._get_sandbox_client()
        await upload_sandbox_init_data(
            sandbox_client,
            sandbox_id=sandbox_id,
            api_key=api_key,
            user_id=user_id,
        )

    async def _delete_sandbox_dcs_record(self, sandbox_id: str) -> None:
        try:
            store = self._get_dcs_store()
            await store.delete_sandbox(sandbox_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to delete sandbox DCS record: sandbox_id=%s", sandbox_id
            )

    async def _delete_routing_mapping(
        self,
        routing_key: str,
        *,
        sandbox_id: str | None = None,
    ) -> None:
        if not self._adopt_existing_enabled:
            return
        sid = str(sandbox_id or "").strip()
        try:
            await self._get_routing_dcs_store().delete_routing(routing_key)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to delete sandbox routing mapping: sandbox_id=%s",
                sid,
            )

    def _get_dcs_store(self) -> SandboxDcsStore:
        if self._dcs_store is None:
            self._dcs_store = SandboxDcsStore.from_env()
        return self._dcs_store

    def _get_routing_dcs_store(self) -> SandboxRoutingDcsStore:
        if self._routing_dcs_store is None:
            self._routing_dcs_store = SandboxRoutingDcsStore.from_env()
        return self._routing_dcs_store

    def _get_workspace_dcs_store(self) -> WorkspaceDcsStore:
        if self._workspace_dcs_store is None:
            self._workspace_dcs_store = WorkspaceDcsStore.from_env()
        return self._workspace_dcs_store

    def _get_sandbox_log_dcs_store(self) -> SandboxLogDcsStore:
        if self._sandbox_log_dcs_store is None:
            self._sandbox_log_dcs_store = SandboxLogDcsStore.from_env()
        return self._sandbox_log_dcs_store

    @staticmethod
    def _user_id_from_routing_key(routing_key: str) -> str | None:
        prefix = "vibeskill:user:"
        key = str(routing_key or "")
        if key.startswith(prefix):
            uid = key[len(prefix) :].strip()
            return uid or None
        return None

    @staticmethod
    def _is_system_session_id(session_id: str | None) -> bool:
        sid = str(session_id or "").strip()
        return bool(sid and sid.startswith(_SYSTEM_SESSION_ID_PREFIXES))

    @staticmethod
    def _restored_session_ids(runtime: SandboxRuntime) -> set[str]:
        restored_ids = runtime.metadata.get("restored_session_ids")
        if not isinstance(restored_ids, set):
            restored_ids = set()
            runtime.metadata["restored_session_ids"] = restored_ids
        return restored_ids

    @staticmethod
    def _should_skip_workspace_restore_for_live_sandbox(
        runtime: SandboxRuntime,
        record: WorkspaceRecord,
    ) -> bool:
        """Skip batch_download only when the snapshot came from this live sandbox."""
        record_sandbox_id = str(record.sandbox_id or "").strip()
        runtime_sandbox_id = str(runtime.sandbox_id or "").strip()
        return bool(
            record_sandbox_id
            and runtime_sandbox_id
            and record_sandbox_id == runtime_sandbox_id
        )

    @staticmethod
    def _workspace_restore_lock(runtime: SandboxRuntime, session_id: str) -> asyncio.Lock:
        locks = runtime.metadata.get("workspace_restore_locks")
        if not isinstance(locks, dict):
            locks = {}
            runtime.metadata["workspace_restore_locks"] = locks
        lock = locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            locks[session_id] = lock
        return lock

    def _track_session_for_runtime(
        self,
        runtime: SandboxRuntime,
        envelope: E2AEnvelope,
    ) -> None:
        method = str(envelope.method or "").strip()
        if method in _WORKSPACE_SKIP_METHODS:
            return
        session_id = str(envelope.session_id or "").strip()
        if (
            not session_id
            or session_id == "batch"
            or self._is_system_session_id(session_id)
        ):
            return
        user_id = str(envelope.user_id or "").strip()
        if user_id:
            runtime.metadata["user_id"] = user_id
        session_ids = runtime.metadata.get("session_ids")
        if not isinstance(session_ids, set):
            session_ids = set()
            runtime.metadata["session_ids"] = session_ids
        session_ids.add(session_id)
        if self._backup_enabled:
            self._mark_backup_period_session_active(runtime, session_id)

    @staticmethod
    def _backup_period_active_sessions(runtime: SandboxRuntime) -> set[str]:
        active_sessions = runtime.metadata.get("backup_period_active_sessions")
        if not isinstance(active_sessions, set):
            active_sessions = set()
            runtime.metadata["backup_period_active_sessions"] = active_sessions
        return active_sessions

    @staticmethod
    def _backup_period_session_versions(runtime: SandboxRuntime) -> dict[str, int]:
        versions = runtime.metadata.get("backup_period_session_versions")
        if not isinstance(versions, dict):
            versions = {}
            runtime.metadata["backup_period_session_versions"] = versions
        return versions

    def _mark_backup_period_session_active(
        self,
        runtime: SandboxRuntime,
        session_id: str,
    ) -> None:
        sid = str(session_id or "").strip()
        if not sid or self._is_system_session_id(sid):
            return
        active_sessions = self._backup_period_active_sessions(runtime)
        versions = self._backup_period_session_versions(runtime)
        active_sessions.add(sid)
        try:
            current_version = int(versions.get(sid) or 0)
        except (TypeError, ValueError):
            current_version = 0
        versions[sid] = current_version + 1

    async def _periodic_backup_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(self._backup_period_seconds)
            await self._backup_active_sessions_for_period()

    async def _backup_active_sessions_for_period(self) -> None:
        snapshots: list[tuple[SandboxRuntime, list[str], dict[str, int]]] = []
        async with self._pool_lock:
            for runtime in self._runtimes.values():
                if runtime.status in {
                    SandboxStatus.TERMINATING,
                    SandboxStatus.TERMINATED,
                }:
                    continue
                if self._runtime_needs_openability_refresh(runtime):
                    continue
                active_sessions = self._backup_period_active_sessions(runtime)
                if not active_sessions:
                    continue
                versions = self._backup_period_session_versions(runtime)
                session_ids = self._normalize_session_ids(active_sessions)
                version_snapshot = {
                    sid: int(versions.get(sid) or 0)
                    for sid in session_ids
                }
                if session_ids:
                    snapshots.append((runtime, session_ids, version_snapshot))

        for runtime, session_ids, version_snapshot in snapshots:
            try:
                persisted = await self._backup_workspaces(
                    runtime,
                    session_ids,
                    reason="periodic",
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Periodic workspace backup failed: sandbox_id=%s session_ids=%s",
                    runtime.sandbox_id,
                    session_ids,
                )
                continue
            if not persisted:
                continue
            async with self._pool_lock:
                active_sessions = self._backup_period_active_sessions(runtime)
                versions = self._backup_period_session_versions(runtime)
                for sid in persisted:
                    if sid not in active_sessions:
                        continue
                    if int(versions.get(sid) or 0) == version_snapshot.get(sid):
                        active_sessions.discard(sid)
                        versions.pop(sid, None)

    async def _ensure_workspace_restored(
        self,
        runtime: SandboxRuntime,
        envelope: E2AEnvelope,
    ) -> None:
        method = str(envelope.method or "").strip()
        if method in _WORKSPACE_SKIP_METHODS:
            return
        session_id = str(envelope.session_id or "").strip()
        if (
            not session_id
            or session_id == "batch"
            or self._is_system_session_id(session_id)
        ):
            return
        if session_id in self._restored_session_ids(runtime):
            return

        lock = self._workspace_restore_lock(runtime, session_id)
        async with lock:
            restored_ids = self._restored_session_ids(runtime)
            if session_id in restored_ids:
                return

            logger.info(
                "Querying workspace snapshot from DCS: session_id=%s sandbox_id=%s",
                session_id,
                runtime.sandbox_id,
            )
            record = await self._get_workspace_dcs_store().get_workspace(session_id)
            if record is None:
                restored_ids.add(session_id)
                logger.info(
                    "No workspace snapshot in DCS; skipping restore "
                    "(expected for new session): session_id=%s sandbox_id=%s",
                    session_id,
                    runtime.sandbox_id,
                )
                return

            logger.info(
                "Found workspace snapshot in DCS: session_id=%s sandbox_id=%s "
                "url=%s name=%s workspace_sandbox_id=%s",
                session_id,
                runtime.sandbox_id,
                record.url,
                record.name,
                str(record.sandbox_id or "").strip() or "n/a",
            )

            if self._should_skip_workspace_restore_for_live_sandbox(runtime, record):
                restored_ids.add(session_id)
                logger.info(
                    "Skipped workspace restore for live sandbox: session_id=%s "
                    "sandbox_id=%s adopted=%s workspace_sandbox_id=%s",
                    session_id,
                    runtime.sandbox_id,
                    bool(runtime.metadata.get("adopted")),
                    str(record.sandbox_id or "").strip(),
                )
                return

            user_id = str(envelope.user_id or runtime.metadata.get("user_id") or "").strip()
            if not user_id:
                user_id = self._user_id_from_routing_key(runtime.routing_key) or ""

            request_id = (
                f"sandbox-restore-{int(time.time() * 1000):x}-{secrets.token_hex(3)}"
            )
            query_url_obs = _create_query_url_obs()
            latest_url = await query_url_obs.get_latest_obs_url(record.url)
            if latest_url != record.url:
                logger.info(
                    "Refreshed OBS download URL for workspace restore: session_id=%s "
                    "sandbox_id=%s",
                    session_id,
                    runtime.sandbox_id,
                )
            else:
                logger.info(
                    "Using workspace OBS URL for restore: session_id=%s sandbox_id=%s url=%s",
                    session_id,
                    runtime.sandbox_id,
                    latest_url,
                )
            restore_env = e2a_from_agent_fields(
                request_id=request_id,
                channel_id=str(envelope.channel or "") or _VIBESKILL_CHANNEL_ID,
                session_id=session_id,
                user_id=user_id or None,
                req_method=ReqMethod.SKILLDEV_BATCH_DOWNLOAD,
                params={
                    "items": [
                        {
                            "sessionID": session_id,
                            "url": latest_url,
                            "name": record.name,
                        }
                    ]
                },
                is_stream=False,
                timestamp=time.time(),
            )
            try:
                resp = await runtime.agent_client.send_request(restore_env)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Workspace restore request failed: session_id=%s sandbox_id=%s",
                    session_id,
                    runtime.sandbox_id,
                )
                return

            if not resp.ok:
                payload = resp.payload if isinstance(resp.payload, dict) else {}
                logger.warning(
                    "Workspace restore failed: session_id=%s sandbox_id=%s error=%s",
                    session_id,
                    runtime.sandbox_id,
                    payload.get("error"),
                )
                return

            payload = resp.payload if isinstance(resp.payload, dict) else {}
            results = payload.get("results")
            if not isinstance(results, list):
                logger.warning(
                    "Workspace restore returned no results: session_id=%s sandbox_id=%s",
                    session_id,
                    runtime.sandbox_id,
                )
                return
            for item in results:
                if not isinstance(item, dict):
                    continue
                sid = str(item.get("sessionID") or item.get("session_id") or "").strip()
                if sid != session_id:
                    continue
                if str(item.get("status") or "").strip().lower() == "success":
                    restored_ids.add(session_id)
                    logger.info(
                        "Workspace restored from DCS: session_id=%s sandbox_id=%s",
                        session_id,
                        runtime.sandbox_id,
                    )
                    return
            logger.warning(
                "Workspace restore did not succeed: session_id=%s sandbox_id=%s results=%s",
                session_id,
                runtime.sandbox_id,
                results,
            )

    async def _backup_workspaces_before_terminate(self, runtime: SandboxRuntime) -> None:
        if self._backup_enabled:
            raw_session_ids = runtime.metadata.get("backup_period_active_sessions")
        else:
            raw_session_ids = runtime.metadata.get("session_ids")
        session_ids = self._normalize_session_ids(raw_session_ids)
        if not session_ids:
            return
        persisted = await self._backup_workspaces(
            runtime,
            session_ids,
            reason="terminate",
        )
        if self._backup_enabled and persisted:
            active_sessions = self._backup_period_active_sessions(runtime)
            versions = self._backup_period_session_versions(runtime)
            for sid in persisted:
                active_sessions.discard(sid)
                versions.pop(sid, None)

    @staticmethod
    def _normalize_session_ids(raw_session_ids: Any) -> list[str]:
        if isinstance(raw_session_ids, set):
            return sorted(
                str(sid).strip()
                for sid in raw_session_ids
                if str(sid).strip()
                and not SandboxRouterAgentClient._is_system_session_id(str(sid).strip())
            )
        if isinstance(raw_session_ids, (list, tuple)):
            return [
                str(sid).strip()
                for sid in raw_session_ids
                if str(sid).strip()
                and not SandboxRouterAgentClient._is_system_session_id(str(sid).strip())
            ]
        sid = str(raw_session_ids or "").strip()
        return [sid] if sid and not SandboxRouterAgentClient._is_system_session_id(sid) else []

    async def _backup_workspaces(
        self,
        runtime: SandboxRuntime,
        session_ids: list[str],
        *,
        reason: str,
    ) -> set[str]:
        session_ids = self._normalize_session_ids(session_ids)
        if not session_ids:
            return set()
        user_id = str(runtime.metadata.get("user_id") or "").strip()
        if not user_id:
            user_id = self._user_id_from_routing_key(runtime.routing_key) or ""

        request_id = (
            f"sandbox-backup-{reason}-{int(time.time() * 1000):x}-{secrets.token_hex(3)}"
        )
        backup_env = e2a_from_agent_fields(
            request_id=request_id,
            channel_id=_VIBESKILL_CHANNEL_ID,
            session_id=session_ids[0],
            user_id=user_id or None,
            req_method=ReqMethod.SKILLDEV_BATCH_UPLOAD,
            params={"session_ids": session_ids},
            is_stream=False,
            timestamp=time.time(),
        )
        resp = await runtime.agent_client.send_request(backup_env)
        sandbox_id = runtime.sandbox_id
        if not resp.ok:
            payload = resp.payload if isinstance(resp.payload, dict) else {}
            logger.warning(
                "Workspace backup failed before terminate: sandbox_id=%s "
                "request_id=%s reason=%s error=%s failed_session_ids=%s",
                sandbox_id,
                request_id,
                reason,
                payload.get("error"),
                session_ids,
            )
            return set()

        payload = resp.payload if isinstance(resp.payload, dict) else {}
        results = payload.get("results")
        if not isinstance(results, list):
            logger.warning(
                "Workspace backup returned no results: sandbox_id=%s "
                "request_id=%s reason=%s failed_session_ids=%s",
                sandbox_id,
                request_id,
                reason,
                session_ids,
            )
            return set()

        succeeded: list[dict[str, str]] = []
        failed_session_ids: list[str] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("sessionID") or item.get("session_id") or "").strip()
            status = str(item.get("status") or "").strip().lower()
            if status == "success":
                url = str(item.get("url") or "").strip()
                if sid and url:
                    succeeded.append(
                        {
                            "session_id": sid,
                            "url": url,
                            "name": str(item.get("name") or "").strip(),
                        }
                    )
                elif sid:
                    failed_session_ids.append(sid)
            elif sid:
                failed_session_ids.append(sid)

        logger.info(
            "Workspace backup completed: sandbox_id=%s "
            "request_id=%s reason=%s succeeded=%s failed_session_ids=%s",
            sandbox_id,
            request_id,
            reason,
            [{"session_id": e["session_id"], "url": e["url"]} for e in succeeded],
            failed_session_ids,
        )

        store = self._get_workspace_dcs_store()
        routing_key = runtime.routing_key
        persisted_session_ids: set[str] = set()
        query_url_obs: Any | None = None
        for entry in succeeded:
            try:
                try:
                    old_record = await store.get_workspace(entry["session_id"])
                except Exception:  # noqa: BLE001
                    old_record = None
                    logger.exception(
                        "Failed to read old workspace snapshot from DCS before overwrite: "
                        "session_id=%s sandbox_id=%s",
                        entry["session_id"],
                        sandbox_id,
                    )
                old_url = str(getattr(old_record, "url", "") or "").strip()
                if old_url and old_url != entry["url"]:
                    if query_url_obs is None:
                        query_url_obs = _create_query_url_obs()
                    try:
                        deleted = await query_url_obs.invoking_osms_delete(old_url)
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "Failed to delete old workspace OBS object before DCS overwrite: "
                            "session_id=%s sandbox_id=%s",
                            entry["session_id"],
                            sandbox_id,
                        )
                    else:
                        if not deleted:
                            logger.warning(
                                "OSMS delete old workspace OBS object returned false: "
                                "session_id=%s sandbox_id=%s",
                                entry["session_id"],
                                sandbox_id,
                            )
                        else:
                            logger.info(
                                "Deleted old workspace OBS object before DCS overwrite: "
                                "session_id=%s sandbox_id=%s",
                                entry["session_id"],
                                sandbox_id,
                            )
                await store.put_workspace(
                    entry["session_id"],
                    url=entry["url"],
                    name=entry["name"],
                    routing_key=routing_key,
                    sandbox_id=sandbox_id,
                )
                persisted_session_ids.add(entry["session_id"])
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Failed to persist workspace snapshot to DCS: session_id=%s "
                    "sandbox_id=%s",
                    entry["session_id"],
                    sandbox_id,
                )

        log_result = payload.get("log_result")
        if log_result is not None:
            await self._persist_sandbox_log_result(
                sandbox_id,
                log_result,
                query_url_obs=query_url_obs,
            )

        return persisted_session_ids

    async def _persist_sandbox_log_result(
        self,
        sandbox_id: str,
        log_result: Any,
        *,
        query_url_obs: Any | None = None,
    ) -> None:
        if not isinstance(log_result, dict):
            return
        status = str(log_result.get("status") or "").strip().lower()
        new_url = str(log_result.get("url") or "").strip()
        if status != "success" or not new_url:
            logger.warning(
                "Sandbox log backup skipped DCS persist: sandbox_id=%s status=%s",
                sandbox_id,
                status or "unknown",
            )
            return

        store = self._get_sandbox_log_dcs_store()
        try:
            try:
                old_record = await store.get_sandbox_log(sandbox_id)
            except Exception:  # noqa: BLE001
                old_record = None
                logger.exception(
                    "Failed to read old sandbox log snapshot from DCS before overwrite: "
                    "sandbox_id=%s",
                    sandbox_id,
                )
            old_url = str(getattr(old_record, "url", "") or "").strip()
            if old_url and old_url != new_url:
                if query_url_obs is None:
                    query_url_obs = _create_query_url_obs()
                try:
                    deleted = await query_url_obs.invoking_osms_delete(old_url)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Failed to delete old sandbox log OBS object before DCS overwrite: "
                        "sandbox_id=%s",
                        sandbox_id,
                    )
                else:
                    if not deleted:
                        logger.warning(
                            "OSMS delete old sandbox log OBS object returned false: "
                            "sandbox_id=%s",
                            sandbox_id,
                        )
                    else:
                        logger.info(
                            "Deleted old sandbox log OBS object before DCS overwrite: "
                            "sandbox_id=%s",
                            sandbox_id,
                        )
            await store.put_sandbox_log(
                sandbox_id,
                url=new_url,
                name=str(log_result.get("name") or "").strip(),
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to persist sandbox log snapshot to DCS: sandbox_id=%s",
                sandbox_id,
            )

    def _new_open_ability_ws_client(
        self,
        sandbox_id: str,
        routing_key: str,
    ) -> OpenAbilityWebSocketClient:
        open_ability_cfg = self._get_open_ability_config()
        client = OpenAbilityWebSocketClient(
            sandbox_id=sandbox_id,
            request_timeout_seconds=open_ability_cfg.request_timeout_seconds,
        )
        if self._on_server_push is not None:
            client.set_server_push_handler(self._on_server_push)
        if hasattr(client, "set_connection_lost_handler"):
            client.set_connection_lost_handler(
                lambda payload, _sandbox_id=sandbox_id, _routing_key=routing_key, _client=client: self._handle_open_ability_connection_lost(
                    sandbox_id=_sandbox_id,
                    routing_key=_routing_key,
                    agent_client=_client,
                    payload=payload,
                )
            )
        if hasattr(client, "set_link_heartbeat_handler"):
            client.set_link_heartbeat_handler(self._handle_inbound_link_heartbeat)
        return client

    @staticmethod
    async def _sleep_openability_reconnect_poll(
        deadline: float,
        poll_interval_seconds: float,
    ) -> None:
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(poll_interval_seconds, remaining))

    async def _connect_open_ability_client(
        self,
        sandbox_id: str,
        routing_key: str,
        metadata: dict[str, Any],
        *,
        connect_reason: str = "initial",
    ) -> AgentServerClient:
        store = self._get_dcs_store()
        open_ability_cfg = self._get_open_ability_config()
        deadline = time.time() + open_ability_cfg.reconnect_timeout_seconds
        attempt = 0
        last_failure = "none"
        trigger_session_id = str(metadata.get("session_id") or "").strip()
        logger.info(
            "Beginning OpenAbility reconnect window: sandbox_id=%s session_id=%s "
            "reason=%s timeout_seconds=%.1f poll_interval_seconds=%.1f",
            sandbox_id,
            trigger_session_id,
            connect_reason,
            open_ability_cfg.reconnect_timeout_seconds,
            open_ability_cfg.readiness_poll_interval_seconds,
        )
        while time.time() < deadline:
            attempt += 1
            remaining_seconds = max(0.0, deadline - time.time())
            endpoint: OpenAbilityEndpoint | None
            try:
                endpoint = await store.get_openability_endpoint(sandbox_id)
            except Exception as exc:  # noqa: BLE001
                last_failure = f"dcs-read-error:{exc}"
                logger.warning(
                    "OpenAbility reconnect attempt %s: DCS endpoint read failed, "
                    "will retry: sandbox_id=%s reason=%s "
                    "remaining_seconds=%.1f error=%s",
                    attempt,
                    sandbox_id,
                    connect_reason,
                    remaining_seconds,
                    exc,
                )
                await self._sleep_openability_reconnect_poll(
                    deadline,
                    open_ability_cfg.readiness_poll_interval_seconds,
                )
                continue

            if endpoint is None:
                last_failure = "endpoint-missing-in-dcs"
                logger.info(
                    "OpenAbility reconnect attempt %s: endpoint not in DCS yet, "
                    "will retry: sandbox_id=%s reason=%s "
                    "remaining_seconds=%.1f",
                    attempt,
                    sandbox_id,
                    connect_reason,
                    remaining_seconds,
                )
                await self._sleep_openability_reconnect_poll(
                    deadline,
                    open_ability_cfg.readiness_poll_interval_seconds,
                )
                continue

            interface_info.mark_current(
                interface_info.TimingPoint.OA_ENDPOINT_OBTAINED
            )

            endpoint_label = format_openability_endpoint(endpoint)
            ws_uri = build_openability_ws_uri(
                endpoint,
                ws_path=open_ability_cfg.ws_path,
            )
            client = self._new_open_ability_ws_client(sandbox_id, routing_key)
            logger.info(
                "OpenAbility reconnect attempt %s: connecting WebSocket: "
                "sandbox_id=%s session_id=%s reason=%s "
                "endpoint=%s uri=%s remaining_seconds=%.1f",
                attempt,
                sandbox_id,
                trigger_session_id,
                connect_reason,
                endpoint_label,
                ws_uri,
                remaining_seconds,
            )
            try:
                interface_info.mark_current(
                    interface_info.TimingPoint.OA_CONNECT_STARTED
                )
                await asyncio.wait_for(
                    client.connect(ws_uri),
                    timeout=open_ability_cfg.connect_timeout_seconds,
                )
                interface_info.log_interface_event(
                    severity="INFO",
                    session_id=trigger_session_id,
                    source="SkillCreator",
                    destination="OpenAbility",
                    interface_type="WebSocket",
                    ws_event="connect",
                    ws_result="success",
                )
                interface_info.mark_current(
                    interface_info.TimingPoint.OA_CONNECTED
                )
            except Exception as exc:  # noqa: BLE001
                last_failure = f"connect-failed:{exc}"
                interface_info.log_interface_event(
                    severity="WARN",
                    session_id=trigger_session_id,
                    source="SkillCreator",
                    destination="OpenAbility",
                    interface_type="WebSocket",
                    ws_event="connect",
                    ws_result="error",
                )
                logger.warning(
                    "OpenAbility reconnect attempt %s: WebSocket connect failed, "
                    "will retry: sandbox_id=%s reason=%s "
                    "endpoint=%s uri=%s remaining_seconds=%.1f error=%s",
                    attempt,
                    sandbox_id,
                    connect_reason,
                    endpoint_label,
                    ws_uri,
                    remaining_seconds,
                    exc,
                )
                await self._disconnect_agent_client(sandbox_id, client)
                await self._sleep_openability_reconnect_poll(
                    deadline,
                    open_ability_cfg.readiness_poll_interval_seconds,
                )
                continue

            probe_ok = await self._probe_link_return_path_for_client(
                sandbox_id,
                routing_key,
                client,
                metadata,
            )
            if not probe_ok:
                last_failure = "link-probe-failed"
                logger.warning(
                    "OpenAbility reconnect attempt %s: link probe failed, will retry: "
                    "sandbox_id=%s reason=%s endpoint=%s uri=%s "
                    "remaining_seconds=%.1f",
                    attempt,
                    sandbox_id,
                    connect_reason,
                    endpoint_label,
                    ws_uri,
                    remaining_seconds,
                )
                await self._disconnect_agent_client(sandbox_id, client)
                await self._sleep_openability_reconnect_poll(
                    deadline,
                    open_ability_cfg.readiness_poll_interval_seconds,
                )
                continue

            metadata["openability_endpoint"] = endpoint
            metadata["openability_ws_uri"] = ws_uri
            metadata["openability_connected_at"] = time.time()
            metadata.pop("last_link_heartbeat_at", None)
            logger.info(
                "OpenAbility reconnect window succeeded: sandbox_id=%s session_id=%s "
                "reason=%s endpoint=%s uri=%s attempts=%s",
                sandbox_id,
                trigger_session_id,
                connect_reason,
                endpoint_label,
                ws_uri,
                attempt,
            )
            return client

        logger.error(
            "OpenAbility reconnect window exhausted: sandbox_id=%s session_id=%s "
            "reason=%s timeout_seconds=%.1f attempts=%s last_failure=%s",
            sandbox_id,
            trigger_session_id,
            connect_reason,
            open_ability_cfg.reconnect_timeout_seconds,
            attempt,
            last_failure,
        )
        raise OpenAbilityReconnectTimeoutError(
            f"OpenAbility reconnect window exhausted for sandbox_id={sandbox_id} "
            f"reason={connect_reason} "
            f"timeout_seconds={open_ability_cfg.reconnect_timeout_seconds} "
            f"attempts={attempt} last_failure={last_failure}"
        )

    def _get_open_ability_config(self) -> OpenAbilityConfig:
        if self._open_ability_config is None:
            self._open_ability_config = OpenAbilityConfig.from_env()
        return self._open_ability_config

    async def _disconnect_agent_client(self, sandbox_id: str, agent_client: AgentServerClient | None) -> None:
        if agent_client is not None:
            await agent_client.disconnect()

    @staticmethod
    def _runtime_needs_openability_refresh(runtime: SandboxRuntime) -> bool:
        return bool(runtime.metadata.get("openability_reconnect_required"))

    async def _handle_open_ability_connection_lost(
        self,
        *,
        sandbox_id: str,
        routing_key: str,
        agent_client: AgentServerClient,
        payload: dict[str, Any],
    ) -> None:
        lock = self._locks.setdefault(routing_key, asyncio.Lock())
        async with lock:
            runtime = self._runtimes.get(routing_key)
            if runtime is None:
                return
            if runtime.sandbox_id != sandbox_id or runtime.agent_client is not agent_client:
                return
            runtime.metadata["openability_reconnect_required"] = True
            runtime.metadata["openability_connection_lost"] = dict(payload)
            runtime.metadata["openability_disconnect_at"] = time.time()
            runtime.last_active_at = time.time()
            session_ids = _tracked_session_ids_from_metadata(runtime.metadata)
            if self._on_openability_connection_lost is not None:
                asyncio.create_task(self._on_openability_connection_lost(
                    {
                        **dict(payload),
                        "sandbox_id": sandbox_id,
                        "routing_key": routing_key,
                        "user_id": str(runtime.metadata.get("user_id") or "").strip(),
                        "session_ids": session_ids,
                    }
                ))
            open_ability_cfg = self._get_open_ability_config()
            logger.warning(
                "Detected OA physical disconnect: sandbox_id=%s session_ids=%s "
                "reconnect_timeout_seconds=%.1f",
                sandbox_id,
                session_ids,
                open_ability_cfg.reconnect_timeout_seconds,
            )
            refreshed = await self._refresh_runtime_open_ability(
                runtime,
                reason="physical-disconnect",
            )
            if refreshed:
                return
            await self._drop_runtime_for_reconnect(runtime)

    async def _refresh_runtime_open_ability(
        self,
        runtime: SandboxRuntime,
        *,
        reason: str,
    ) -> bool:
        old_client = runtime.agent_client
        runtime.metadata["openability_reconnect_required"] = True
        runtime.last_active_at = time.time()
        session_ids = _tracked_session_ids_from_metadata(runtime.metadata)
        try:
            new_client = await self._connect_open_ability_client(
                runtime.sandbox_id,
                runtime.routing_key,
                runtime.metadata,
                connect_reason=reason,
            )
        except OpenAbilityReconnectTimeoutError as exc:
            logger.error(
                "Failed to refresh OA client after reconnect window: "
                "sandbox_id=%s session_ids=%s reason=%s error=%s",
                runtime.sandbox_id,
                session_ids,
                reason,
                exc,
            )
            return False
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to refresh OA client: sandbox_id=%s session_ids=%s reason=%s",
                runtime.sandbox_id,
                session_ids,
                reason,
            )
            return False

        if self._server_config:
            new_client.set_or_update_server_config(
                config=self._server_config,
                env=self._server_env,
            )
        runtime.agent_client = new_client
        runtime.metadata["openability_reconnect_required"] = False
        runtime.metadata["openability_reconnected_at"] = time.time()
        await self._flush_reconnect_waiters(runtime.routing_key)
        if self._on_openability_reconnected is not None:
            asyncio.create_task(self._on_openability_reconnected(
                {
                    "event": "openability.reconnected",
                    "sandbox_id": runtime.sandbox_id,
                    "routing_key": runtime.routing_key,
                    "user_id": str(runtime.metadata.get("user_id") or "").strip(),
                    "session_ids": _tracked_session_ids_from_metadata(runtime.metadata),
                    "reason": reason,
                }
            ))
        try:
            await self._disconnect_agent_client(runtime.sandbox_id, old_client)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to disconnect stale OA client: sandbox_id=%s",
                runtime.sandbox_id,
            )
        logger.info(
            "Refreshed OA client: sandbox_id=%s session_ids=%s reason=%s",
            runtime.sandbox_id,
            _tracked_session_ids_from_metadata(runtime.metadata),
            reason,
        )
        return True

    async def _drop_runtime_for_reconnect(self, runtime: SandboxRuntime) -> None:
        session_ids = _tracked_session_ids_from_metadata(runtime.metadata)
        async with self._pool_lock:
            current = self._runtimes.get(runtime.routing_key)
            if current is runtime:
                self._runtimes.pop(runtime.routing_key, None)
            runtime.status = SandboxStatus.TERMINATED
        await self._flush_reconnect_waiters(runtime.routing_key)
        try:
            await self._disconnect_agent_client(runtime.sandbox_id, runtime.agent_client)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to disconnect dropped OA client: sandbox_id=%s",
                runtime.sandbox_id,
            )
        self._notify_next_waiter()
        if self._on_openability_reconnect_failed is not None:
            asyncio.create_task(self._on_openability_reconnect_failed(
                {
                    "event": "openability.reconnect_failed",
                    "sandbox_id": runtime.sandbox_id,
                    "routing_key": runtime.routing_key,
                    "user_id": str(runtime.metadata.get("user_id") or "").strip(),
                    "session_ids": session_ids,
                    "reason": "runtime-drop",
                }
            ))
        reconnect_timeout = self._get_open_ability_config().reconnect_timeout_seconds
        logger.warning(
            "Dropped runtime after OA reconnect window exhausted; next request will "
            "re-adopt from DCS: sandbox_id=%s session_ids=%s "
            "reconnect_timeout_seconds=%.1f",
            runtime.sandbox_id,
            session_ids,
            reconnect_timeout,
        )

    def _get_sandbox_client(self) -> SandboxClient:
        if self._sandbox_client is None:
            self._sandbox_client = SandboxClient(SandboxConfig.from_env())
        return self._sandbox_client

    def _get_duration_seconds(self) -> int:
        return max(1, int(self._get_sandbox_client().get_config.duration_seconds))

    def _compute_expires_at(self, *, anchor_at: float | None = None) -> float:
        anchor = time.time() if anchor_at is None else float(anchor_at)
        return anchor + self._get_duration_seconds()

    def _sandbox_remaining_seconds(self, runtime: SandboxRuntime) -> float:
        return runtime.expires_at - time.time()

    def _should_refresh_sandbox_duration(self, runtime: SandboxRuntime) -> bool:
        if runtime.status in {SandboxStatus.TERMINATING, SandboxStatus.TERMINATED}:
            return False
        return self._sandbox_remaining_seconds(runtime) < self._idle_timeout_seconds

    async def _maybe_refresh_sandbox_duration(self, runtime: SandboxRuntime) -> None:
        if not self._should_refresh_sandbox_duration(runtime):
            return
        async with runtime.duration_refresh_lock():
            if not self._should_refresh_sandbox_duration(runtime):
                return
            duration_seconds = self._get_duration_seconds()
            remaining_before = self._sandbox_remaining_seconds(runtime)
            try:
                result = await self._get_sandbox_client().refresh_duration(
                    runtime.sandbox_id,
                    duration_seconds=duration_seconds,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Sandbox duration refresh failed: sandbox_id=%s",
                    runtime.sandbox_id,
                )
                return
            if not result.success:
                logger.warning(
                    "Sandbox duration refresh rejected: sandbox_id=%s "
                    "remaining_seconds=%.1f error=%s",
                    runtime.sandbox_id,
                    remaining_before,
                    result.error,
                )
                return
            runtime.expires_at = time.time() + duration_seconds
            logger.info(
                "Sandbox duration refreshed: sandbox_id=%s "
                "remaining_before_seconds=%.1f new_duration_seconds=%d",
                runtime.sandbox_id,
                remaining_before,
                duration_seconds,
            )
            await self._refresh_sandbox_dcs_ttl(runtime)

    async def _refresh_sandbox_dcs_ttl(self, runtime: SandboxRuntime) -> None:
        sandbox_id = runtime.sandbox_id
        try:
            await self._get_dcs_store().refresh_sandbox_ttl(sandbox_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to refresh sandbox metadata DCS TTL: sandbox_id=%s",
                sandbox_id,
            )

        if self._adopt_existing_enabled:
            try:
                await self._get_routing_dcs_store().refresh_routing_ttl(
                    runtime.routing_key,
                    sandbox_id=sandbox_id,
                    gateway_id=self._gateway_instance_id,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Failed to refresh sandbox routing DCS TTL: sandbox_id=%s",
                    sandbox_id,
                )

    async def release_session(
        self,
        session_id: str,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Drop session tracking and purge its workspace snapshot from DCS.

        Does not terminate the sandbox runtime (other sessions may share it).
        """
        sid = str(session_id or "").strip()
        if not sid:
            return {"ok": False, "error": "session_id is required"}

        uid = str(user_id or "").strip() or sid
        routing_key = self._routing_key(uid, sid)

        workspace_obs_url = ""
        workspace_obs_delete_attempted = False
        workspace_obs_deleted = False
        workspace_purged = False
        workspace_store: WorkspaceDcsStore | None = None
        try:
            workspace_store = self._get_workspace_dcs_store()
            record = await workspace_store.get_workspace(sid)
        except Exception:  # noqa: BLE001
            record = None
            logger.exception(
                "Failed to read workspace snapshot on session release: session_id=%s",
                sid,
            )
        if record is not None:
            workspace_obs_url = str(record.url or "").strip()
            if workspace_obs_url:
                workspace_obs_delete_attempted = True
                try:
                    workspace_obs_deleted = bool(
                        await _create_query_url_obs().invoking_osms_delete(
                            workspace_obs_url
                        )
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Failed to delete workspace OBS object on session release: "
                        "session_id=%s url=%s",
                        sid,
                        workspace_obs_url,
                    )
                else:
                    if workspace_obs_deleted:
                        logger.info(
                            "Deleted workspace OBS object on session release: "
                            "session_id=%s url=%s",
                            sid,
                            workspace_obs_url,
                        )
                    else:
                        logger.warning(
                            "OSMS delete workspace OBS object returned false on session release: "
                            "session_id=%s url=%s",
                            sid,
                            workspace_obs_url,
                        )

        try:
            if workspace_store is None:
                workspace_store = self._get_workspace_dcs_store()
            await workspace_store.delete_workspace(sid)
            workspace_purged = True
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to purge workspace snapshot on session release: session_id=%s",
                sid,
            )

        untracked = False
        sandbox_id: str | None = None
        remaining_session_ids: list[str] = []
        async with self._pool_lock:
            runtime = self._runtimes.get(routing_key)
            if runtime is not None:
                sandbox_id = runtime.sandbox_id
                session_ids = runtime.metadata.get("session_ids")
                if isinstance(session_ids, set):
                    untracked = sid in session_ids
                    session_ids.discard(sid)
                self._backup_period_active_sessions(runtime).discard(sid)
                self._backup_period_session_versions(runtime).pop(sid, None)
                self._restored_session_ids(runtime).discard(sid)
                remaining_session_ids = _tracked_session_ids_from_metadata(runtime.metadata)

        return {
            "ok": True,
            "session_id": sid,
            "routing_key": routing_key,
            "sandbox_id": sandbox_id,
            "workspace_purged": workspace_purged,
            "workspace_obs_url": workspace_obs_url,
            "workspace_obs_delete_attempted": workspace_obs_delete_attempted,
            "workspace_obs_deleted": workspace_obs_deleted,
            "untracked": untracked,
            "remaining_session_ids": remaining_session_ids,
            "sandbox": "unchanged",
        }

    def _notify_next_waiter(self) -> None:
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                return
