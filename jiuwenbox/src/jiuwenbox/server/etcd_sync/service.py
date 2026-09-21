# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Apply the etcd jiuwenbox section via SandboxManager.update_all_policies.

Watch / decode live in ``client.py``. This module owns start/stop, instance-level
revision dedup, and exception isolation so a bad revision cannot kill the
watcher task.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from jiuwenbox.models.sandbox import PolicyMode
from jiuwenbox.server.etcd_sync import (
    CONFIG_SYNC_KEY,
    ETCD_CONFIG_KEY_ENV,
    ETCD_ENDPOINTS_ENV,
)
from jiuwenbox.server.etcd_sync.client import (
    FetchResult,
    PolicySyncClient,
    parse_etcd_endpoints,
)

logger = logging.getLogger(__name__)


class PolicySyncService:
    """Watch the data-plane key and feed fragments into update_all_policies.

    Disabled when no etcd endpoint is configured. Setup/apply failures are
    logged and never raised out of ``start()`` / the watcher.
    """

    def __init__(
        self,
        manager: Any,
        *,
        etcd_endpoints: list[str],
        key: str = CONFIG_SYNC_KEY,
        client: PolicySyncClient | None = None,
    ) -> None:
        self._manager = manager
        self._endpoints = parse_etcd_endpoints(etcd_endpoints)
        self._key = (key or "").strip() or CONFIG_SYNC_KEY
        self._client = client
        self._task: asyncio.Task[None] | None = None
        self._last_applied_mod_revision = 0

    @classmethod
    def from_env(cls, manager: Any) -> PolicySyncService:
        endpoints = parse_etcd_endpoints(os.environ.get(ETCD_ENDPOINTS_ENV, ""))
        raw_key = (os.environ.get(ETCD_CONFIG_KEY_ENV) or "").strip()
        return cls(
            manager,
            etcd_endpoints=endpoints,
            key=raw_key or CONFIG_SYNC_KEY,
        )

    @property
    def enabled(self) -> bool:
        return bool(self._endpoints)

    @property
    def last_applied_mod_revision(self) -> int:
        return self._last_applied_mod_revision

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if not self.enabled:
            logger.info("[PolicySync] disabled: no etcd endpoints configured")
            return
        client = self._client or PolicySyncClient(
            etcd_endpoints=self._endpoints,
            key=self._key,
        )
        self._client = client
        self._task = asyncio.create_task(
            self._watch(client),
            name="jiuwenbox-policy-sync",
        )
        logger.info(
            "[PolicySync] watching key=%s endpoints=%s",
            self._key,
            ",".join(self._endpoints),
        )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._client = None

    async def _watch(self, client: PolicySyncClient) -> None:
        try:
            await client.watch_loop(self.apply)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("[PolicySync] watcher exited unexpectedly")
        finally:
            await client.aclose()
            if self._client is client:
                self._client = None

    async def apply(self, fetched: FetchResult) -> None:
        """Apply one fetch result. Never raises."""
        mod_revision = int(getattr(fetched, "mod_revision", 0) or 0)
        if mod_revision and mod_revision <= self._last_applied_mod_revision:
            logger.debug(
                "[PolicySync] skip revision-unchanged mod_revision=%s",
                mod_revision,
            )
            return

        section = getattr(fetched, "section", None)
        if not isinstance(section, dict) or not section:
            if mod_revision:
                self._last_applied_mod_revision = mod_revision
            logger.info(
                "[PolicySync] skip empty jiuwenbox section mod_revision=%s",
                mod_revision,
            )
            return

        try:
            result = await self._manager.update_all_policies(
                policy_data=section,
                policy_mode=PolicyMode.OVERRIDE,
                update_default_policy=True,
                update_existing_sandboxes=True,
            )
        except Exception:  # noqa: BLE001 - isolate one bad revision
            logger.exception(
                "[PolicySync] apply failed mod_revision=%s; not advancing",
                mod_revision,
            )
            return

        if mod_revision:
            self._last_applied_mod_revision = mod_revision

        skipped = (result or {}).get("skipped") or []
        failed = (result or {}).get("failed") or []
        updated = (result or {}).get("updated") or []
        if failed:
            logger.warning(
                "[PolicySync] applied mod_revision=%s default_updated=%s "
                "updated=%d skipped=%d failed=%d failed_items=%s",
                mod_revision,
                (result or {}).get("default_updated"),
                len(updated),
                len(skipped),
                len(failed),
                failed,
            )
        else:
            logger.info(
                "[PolicySync] applied mod_revision=%s default_updated=%s "
                "updated=%d skipped=%d",
                mod_revision,
                (result or {}).get("default_updated"),
                len(updated),
                len(skipped),
            )
        if skipped:
            logger.info("[PolicySync] skipped sandboxes: %s", skipped)


__all__ = ["PolicySyncService"]
