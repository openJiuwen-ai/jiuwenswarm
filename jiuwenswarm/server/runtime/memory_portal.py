# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Settings binding for agent-memory Portal; no memory CRUD or model proxy."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from jiuwenswarm.agents.harness.common.memory.jiuwen_rail_builder import (
    build_jiuwen_memory_provider,
    get_jiuwen_builtin_mode,
    is_jiuwen_builtin_memory,
    memory_portal_configuration,
    memory_provider_identity,
)
from jiuwenswarm.agents.harness.common.memory.portal_registry import (
    ProviderRecord,
    ProviderRegistry,
    provider_registry,
)

from jiuwenswarm.common.memory_config import is_memory_portal_enabled

logger = logging.getLogger(__name__)


def portal_enabled(config: dict[str, Any]) -> bool:
    return (
        is_memory_portal_enabled(config)
        and is_jiuwen_builtin_memory(config)
    )


class MemoryPortalService:
    """Stable selection; only providers created here are owned here.

    All operations on this service run on the AgentServer event loop. Borrowed
    providers are called on the loop that successfully initialized them.
    """

    def __init__(
        self, registry: ProviderRegistry = provider_registry,
        factory: Callable[[dict[str, Any], str], Any] | None = None,
    ) -> None:
        self._registry = registry
        self._factory = factory or build_jiuwen_memory_provider
        self._lock = asyncio.Lock()
        self._binding: ProviderRecord | None = None
        self._owned: Any = None
        self._configuration = ""

    async def _release_owned(self) -> None:
        provider = self._owned
        self._binding = None
        if provider is not None:
            await provider.shutdown()
            self._owned = None

    async def configure(self, config: dict[str, Any]) -> None:
        async with self._lock:
            await self._configure(config)

    async def _configure(self, config: dict[str, Any]) -> None:
        configuration = memory_portal_configuration(config)
        if configuration != self._configuration:
            await self._release_owned()
            self._configuration = configuration

    async def close(self) -> None:
        async with self._lock:
            await self._release_owned()

    async def _select(self, config: dict[str, Any]) -> ProviderRecord:
        if self._binding is not None and self._registry.contains(self._binding):
            return self._binding
        await self._release_owned()
        default_agent = memory_provider_identity(config)["agent_id"]
        self._binding = self._registry.select(self._configuration, default_agent)
        if self._binding is not None:
            return self._binding
        provider = self._factory(config, default_agent)
        self._owned = provider
        try:
            await provider.initialize(**memory_provider_identity(config, default_agent))
            if not provider.is_initialized:
                raise RuntimeError("memory initialization did not complete")
            self._binding = self._registry.register(provider, self._configuration, default_agent)
            return self._binding
        except BaseException:
            await self._release_owned()
            raise

    async def open(self, config: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {"mode": get_jiuwen_builtin_mode(config)}
        try:
            async with asyncio.timeout(30):
                async with self._lock:
                    await self._configure(config)
                    if not portal_enabled(config):
                        return {**result, "status": "disabled", "code": "not_enabled"}
                    record = await self._select(config)
                    result["agent"] = record.agent
                    url = await record.open_portal()
                    if not url:
                        return {**result, "status": "unavailable", "code": "portal_unavailable"}
                    parsed = urlsplit(url)
                    has_credentials = bool(parsed.username or parsed.password)
                    if parsed.scheme not in ("http", "https") or not parsed.hostname or has_credentials:
                        raise ValueError("invalid portal URL")
                    return {**result, "status": "ready", "url": url}
        except (ImportError, AttributeError):
            return {**result, "status": "unavailable", "code": "dependency_unavailable"}
        except TimeoutError:
            return {**result, "status": "unavailable", "code": "timeout"}
        except Exception as exc:
            # URLs, launch tokens and model/service keys must not enter logs or errors.
            logger.warning("Memory Portal request failed (%s)", type(exc).__name__)
            return {**result, "status": "unavailable", "code": "portal_unavailable"}


_services: dict[str, MemoryPortalService] = {}


def get_memory_portal_service() -> MemoryPortalService:
    from jiuwenswarm.common.utils import get_user_workspace_dir

    home = str(get_user_workspace_dir())
    if home not in _services:
        _services[home] = MemoryPortalService()
    return _services[home]


async def close_memory_portal_services() -> None:
    services = list(_services.items())
    results = await asyncio.gather(
        *(service.close() for _, service in services), return_exceptions=True,
    )
    for (home, service), result in zip(services, results):
        if isinstance(result, BaseException):
            logger.warning("Memory Portal close failed (%s)", type(result).__name__)
        elif _services.get(home) is service:
            _services.pop(home)
