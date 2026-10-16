# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Own the registry watcher task and hand the desired port map to the Gateway."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from jiuwenswarm.extensions.agentos.registry_watcher.client import RegistryWatcherClient

logger = logging.getLogger(__name__)

# Receives the full desired ``{agent_type: port}`` map.
ApplyHandler = Callable[[dict[str, int]], Awaitable[None]]


class RegistryWatcherService:
    """Watch the registry prefix; apply the desired map via an injected callback."""

    def __init__(
        self,
        *,
        etcd_endpoints: list[str],
        on_desired: ApplyHandler,
        namespace: str | None = None,
        client: RegistryWatcherClient | None = None,
    ) -> None:
        self._endpoints = [str(e).strip() for e in etcd_endpoints if str(e).strip()]
        self._namespace = namespace
        self._on_desired = on_desired
        self._client = client
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._endpoints)

    async def start(self) -> None:
        if self._task is not None:
            return
        if not self._endpoints:
            logger.info("[RegistryWatcher] disabled: no etcd endpoints configured")
            return

        client = self._client or RegistryWatcherClient(
            etcd_endpoints=self._endpoints, namespace=self._namespace
        )
        self._task = asyncio.create_task(self._watch(client), name="registry-watcher")
        logger.info("[RegistryWatcher] watcher started endpoints=%s", self._endpoints)

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _watch(self, client: RegistryWatcherClient) -> None:
        async def _apply(desired: dict[str, int]) -> None:
            try:
                await self._on_desired(dict(desired))
            except Exception:  # noqa: BLE001 - never take the gateway down
                logger.exception("[RegistryWatcher] apply failed")

        try:
            await client.watch_loop(_apply)
        except Exception as exc:  # noqa: BLE001 - watcher must not crash the gateway
            logger.warning("[RegistryWatcher] watcher exited: %s", exc)
        finally:
            await client.aclose()
            if self._task is asyncio.current_task():
                self._task = None


__all__ = ["RegistryWatcherService", "ApplyHandler"]
