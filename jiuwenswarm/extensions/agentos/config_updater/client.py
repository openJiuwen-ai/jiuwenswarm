# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""etcd pull and watch for the AgentOS data-plane config.

I/O only; application decisions live in ``service.py``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import yaml

from jiuwenswarm.common.etcd.client import EtcdJsonClient, EtcdKv
from jiuwenswarm.extensions.agentos.config_updater import CONFIG_SYNC_KEY
from jiuwenswarm.extensions.agentos.config_updater.merge import extract_section

logger = logging.getLogger(__name__)

_CONNECT_INITIAL_DELAY = 2.0
_CONNECT_MAX_DELAY = 60.0
_DEFAULT_FETCH_TIMEOUT = 10.0


@dataclass
class FetchResult:
    """One fetched revision: the component section plus stripped metadata."""

    section: dict[str, Any]
    metadata: dict[str, Any]
    mod_revision: int


class ConfigUpdaterClient:
    """Pull and watch one etcd key; decode the gateway section.

    Inputs are constructor-injected; this class never reads ``config.yaml``
    (that would create a cycle through ``get_config``).
    """

    def __init__(
        self,
        *,
        etcd_endpoints: list[str],
        key: str = CONFIG_SYNC_KEY,
        fetch_timeout_seconds: float = _DEFAULT_FETCH_TIMEOUT,
        etcd_client_factory: Callable[..., EtcdJsonClient] | None = None,
    ) -> None:
        self._endpoints = [
            str(item).strip() for item in etcd_endpoints if str(item).strip()
        ]
        self._key = key
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

    async def fetch_once(self) -> FetchResult | None:
        """Read the key once; ``None`` when absent. Transport errors propagate."""
        fetched, _revision = await self._fetch_once_with_revision()
        return fetched

    async def _fetch_once_with_revision(self) -> tuple[FetchResult | None, int]:
        """Read the key and return the etcd snapshot revision used for watch."""
        key_bytes = self._key.encode("utf-8")
        # No range_end -> exact single-key read.
        result = await self._ensure_client().range(key_bytes)
        kv = _pick_exact(result.kvs, key_bytes)
        if kv is None:
            return None, int(result.revision or 0)
        return self._decode(kv), int(result.revision or 0)

    async def watch_loop(
        self,
        on_event: Callable[[FetchResult], Awaitable[None]],
    ) -> None:
        """Pull once, then watch; reconnect with backoff when the stream dies.

        Two things worth knowing:
        1. ``watch_prefix`` is prefix-scoped, so events are filtered to the
           exact key -- otherwise ``.../data-plane-backup`` would also match.
        2. The watch starts at the range snapshot revision + 1, closing the
           range-to-watch race even when applying the initial value is slow.
        """
        delay = _CONNECT_INITIAL_DELAY
        while True:
            try:
                if not self._endpoints:
                    raise RuntimeError("etcd endpoints are empty")

                fetched, snapshot_revision = await self._fetch_once_with_revision()
                if fetched is not None:
                    await on_event(fetched)

                key_bytes = self._key.encode("utf-8")
                start_revision = snapshot_revision + 1 if snapshot_revision > 0 else None
                async for events in self._ensure_client().watch_prefix(
                    key_bytes,
                    start_revision=start_revision,
                ):
                    kv = _pick_exact(events, key_bytes)
                    if kv is not None:
                        delay = _CONNECT_INITIAL_DELAY
                        await on_event(self._decode(kv))
                raise RuntimeError("etcd watch stream ended")
            except Exception as exc:  # noqa: BLE001 - keep the watcher alive
                logger.warning(
                    "[ConfigUpdater] watch/connect retry in %.1fs: %s", delay, exc
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, _CONNECT_MAX_DELAY)

    @staticmethod
    def _decode(kv: EtcdKv) -> FetchResult:
        """Parse a kv value. A parse failure yields an empty section."""
        mod_revision = int(kv.mod_revision or 0)
        try:
            document = yaml.safe_load(kv.value.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            logger.error("[ConfigUpdater] failed to parse remote config: %s", exc)
            return FetchResult(section={}, metadata={}, mod_revision=mod_revision)

        # Contract: the data plane picks a section by component name -- gateway
        # reads the outer ``gateway:`` key. The keys *inside* that section
        # mirror config.yaml's top-level keys (``gateway``, ``sandbox``), which
        # is why the managed field paths in merge.py start with those names.
        section, metadata = extract_section(document, "gateway")
        return FetchResult(
            section=section, metadata=metadata, mod_revision=mod_revision
        )


def _pick_exact(kvs: list[EtcdKv], key_bytes: bytes) -> EtcdKv | None:
    matches = (kv for kv in kvs if kv.key == key_bytes)
    return max(matches, key=lambda kv: kv.mod_revision, default=None)


__all__ = ["ConfigUpdaterClient", "FetchResult"]
