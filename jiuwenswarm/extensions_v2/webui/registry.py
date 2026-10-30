# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""活跃渲染实例登记：实例定位、revision、同实例串行与容量淘汰。"""

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

from jiuwenswarm.extensions_v2.webui.constants import MAX_INSTANCES_PER_SESSION
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
    """一个渲染实例的最近一帧状态（只保留当前值，不留历史）。"""

    target: RenderTarget
    revision: int = 0
    mode: RenderMode = RenderMode.REPLACE
    root: RenderNode | None = None
    updated_at: float = 0.0


@dataclass(frozen=True, slots=True)
class RenderClaim:
    """一次串行化占用的结果：实例状态、待淘汰实例与新建标记。"""

    instance: RenderInstance
    evicted: tuple[RenderInstance, ...] = field(default_factory=tuple)
    created: bool = False


class ActiveRenderRegistry:
    """进程内渲染登记表：定位实例、分配 revision、容量淘汰与按范围清理。"""

    def __init__(self, max_instances_per_session: int = MAX_INSTANCES_PER_SESSION) -> None:
        self._instances: dict[str, RenderInstance] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_users: dict[str, int] = {}
        self._max_per_session = max_instances_per_session

    def contains(self, target: RenderTarget) -> bool:
        return target.key in self._instances

    def get(self, target: RenderTarget) -> RenderInstance | None:
        return self._instances.get(target.key)

    def snapshot(self) -> tuple[RenderInstance, ...]:
        return tuple(self._instances.values())

    @asynccontextmanager
    async def claim(self, target: RenderTarget) -> AsyncIterator[RenderClaim]:
        """串行占用一个实例，返回当前状态与待淘汰实例；跨实例可并发。"""

        async with self._lock_for(target.key):
            evicted = self._evict_candidates(target)
            instance = self._instances.get(target.key)
            created = instance is None
            if instance is None:
                instance = RenderInstance(target=target)
                self._instances[target.key] = instance
            instance.updated_at = time.monotonic()
            yield RenderClaim(instance=instance, evicted=evicted, created=created)

    def commit(self, instance: RenderInstance, request: UiRenderInput) -> RenderFrame:
        """提交一次成功下发：revision 单调递增并产出帧。"""

        instance.revision += 1
        instance.mode = request.mode
        instance.root = _snapshot_content(request.root) if request.mode is RenderMode.REPLACE else None
        instance.updated_at = time.monotonic()
        return RenderFrame(
            target=instance.target,
            revision=instance.revision,
            mode=instance.mode,
            root=instance.root,
        )

    def restore(self, instance: RenderInstance, previous: RenderInstance) -> None:
        """下发失败时恢复提交前的状态，避免 revision 空跳。"""

        instance.revision = previous.revision
        instance.mode = previous.mode
        instance.root = previous.root
        instance.updated_at = previous.updated_at

    @asynccontextmanager
    async def evict(self, expected: RenderInstance) -> AsyncIterator[bool]:
        """Hold the instance lock and confirm it is still the revision to evict."""

        async with self._lock_for(expected.target.key):
            current = self._instances.get(expected.target.key)
            yield current is expected and current.revision == expected.revision

    def drop_instance(self, target: RenderTarget) -> bool:
        """移除单个实例（实例不再活跃）。"""

        removed = self._instances.pop(target.key, None) is not None
        if removed and self._lock_users.get(target.key, 0) == 0:
            self._locks.pop(target.key, None)
        return removed

    def drop_session(self, session_id: str) -> tuple[RenderInstance, ...]:
        """会话结束或归档时清理该会话全部实例。"""

        return self._drop_matching(lambda target: target.session_id == session_id)

    def drop_package(self, package_id: str) -> tuple[RenderInstance, ...]:
        """插件卸载时清理该包的全部实例。"""

        return self._drop_matching(lambda target: target.package_id == package_id)

    @asynccontextmanager
    async def _lock_for(self, key: str) -> AsyncIterator[None]:
        """按实例引用计数持有锁；最后一个使用者离开且实例已删除时回收锁。"""

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

    def _drop_matching(self, predicate) -> tuple[RenderInstance, ...]:
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
        """返回同会话最久未更新的实例；提交成功后再由调用方真正移除。"""

        if self._max_per_session <= 0 or target.key in self._instances:
            return ()
        candidates = [
            instance
            for instance in self._instances.values()
            if instance.target.session_id == target.session_id
        ]
        if len(candidates) < self._max_per_session:
            return ()
        candidates.sort(key=lambda instance: instance.updated_at)
        excess = len(candidates) - self._max_per_session + 1
        return tuple(candidates[:excess])


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
            action=_snapshot_content(value.action),
            events=_snapshot_content(value.events),
        )
    if isinstance(value, Mapping):
        return MappingProxyType({key: _snapshot_content(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_snapshot_content(item) for item in value)
    return value
