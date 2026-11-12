# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Track active render instances, revisions, locking and capacity eviction."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from jiuwenswarm.extensions_v2.webui.constants import MAX_INSTANCES_PER_SESSION, SlotScope
from jiuwenswarm.extensions_v2.webui.models import (
    RenderFrame,
    RenderMode,
    RenderNode,
    RenderTarget,
    UiRenderInput,
)

logger = logging.getLogger(__name__)


@dataclass
class RenderInstance:
    """The latest frame state for one render instance, without history."""

    target: RenderTarget
    revision: int = 0
    mode: RenderMode = RenderMode.REPLACE
    root: RenderNode | None = None
    updated_at: float = 0.0
    visible_when: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RenderClaim:
    """The instance, eviction candidates and creation flag for one claim."""

    instance: RenderInstance
    evicted: tuple[RenderInstance, ...] = field(default_factory=tuple)
    created: bool = False


class ActiveRenderRegistry:
    """Manage in-process render instances, revisions, eviction and scoped cleanup."""

    def __init__(self, max_instances_per_session: int = MAX_INSTANCES_PER_SESSION) -> None:
        self._instances: dict[str, RenderInstance] = {}
        self._revisions: dict[str, tuple[RenderTarget, int]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_users: dict[str, int] = {}
        self._max_per_session = max_instances_per_session

    def contains(self, target: RenderTarget) -> bool:
        return target.key in self._instances

    def get(self, target: RenderTarget) -> RenderInstance | None:
        return self._instances.get(target.key)

    def snapshot(self) -> tuple[RenderInstance, ...]:
        return tuple(self._instances.values())

    def clear(self) -> None:
        """Release active instances and revision history when the runtime stops."""

        self._instances.clear()
        self._revisions.clear()
        for key in list(self._locks):
            if self._lock_users.get(key, 0) == 0:
                self._locks.pop(key, None)

    @asynccontextmanager
    async def claim(self, target: RenderTarget) -> AsyncIterator[RenderClaim]:
        """Claim one instance exclusively; independent instances may run concurrently."""

        async with self._lock_for(target.key):
            evicted = self._evict_candidates(target)
            instance = self._instances.get(target.key)
            created = instance is None
            if instance is None:
                revision = self._revisions.get(target.key, (target, 0))[1] if _retains_revision(target) else 0
                instance = RenderInstance(target=target, revision=revision)
                self._instances[target.key] = instance
            instance.updated_at = time.monotonic()
            yield RenderClaim(instance=instance, evicted=evicted, created=created)

    def commit(self, instance: RenderInstance, request: UiRenderInput) -> RenderFrame:
        """Commit a delivered update and return its next revision frame."""

        instance.revision += 1
        if _retains_revision(instance.target):
            self._revisions[instance.target.key] = (instance.target, instance.revision)
        instance.mode = request.mode
        instance.root = _snapshot_content(request.root) if request.mode is RenderMode.REPLACE else None
        instance.visible_when = _snapshot_content(request.visible_when)
        instance.updated_at = time.monotonic()
        return RenderFrame(
            target=instance.target,
            revision=instance.revision,
            mode=instance.mode,
            root=instance.root,
            visible_when=instance.visible_when,
        )

    def restore(self, instance: RenderInstance, previous: RenderInstance) -> None:
        """Restore the previous state after a failed delivery without skipping revisions."""

        instance.revision = previous.revision
        if _retains_revision(instance.target):
            self._revisions[instance.target.key] = (instance.target, instance.revision)
        instance.mode = previous.mode
        instance.root = previous.root
        instance.visible_when = previous.visible_when
        instance.updated_at = previous.updated_at

    @asynccontextmanager
    async def evict(self, expected: RenderInstance) -> AsyncIterator[bool]:
        """Hold the instance lock and confirm it is still the revision to evict."""

        async with self._lock_for(expected.target.key):
            current = self._instances.get(expected.target.key)
            yield current is expected and current.revision == expected.revision

    def drop_instance(self, target: RenderTarget, *, retired_revision: int | None = None) -> bool:
        """Remove one instance from the active registry."""

        instance = self._instances.pop(target.key, None)
        removed = instance is not None
        if instance is not None and _retains_revision(instance.target):
            revision = instance.revision if retired_revision is None else retired_revision
            self._revisions[target.key] = (instance.target, revision)
        if removed and self._lock_users.get(target.key, 0) == 0:
            self._locks.pop(target.key, None)
        return removed

    def drop_session(self, session_id: str) -> tuple[RenderInstance, ...]:
        """Drop session instances when their conversation ends or is archived."""

        return self._drop_matching(
            lambda target: (
                target.resolved_scope != SlotScope.APPLICATION.value and target.session_id == session_id
            ),
            forget_revisions=True,
        )

    def drop_package(self, package_id: str) -> tuple[RenderInstance, ...]:
        """Drop all instances owned by an unloaded package."""

        return self._drop_matching(lambda target: target.package_id == package_id)

    def drop_connection(self, connection_id: str) -> tuple[RenderInstance, ...]:
        """Release application instances without touching any chat session."""
        return self._drop_matching(
            lambda target: (
                target.resolved_scope == SlotScope.APPLICATION.value
                and target.connection_id == connection_id
            ),
            forget_revisions=True,
        )

    @asynccontextmanager
    async def _lock_for(self, key: str) -> AsyncIterator[None]:
        """Hold a reference-counted lock and reclaim it when an absent instance has no users."""

        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        self._lock_users[key] = self._lock_users.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            remaining = self._lock_users.get(key, 1) - 1
            if remaining > 0:
                self._lock_users[key] = remaining
            else:
                self._lock_users.pop(key, None)
                if key not in self._instances and self._locks.get(key) is lock:
                    self._locks.pop(key, None)

    def _drop_matching(self, predicate, *, forget_revisions: bool = False) -> tuple[RenderInstance, ...]:
        if forget_revisions:
            for key, (target, _) in list(self._revisions.items()):
                if predicate(target):
                    self._revisions.pop(key, None)
        dropped: list[RenderInstance] = []
        for key, instance in list(self._instances.items()):
            if not predicate(instance.target):
                continue
            dropped.append(instance)
            self._instances.pop(key, None)
            if self._lock_users.get(key, 0) == 0:
                self._locks.pop(key, None)
        return tuple(dropped)

    def _evict_candidates(self, target: RenderTarget) -> tuple[RenderInstance, ...]:
        """Select the oldest instances in the capacity group for removal after delivery."""

        if self._max_per_session <= 0 or target.key in self._instances:
            return ()
        candidates = [
            instance
            for instance in self._instances.values()
            if _same_capacity_group(instance.target, target)
        ]
        if len(candidates) < self._max_per_session:
            return ()
        candidates.sort(key=lambda instance: instance.updated_at)
        excess = len(candidates) - self._max_per_session + 1
        return tuple(candidates[:excess])


def _same_capacity_group(left: RenderTarget, right: RenderTarget) -> bool:
    """Session instances share a chat budget. Application instances share a connection budget."""

    if right.resolved_scope == SlotScope.APPLICATION.value:
        return (
            left.resolved_scope == SlotScope.APPLICATION.value
            and left.connection_id == right.connection_id
        )
    return (
        left.resolved_scope != SlotScope.APPLICATION.value
        and left.session_id == right.session_id
    )


def _retains_revision(target: RenderTarget) -> bool:
    """Keep application revisions until their browser connection closes."""

    return target.resolved_scope == SlotScope.APPLICATION.value


def log_eviction(instance: RenderInstance) -> None:
    """Record a capacity eviction after the new frame has been delivered."""

    logger.warning("[WebUI] 淘汰最久未更新实例: %s", instance.target.key)


def _snapshot_content(value: Any) -> Any:
    """Retain the delivered declarations independently of mutable plugin inputs."""
    if isinstance(value, RenderNode):
        return RenderNode(
            component=value.component,
            props=_snapshot_content(value.props),
            children=tuple(_snapshot_content(child) for child in value.children),
            events=_snapshot_content(value.events),
        )
    if isinstance(value, Mapping):
        return MappingProxyType({key: _snapshot_content(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_snapshot_content(item) for item in value)
    return value
