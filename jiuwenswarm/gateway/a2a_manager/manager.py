"""Lifecycle and management operations for Gateway A2A services."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Protocol

from jiuwenswarm.gateway.channel_manager.protocol.a2a.a2a_connect import (
    A2AChannel,
    A2ADependencyMissingError,
)

from .config import A2AIngressConfigRepository, A2AOutboundSettingsRepository
from .models import (
    A2AIngressConfig,
    A2AIngressError,
    A2AIngressSnapshot,
    A2AIngressState,
)
from .outbound import (
    A2AOutboundDiscoveryService,
    A2AOutboundDispatcher,
    A2AOutboundError,
    A2AOutboundErrorCode,
    A2AOutboundRegistry,
    A2AOutboundRepository,
)

logger = logging.getLogger(__name__)

_TERMINAL_REQUEST_STATUSES = frozenset({"completed", "failed", "canceled"})


def _is_loopback_host(host: str) -> bool:
    normalized = str(host or "").strip().strip("[]").rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


class _ManagedA2AChannel(Protocol):
    channel_id: str

    async def start(self) -> None:
        ...

    async def stop(self) -> None:
        ...


ChannelFactory = Callable[[Any, Any], _ManagedA2AChannel]


class A2AManager:
    """Serializes A2A ingress persistence and channel lifecycle transitions."""

    def __init__(
        self,
        channel_manager: Any,
        router: Any,
        config: A2AIngressConfig,
        *,
        repository: A2AIngressConfigRepository | Any | None = None,
        channel_factory: ChannelFactory = A2AChannel,
        initial_error: A2AIngressError | None = None,
        outbound_repository: A2AOutboundRepository | None = None,
        outbound_registry: A2AOutboundRegistry | None = None,
        outbound_dispatcher: A2AOutboundDispatcher | None = None,
        outbound_settings_repository: A2AOutboundSettingsRepository | Any | None = None,
    ) -> None:
        self._channel_manager = channel_manager
        self._router = router
        self._config = config.validate()
        self._repository = repository or A2AIngressConfigRepository()
        self._channel_factory = channel_factory
        self._channel: _ManagedA2AChannel | None = None
        self._effective_config: A2AIngressConfig | None = None
        self._starting_config: A2AIngressConfig | None = None
        self._start_task: asyncio.Task[None] | None = None
        self._state = (
            A2AIngressState.ERROR if initial_error else A2AIngressState.DISABLED
        )
        self._started_at: float | None = None
        self._last_error: str | None = str(initial_error) if initial_error else None
        self._config_revision = 0
        self._request_history: deque[dict[str, Any]] = deque(maxlen=200)
        self._lock = asyncio.Lock()
        self._pending_callbacks: set[asyncio.Task[None]] = set()
        self._outbound_settings_repository = (
            outbound_settings_repository or A2AOutboundSettingsRepository()
        )
        outbound_settings = self._outbound_settings_repository.load()
        allow_loopback_http = bool(outbound_settings.get("allow_loopback_http", False))
        self._outbound = outbound_registry
        if self._outbound is None and outbound_repository is not None:
            self._outbound = A2AOutboundRegistry(
                outbound_repository,
                discovery_service=A2AOutboundDiscoveryService(
                    allow_loopback_http=allow_loopback_http
                ),
            )
        self._outbound_dispatcher = outbound_dispatcher
        if self._outbound_dispatcher is None and outbound_repository is not None:
            self._outbound_dispatcher = A2AOutboundDispatcher(
                outbound_repository,
                discovery_service=A2AOutboundDiscoveryService(
                    allow_loopback_http=allow_loopback_http
                ),
            )
        self._apply_outbound_settings(allow_loopback_http)

    @property
    def channel(self) -> _ManagedA2AChannel | None:
        return self._channel

    @property
    def outbound_available(self) -> bool:
        return self._outbound is not None

    def _require_outbound(self) -> A2AOutboundRegistry:
        if self._outbound is None:
            raise A2AOutboundError(A2AOutboundErrorCode.STORE_INVALID)
        return self._outbound

    def _require_outbound_dispatcher(self) -> A2AOutboundDispatcher:
        if self._outbound_dispatcher is None:
            raise A2AOutboundError(A2AOutboundErrorCode.MANAGER_UNAVAILABLE)
        return self._outbound_dispatcher

    async def outbound_discover(
        self, url: str, card_path: str | None = None
    ) -> dict[str, Any]:
        return await self._require_outbound().discover(url, card_path)

    async def outbound_get_settings(self) -> dict[str, bool]:
        return dict(self._outbound_settings_repository.load())

    async def outbound_update_settings(
        self, *, allow_loopback_http: bool
    ) -> dict[str, bool]:
        if not isinstance(allow_loopback_http, bool):
            raise A2AOutboundError(A2AOutboundErrorCode.STORE_INVALID)
        self._outbound_settings_repository.save(allow_loopback_http=allow_loopback_http)
        self._apply_outbound_settings(allow_loopback_http)
        return {"allow_loopback_http": allow_loopback_http}

    def _apply_outbound_settings(self, allow_loopback_http: bool) -> None:
        for target in (self._outbound, self._outbound_dispatcher):
            setter = getattr(target, "set_allow_loopback_http", None)
            if callable(setter):
                setter(allow_loopback_http)

    async def outbound_register(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._require_outbound().register(params)

    async def outbound_list(self) -> dict[str, Any]:
        return await self._require_outbound().list_agents()

    async def outbound_get(self, agent_id: str) -> dict[str, Any]:
        return await self._require_outbound().get_agent(agent_id)

    async def outbound_set_user_enabled(
        self, agent_id: str, *, enabled: bool
    ) -> dict[str, Any]:
        return await self._require_outbound().set_user_enabled(agent_id, enabled)

    async def outbound_edit(self, agent_id: str) -> dict[str, Any]:
        return await self._require_outbound().edit_agent(agent_id)

    async def edit_config(self) -> dict[str, Any]:
        async with self._lock:
            return {**self.snapshot().to_dict(), "credential": self._config.credential}

    async def outbound_update(
        self, agent_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._require_outbound().update_agent(agent_id, params)

    async def outbound_refresh(self, agent_id: str) -> dict[str, Any]:
        return await self._require_outbound().refresh_agent(agent_id)

    async def outbound_confirm_revision(
        self, agent_id: str, *, accept: bool
    ) -> dict[str, Any]:
        return await self._require_outbound().confirm_revision(agent_id, accept=accept)

    async def outbound_delete(self, agent_id: str) -> dict[str, Any]:
        return await self._require_outbound().delete_agent(agent_id)

    async def outbound_dispatch_get(self, dispatch_id: str) -> dict[str, Any]:
        return await self._require_outbound().get_dispatch(dispatch_id)

    async def outbound_dispatch_list(self, *, limit: int = 200) -> dict[str, Any]:
        return await self._require_outbound().list_dispatches(limit=limit)

    async def outbound_find_agents(
        self,
        *,
        query: str = "",
        required_skills: list[str] | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        return await self._require_outbound_dispatcher().find_agents(
            query=query,
            required_skills=required_skills,
            limit=limit,
        )

    async def outbound_dispatch_task(
        self,
        *,
        agent_id: str,
        task: str,
        mode: str,
        source_session_id: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return await self._require_outbound_dispatcher().dispatch(
            agent_id=agent_id,
            task=task,
            mode=mode,
            source_session_id=source_session_id,
            reason=reason,
        )

    async def outbound_get_dispatch(
        self, *, dispatch_id: str, source_session_id: str
    ) -> dict[str, Any]:
        return await self._require_outbound_dispatcher().query_dispatch(
            dispatch_id,
            source_session_id=source_session_id,
        )

    def snapshot(self) -> A2AIngressSnapshot:
        config = self._config
        effective = self._effective_config
        desired_base_url = f"http://{config.host}:{config.port}"
        effective_base_url = (
            f"http://{effective.host}:{effective.port}" if effective else None
        )
        security_fields = (
            "auth_type",
            "api_key_header",
            "card_auth_required",
            "credential_hash",
        )
        security_pending_apply = False
        if effective is not None:
            for name in security_fields:
                if getattr(config, name) != getattr(effective, name):
                    security_pending_apply = True
                    break
        return A2AIngressSnapshot(
            enabled=config.enabled,
            state=self._state,
            desired_host=config.host,
            desired_port=config.port,
            desired_rpc_path=config.rpc_path,
            desired_card_path=config.card_path,
            desired_extended_card_path=config.extended_card_path,
            desired_protocol_version=config.protocol_version,
            desired_app_name=config.app_name,
            desired_app_description=config.app_description,
            desired_app_version=config.app_version,
            desired_expose_reasoning=config.expose_reasoning,
            desired_rpc_url=f"{desired_base_url}{config.rpc_path}",
            desired_card_url=f"{desired_base_url}{config.card_path}",
            desired_extended_card_url=(
                f"{desired_base_url}{config.extended_card_path}"
            ),
            effective_host=effective.host if effective else None,
            effective_port=effective.port if effective else None,
            effective_rpc_path=effective.rpc_path if effective else None,
            effective_card_path=effective.card_path if effective else None,
            effective_extended_card_path=(
                effective.extended_card_path if effective else None
            ),
            effective_rpc_url=(
                f"{effective_base_url}{effective.rpc_path}" if effective else None
            ),
            effective_card_url=(
                f"{effective_base_url}{effective.card_path}" if effective else None
            ),
            effective_extended_card_url=(
                f"{effective_base_url}{effective.extended_card_path}"
                if effective
                else None
            ),
            exposure_warning=(
                "A2A ingress is bound to a non-loopback interface; configure network access controls."
                if not _is_loopback_host(config.host)
                else None
            ),
            started_at=self._started_at,
            last_error=self._last_error,
            config_revision=self._config_revision,
            desired_auth_type=config.auth_type,
            desired_api_key_header=config.api_key_header,
            desired_card_auth_required=config.card_auth_required,
            credential_configured=bool(config.credential_hash),
            effective_auth_type=effective.auth_type if effective else None,
            effective_card_auth_required=effective.card_auth_required
            if effective
            else None,
            security_pending_apply=security_pending_apply,
        )

    def history(self, limit: int = 100) -> dict[str, Any]:
        """Return newest-first request lifecycle metadata for this Gateway process."""
        normalized_limit = max(1, min(int(limit), 200))
        items = list(reversed(self._request_history))[:normalized_limit]
        return {
            "items": [dict(item) for item in items],
            "total": len(self._request_history),
        }

    def _record_request_event(self, event: dict[str, Any]) -> None:
        request_id = str(event.get("request_id") or "").strip()
        if not request_id:
            return
        existing = next(
            (
                item
                for item in self._request_history
                if item["request_id"] == request_id
            ),
            None,
        )
        if (
            existing is not None
            and existing.get("status") in _TERMINAL_REQUEST_STATUSES
        ):
            return
        if existing is None:
            started_at = float(event.get("started_at") or time.time())
            existing = {
                "request_id": request_id,
                "context_id": str(event.get("context_id") or "") or None,
                "message_id": str(event.get("message_id") or "") or None,
                "operation": "message",
                "status": "processing",
                "started_at": started_at,
                "finished_at": None,
                "duration_ms": None,
                "error": None,
            }
            self._request_history.append(existing)
        for key in ("context_id", "message_id", "status", "finished_at", "error"):
            if key in event:
                existing[key] = event[key]
        if existing.get("finished_at") is not None:
            existing["duration_ms"] = max(
                0,
                round(
                    (float(existing["finished_at"]) - float(existing["started_at"]))
                    * 1000
                ),
            )

    async def start_from_config(self) -> A2AIngressSnapshot:
        """Start during Gateway boot without delaying boot on A2A failures."""
        async with self._lock:
            if self._channel is not None:
                return self.snapshot()
            if not self._config.enabled:
                if self._last_error is None:
                    self._state = A2AIngressState.DISABLED
                return self.snapshot()
            await self._create_and_start_locked(wait=False)
            return self.snapshot()

    async def update(
        self, patch: dict[str, Any], *, apply: bool = False
    ) -> A2AIngressSnapshot:
        async with self._lock:
            next_config = self._config.with_patch(patch)
            self._persist_locked(next_config)
            if apply:
                if next_config.enabled:
                    await self._reload_locked()
                else:
                    await self._disable_locked()
            return self.snapshot()

    async def enable(self) -> A2AIngressSnapshot:
        async with self._lock:
            if not self._config.enabled:
                self._persist_locked(self._config.with_patch({"enabled": True}))
            if self._state == A2AIngressState.RUNNING:
                if self._effective_config != self._config:
                    await self._reload_locked()
                return self.snapshot()
            await self._dispose_channel_locked()
            await self._create_and_start_locked(wait=True)
            return self.snapshot()

    async def disable(self) -> A2AIngressSnapshot:
        async with self._lock:
            if self._config.enabled:
                self._persist_locked(self._config.with_patch({"enabled": False}))
            await self._disable_locked()
            return self.snapshot()

    async def reload(self) -> A2AIngressSnapshot:
        async with self._lock:
            if not self._config.enabled:
                await self._disable_locked()
                return self.snapshot()
            await self._reload_locked()
            return self.snapshot()

    def _persist_locked(self, config: A2AIngressConfig) -> None:
        self._repository.save(config)
        self._config = config
        self._config_revision += 1

    async def _reload_locked(self) -> None:
        await self._dispose_channel_locked()
        await self._create_and_start_locked(wait=True)

    async def _disable_locked(self) -> None:
        await self._dispose_channel_locked()
        self._state = A2AIngressState.DISABLED
        self._started_at = None
        self._last_error = None

    async def _create_and_start_locked(self, *, wait: bool) -> None:
        effective_config = self._config
        channel = self._channel_factory(
            effective_config.to_channel_config(), self._router
        )
        set_request_observer = getattr(channel, "set_request_observer", None)
        if callable(set_request_observer):
            set_request_observer(self._record_request_event)
        set_runtime_error_observer = getattr(
            channel, "set_runtime_error_observer", None
        )
        if callable(set_runtime_error_observer):
            set_runtime_error_observer(
                lambda exc: self._record_runtime_error(channel, exc)
            )
        self._channel_manager.register_channel(channel)
        self._channel = channel
        self._starting_config = effective_config
        self._state = A2AIngressState.STARTING
        self._last_error = None
        logger.info(
            "a2a.ingress starting: rpc_url=http://%s:%s%s",
            effective_config.host,
            effective_config.port,
            effective_config.rpc_path,
        )
        task = asyncio.create_task(channel.start(), name="a2a-channel")
        self._start_task = task
        task.add_done_callback(self._on_start_done)
        if wait:
            await self._await_start_locked(task)

    async def _await_start_locked(self, task: asyncio.Task[None]) -> None:
        try:
            await task
        except Exception as exc:  # noqa: BLE001
            self._record_start_error(exc)
            raise self._as_operation_error(exc) from exc
        if self._start_task is task:
            self._state = A2AIngressState.RUNNING
            self._started_at = time.time()
            self._effective_config = self._starting_config

    async def _dispose_channel_locked(self) -> None:
        channel, task = self._channel, self._start_task
        self._channel = None
        self._start_task = None
        self._effective_config = None
        self._starting_config = None
        if channel is None:
            return
        self._state = A2AIngressState.STOPPING
        propagate_cancellation = False
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                if (
                    asyncio.current_task() is not None
                    and asyncio.current_task().cancelling()
                ):
                    propagate_cancellation = True
            except Exception:  # noqa: BLE001
                logger.debug("a2a.ingress start task cleanup failed", exc_info=True)
        try:
            await channel.stop()
        finally:
            self._channel_manager.unregister_channel(channel.channel_id)
        if propagate_cancellation:
            raise asyncio.CancelledError

    def _track_callback_task(self, task: asyncio.Task[None]) -> None:
        self._pending_callbacks.add(task)
        task.add_done_callback(self._pending_callbacks.discard)

    def _on_start_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            callback = asyncio.create_task(
                self._record_start_success(task), name="a2a-ingress-started"
            )
        else:
            callback = asyncio.create_task(
                self._record_background_error(task, exc),
                name="a2a-ingress-start-failed",
            )
        self._track_callback_task(callback)

    async def _record_start_success(self, task: asyncio.Task[None]) -> None:
        async with self._lock:
            if self._start_task is task and self._state == A2AIngressState.STARTING:
                self._state = A2AIngressState.RUNNING
                self._started_at = time.time()
                self._effective_config = self._starting_config
                logger.info("a2a.ingress running")

    async def _record_background_error(
        self, task: asyncio.Task[None], exc: Exception
    ) -> None:
        async with self._lock:
            if self._start_task is task:
                self._record_start_error(exc)
        logger.error("a2a.ingress start failed: %s", exc, exc_info=exc)

    async def _record_runtime_error(
        self, channel: _ManagedA2AChannel, exc: BaseException
    ) -> None:
        async with self._lock:
            if self._channel is not channel:
                return
            await self._dispose_channel_locked()
            self._record_start_error(
                A2AIngressError(
                    "A2A_RUNTIME_FAILED",
                    "A2A ingress server stopped unexpectedly",
                )
            )
        logger.error(
            "a2a.ingress runtime failed: %s",
            exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    def _record_start_error(self, exc: Exception) -> None:
        error = self._as_operation_error(exc)
        self._state = A2AIngressState.ERROR
        self._started_at = None
        self._last_error = str(error)

    @staticmethod
    def _as_operation_error(exc: Exception) -> A2AIngressError:
        message = str(exc) or type(exc).__name__
        if isinstance(exc, A2AIngressError):
            return exc
        if isinstance(exc, A2ADependencyMissingError):
            return A2AIngressError("A2A_DEPENDENCY_MISSING", message)
        if isinstance(exc, OSError):
            return A2AIngressError("A2A_BIND_FAILED", message)
        if isinstance(exc, (TypeError, ValueError)):
            return A2AIngressError("A2A_CONFIG_INVALID", message)
        return A2AIngressError("A2A_START_FAILED", message)

    async def stop(self) -> None:
        """Gateway shutdown; does not persist a configuration change."""
        async with self._lock:
            await self._dispose_channel_locked()
            self._state = (
                A2AIngressState.DISABLED
                if not self._config.enabled
                else A2AIngressState.ERROR
            )
