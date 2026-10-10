# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Apply AgentOS management-plane config to the running Gateway."""

from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from jiuwenswarm.extensions.agentos.config_updater.client import ConfigUpdaterClient
from jiuwenswarm.extensions.agentos.config_updater.merge import (
    filter_managed_section,
    merge_section,
    normalize_section,
    validate_section,
)

logger = logging.getLogger(__name__)

_APPLY_RETRY_INITIAL_DELAY = 1.0
_APPLY_RETRY_MAX_DELAY = 30.0

RefreshHandler = Callable[[dict[str, Any]], Awaitable[bool | None]]
ConfigProvider = Callable[[], Mapping[str, Any]]


def build_refresh_handler(agent_client: Any) -> Callable[[dict[str, Any]], None]:
    """Wrap ``agent_client.apply_remote_overrides`` into a plain callable."""
    apply_overrides = getattr(agent_client, "apply_remote_overrides", None)
    if not callable(apply_overrides):
        return lambda _merged: None
    return apply_overrides


@dataclass
class ApplyResult:
    """Outcome of one apply attempt."""

    applied: bool = False
    skipped_reason: str = ""
    errors: list[str] = field(default_factory=list)
    retryable: bool = False


class ConfigUpdaterApplier:
    """Merge managed fields into an in-memory full-config snapshot."""

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        config_provider: ConfigProvider | None = None,
        refresh_handler: RefreshHandler | None = None,
    ) -> None:
        self._effective_config = copy.deepcopy(dict(config))
        self._config_provider = config_provider or (lambda: self._effective_config)
        self._managed_overrides: dict[str, Any] = {}
        self._refresh_handler = refresh_handler
        self._last_applied_mod_revision = 0
        self._pending_refresh_mod_revision = 0
        self._pending_config: dict[str, Any] | None = None
        self._pending_overrides: dict[str, Any] | None = None

    @property
    def last_applied_mod_revision(self) -> int:
        return self._last_applied_mod_revision

    async def apply(self, fetched: Any) -> ApplyResult:
        """Validate, merge, and refresh one etcd revision without disk I/O."""
        section = getattr(fetched, "section", None)
        mod_revision = int(getattr(fetched, "mod_revision", 0) or 0)

        if mod_revision and mod_revision <= self._last_applied_mod_revision:
            return ApplyResult(skipped_reason="revision-unchanged")
        if (
            mod_revision
            and self._pending_refresh_mod_revision
            and mod_revision < self._pending_refresh_mod_revision
        ):
            return ApplyResult(skipped_reason="revision-unchanged")
        if not section:
            self._last_applied_mod_revision = mod_revision
            return ApplyResult(skipped_reason="empty-section")

        section, ignored_paths = filter_managed_section(section)
        if ignored_paths:
            logger.warning(
                "[ConfigUpdater] ignored unmanaged fields mod_revision=%s: %s",
                mod_revision,
                ", ".join(ignored_paths),
            )
        if not section:
            self._last_applied_mod_revision = mod_revision
            return ApplyResult(skipped_reason="no-managed-fields")

        errors = validate_section(section)
        if errors:
            logger.error(
                "[ConfigUpdater] rejected mod_revision=%s: %s",
                mod_revision,
                "; ".join(errors),
            )
            self._last_applied_mod_revision = mod_revision
            return ApplyResult(skipped_reason="validation-failed", errors=errors)

        if (
            mod_revision == self._pending_refresh_mod_revision
            and self._pending_config is not None
        ):
            merged = self._pending_config
            managed_overrides = self._pending_overrides or {}
        else:
            section = normalize_section(section)
            managed_overrides = copy.deepcopy(self._managed_overrides)
            merge_section(managed_overrides, section)
            try:
                merged = copy.deepcopy(dict(self._config_provider()))
            except Exception as exc:  # noqa: BLE001 - retry current revision
                logger.warning("[ConfigUpdater] failed to read base config: %s", exc)
                return ApplyResult(
                    skipped_reason="config-read-failed",
                    errors=[str(exc)],
                    retryable=True,
                )
            merge_section(merged, managed_overrides)
            if merged == self._effective_config:
                self._last_applied_mod_revision = mod_revision
                return ApplyResult(skipped_reason="no-change")
            self._pending_refresh_mod_revision = mod_revision
            self._pending_config = merged
            self._pending_overrides = managed_overrides

        return await self._refresh(
            merged=merged,
            managed_overrides=managed_overrides,
            mod_revision=mod_revision,
        )

    async def _refresh(
        self,
        *,
        merged: dict[str, Any],
        managed_overrides: dict[str, Any],
        mod_revision: int,
    ) -> ApplyResult:
        if self._refresh_handler is not None:
            try:
                refreshed = await self._refresh_handler(copy.deepcopy(merged))
                if refreshed is False:
                    raise RuntimeError("refresh handler reported failure")
            except Exception as exc:  # noqa: BLE001 - revision remains pending
                logger.exception("[ConfigUpdater] refresh handler failed")
                return ApplyResult(
                    skipped_reason="refresh-failed",
                    errors=[str(exc)],
                    retryable=True,
                )

        self._effective_config = merged
        self._managed_overrides = managed_overrides
        self._pending_config = None
        self._pending_overrides = None
        self._pending_refresh_mod_revision = 0
        self._last_applied_mod_revision = mod_revision
        return ApplyResult(applied=True)


class ConfigUpdaterService:
    """Watch the data-plane key and apply it to the running Gateway."""

    def __init__(
        self,
        *,
        etcd_endpoints: list[str],
        config: Mapping[str, Any],
        config_provider: ConfigProvider | None = None,
        refresh_handler: RefreshHandler | None = None,
        client: ConfigUpdaterClient | None = None,
    ) -> None:
        self._endpoints = [
            str(item).strip() for item in etcd_endpoints if str(item).strip()
        ]
        self._config = copy.deepcopy(dict(config))
        self._config_provider = config_provider
        self._refresh_handler = refresh_handler
        self._client = client
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._endpoints)

    async def start(self) -> None:
        if self._task is not None:
            return
        if not self._endpoints:
            logger.info("[ConfigUpdater] disabled: no etcd endpoints configured")
            return

        client = self._client or ConfigUpdaterClient(etcd_endpoints=self._endpoints)
        applier = ConfigUpdaterApplier(
            self._config,
            config_provider=self._config_provider,
            refresh_handler=self._refresh_handler,
        )
        self._task = asyncio.create_task(
            self._watch(client, applier),
            name="config-updater-watcher",
        )
        logger.info("[ConfigUpdater] watcher started endpoints=%s", self._endpoints)

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _watch(
        self,
        client: ConfigUpdaterClient,
        applier: ConfigUpdaterApplier,
    ) -> None:
        async def _apply(fetched: Any) -> None:
            delay = _APPLY_RETRY_INITIAL_DELAY
            while True:
                result = await applier.apply(fetched)
                if not result.retryable:
                    return
                logger.warning(
                    "[ConfigUpdater] apply retry in %.1fs mod_revision=%s: %s",
                    delay,
                    int(getattr(fetched, "mod_revision", 0) or 0),
                    "; ".join(result.errors) or result.skipped_reason,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, _APPLY_RETRY_MAX_DELAY)

        try:
            await client.watch_loop(_apply)
        except Exception as exc:  # noqa: BLE001 - never take the gateway down
            logger.warning("[ConfigUpdater] watcher exited: %s", exc)
        finally:
            await client.aclose()
            if self._task is asyncio.current_task():
                self._task = None


__all__ = [
    "ApplyResult",
    "ConfigProvider",
    "ConfigUpdaterApplier",
    "ConfigUpdaterService",
    "RefreshHandler",
    "build_refresh_handler",
]
