# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""etcd range + watch for the registry's 3rd-agent image records.

Emits the *full* desired ``{agent_type: port}`` map: once after the initial
range, then on every watch event. Emitting the whole map (rather than per-key
deltas) keeps the consumer idempotent and makes reconnect alignment trivial.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from gateway.common.etcd.client import EtcdJsonClient, prefix_range_end
from gateway.extensions.agentos.registry_watcher import registry_prefix
from gateway.extensions.agentos.registry_watcher.decode import decode_record

logger = logging.getLogger(__name__)

_CONNECT_INITIAL_DELAY = 2.0
_CONNECT_MAX_DELAY = 60.0
_DEFAULT_FETCH_TIMEOUT = 10.0

# on_change receives the full desired ``{agent_type: port}`` map.
DesiredHandler = Callable[[dict[str, int]], Awaitable[None]]


class RegistryWatcherClient:
    """Range + watch the registry prefix; emit the desired port map."""

    def __init__(
        self,
        *,
        etcd_endpoints: list[str],
        namespace: str | None = None,
        fetch_timeout_seconds: float = _DEFAULT_FETCH_TIMEOUT,
        etcd_client_factory: Callable[..., EtcdJsonClient] | None = None,
    ) -> None:
        self._endpoints = [str(e).strip() for e in etcd_endpoints if str(e).strip()]
        self._prefix = registry_prefix(namespace).encode("utf-8")
        self._timeout = float(fetch_timeout_seconds)
        self._factory = etcd_client_factory or EtcdJsonClient
        self._client: EtcdJsonClient | None = None

    @property
    def enabled(self) -> bool:
        """False when no endpoint is configured; the caller then skips watching."""
        return bool(self._endpoints)

    def _ensure_client(self) -> EtcdJsonClient:
        if self._client is None:
            self._client = self._factory(self._endpoints, timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    async def fetch_once(self) -> dict[str, int]:
        """Read the whole prefix once into ``{agent_type: port}``."""
        desired, _revision = await self._fetch_with_revision()
        return desired

    async def _fetch_with_revision(self) -> tuple[dict[str, int], int]:
        """Range the prefix and return ``(desired, snapshot_revision)``."""
        result = await self._ensure_client().range(
            self._prefix, range_end=prefix_range_end(self._prefix)
        )
        desired: dict[str, int] = {}
        for kv in result.kvs:
            decoded = decode_record(kv.value, kv.key.decode("utf-8", "replace"))
            if decoded is not None:
                desired[decoded[0]] = decoded[1]
        return desired, int(result.revision or 0)

    async def watch_loop(self, on_change: DesiredHandler) -> None:
        """Emit the desired map, then on every change; reconnect with backoff.

        The etcd key is an opaque ``image_<sid>`` (one per version), so a delta
        cannot be mapped back to a ``name``. Every event therefore re-ranges the
        whole prefix and emits the rebuilt map; a reconnect re-ranges too, so
        events missed while the stream was down are reconciled.
        """
        delay = _CONNECT_INITIAL_DELAY
        while True:
            try:
                if not self._endpoints:
                    raise RuntimeError("etcd endpoints are empty")

                desired, snapshot_revision = await self._fetch_with_revision()
                await on_change(dict(desired))

                start_revision = snapshot_revision + 1 if snapshot_revision > 0 else None
                async for _events in self._ensure_client().watch_prefix(
                    self._prefix, start_revision=start_revision
                ):
                    delay = _CONNECT_INITIAL_DELAY
                    desired, _ = await self._fetch_with_revision()
                    await on_change(dict(desired))
                raise RuntimeError("etcd watch stream ended")
            except Exception as exc:  # noqa: BLE001 - keep the watcher alive
                logger.warning(
                    "[RegistryWatcher] watch/connect retry in %.1fs: %s", delay, exc
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, _CONNECT_MAX_DELAY)


__all__ = ["RegistryWatcherClient", "DesiredHandler"]
