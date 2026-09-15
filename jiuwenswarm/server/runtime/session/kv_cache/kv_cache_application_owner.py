# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Application-scoped ownership and publication of KVC lifecycle resources."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import Enum

from jiuwenswarm.runtime.session_lifecycle import RuntimeParticipantRegistry
from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_session_lifecycle_participant import (
    KVCacheSessionLifecycleParticipant,
)


class KVCacheApplicationState(str, Enum):
    """Publication state of the process-scoped KVC participant.

    ``TERMINAL_ONLY`` keeps deletion cleanup while suppressing activity hints.
    ``SUSPENDED`` publishes no participant at all, which is the AgentServer
    stopped state and guarantees that offline deletion performs no remote KVC
    management. ``CLOSED`` is reserved for final application-owned teardown.
    """

    DISABLED = "disabled"
    ENABLED = "enabled"
    TERMINAL_ONLY = "terminal_only"
    SUSPENDED = "suspended"
    CLOSING = "closing"
    CLOSED = "closed"


class _RuntimeLease:
    """Idempotent attachment handle owned by one Session Runtime registry."""

    def __init__(
        self,
        release_registry: Callable[
            [RuntimeParticipantRegistry],
            Awaitable[None],
        ],
        registry: RuntimeParticipantRegistry,
    ) -> None:
        self._release_registry = release_registry
        self._registry = registry
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._release_registry(self._registry)


class KVCacheApplicationOwner:
    """Own participant publication and final closure of the shared KVC runtime.

    A process may create more than one Session Runtime/registry over time, but
    they all observe the same KVC participant. Registries only borrow that
    participant through a lease; releasing a lease must never close the shared
    KVC runtime.
    """

    def __init__(self) -> None:
        self._state_lock = asyncio.Lock()
        self._registrations: set[RuntimeParticipantRegistry] = set()
        self._participant: KVCacheSessionLifecycleParticipant | None = None
        self._state = KVCacheApplicationState.DISABLED

    @property
    def state(self) -> KVCacheApplicationState:
        return self._state

    @property
    def closed(self) -> bool:
        return self._state is KVCacheApplicationState.CLOSED

    @staticmethod
    def _affinity_enabled() -> bool:
        # Configuration and provider imports stay lazy so KVC OFF has no model
        # construction side effects and remains a cheap, fail-open path.
        try:
            from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_model_provider import (
                is_kv_cache_affinity_enabled,
            )

            return is_kv_cache_affinity_enabled()
        except Exception:
            return False

    def _enable_synchronously(self) -> bool:
        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_application_runtime import (
            get_kv_cache_runtime,
        )

        runtime = get_kv_cache_runtime()
        if runtime is None:
            return False
        self._participant = KVCacheSessionLifecycleParticipant(runtime)
        self._state = KVCacheApplicationState.ENABLED
        return True

    def attach_runtime(
        self,
        registry: RuntimeParticipantRegistry,
    ) -> _RuntimeLease:
        """Attach one registry without activating KVC or creating a model."""

        if self._state in {
            KVCacheApplicationState.CLOSING,
            KVCacheApplicationState.CLOSED,
        }:
            raise RuntimeError("KVC application owner is closing")
        self._registrations.add(registry)
        self._publish(registry)
        return _RuntimeLease(self._release_registry, registry)

    async def activate_from_config(self) -> None:
        """Lazily create KVC resources when the owning service starts."""
        await self.set_enabled(self._affinity_enabled())

    def _publish(self, registry: RuntimeParticipantRegistry) -> None:
        # Replacing the complete snapshots makes state transitions atomic from
        # the Runtime's point of view and prevents duplicate registrations.
        participant = self._participant
        if self._state is KVCacheApplicationState.ENABLED and participant is not None:
            registry.replace_session_participants(
                activity=(participant,), delete=(participant,)
            )
        elif (
            self._state is KVCacheApplicationState.TERMINAL_ONLY
            and participant is not None
        ):
            registry.replace_session_participants(activity=(), delete=(participant,))
        else:
            registry.replace_session_participants(activity=(), delete=())

    async def set_enabled(self, enabled: bool) -> None:
        """Apply a live config transition while retaining terminal cleanup."""

        async with self._state_lock:
            if self._state in {
                KVCacheApplicationState.CLOSING,
                KVCacheApplicationState.CLOSED,
            }:
                raise RuntimeError("KVC application owner is closing")
            if enabled:
                if self._state is KVCacheApplicationState.ENABLED:
                    return
                if self._state is KVCacheApplicationState.DISABLED:
                    if not self._enable_synchronously():
                        return
                else:
                    self._state = KVCacheApplicationState.ENABLED
            else:
                if self._state is KVCacheApplicationState.DISABLED:
                    return
                if self._state is KVCacheApplicationState.TERMINAL_ONLY:
                    return
                self._state = KVCacheApplicationState.TERMINAL_ONLY
            for registry in tuple(self._registrations):
                self._publish(registry)

    async def _release_registry(
        self,
        registry: RuntimeParticipantRegistry,
    ) -> None:
        async with self._state_lock:
            registry.replace_session_participants(activity=(), delete=())
            self._registrations.discard(registry)

    async def suspend(self) -> None:
        """Detach KVC from a stopped AgentServer without remote management I/O."""
        async with self._state_lock:
            if self._state in {
                KVCacheApplicationState.DISABLED,
                KVCacheApplicationState.SUSPENDED,
                KVCacheApplicationState.CLOSED,
            }:
                return
            if self._state is KVCacheApplicationState.CLOSING:
                raise RuntimeError("KVC application owner is closing")
            self._state = KVCacheApplicationState.SUSPENDED
            for registry in tuple(self._registrations):
                self._publish(registry)
            participant = self._participant
            if participant is not None:
                # Cancels only JiuwenSwarm-owned scheduling tasks.  In
                # particular this does not call KVCacheRuntime.close(), whose
                # contract includes remote root eviction.
                await participant.close()

    async def close(self) -> None:
        """Perform final application teardown, including runtime root cleanup."""

        async with self._state_lock:
            if self._state is KVCacheApplicationState.CLOSED:
                return
            self._state = KVCacheApplicationState.CLOSING
            for registry in tuple(self._registrations):
                registry.replace_session_participants(activity=(), delete=())
            self._registrations.clear()
        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_application_runtime import (
            close_kv_cache_runtime,
        )

        try:
            participant = self._participant
            if participant is not None:
                await participant.close()
        finally:
            try:
                await close_kv_cache_runtime()
            finally:
                async with self._state_lock:
                    self._participant = None
                    self._state = KVCacheApplicationState.CLOSED


_OWNER: KVCacheApplicationOwner | None = None


def get_kv_cache_application_owner() -> KVCacheApplicationOwner:
    global _OWNER
    if _OWNER is None or _OWNER.closed:
        _OWNER = KVCacheApplicationOwner()
    return _OWNER


__all__ = [
    "KVCacheApplicationOwner",
    "KVCacheApplicationState",
    "get_kv_cache_application_owner",
]
