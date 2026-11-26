# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Track initialized memory providers without taking ownership of chat providers."""

from __future__ import annotations

import asyncio
import threading
import weakref
from dataclasses import dataclass
from itertools import count
from typing import Any


@dataclass(frozen=True)
class ProviderRecord:
    provider: weakref.ReferenceType
    configuration: str
    agent: str
    sequence: int
    loop: asyncio.AbstractEventLoop

    def live_provider(self) -> Any:
        provider = self.provider()
        if provider is None or not provider.is_initialized or self.loop.is_closed():
            return None
        return provider

    async def _open_on_owner_loop(self) -> str | None:
        provider = self.live_provider()
        if provider is None:
            return None
        return await provider.open_portal()

    async def open_portal(self) -> str | None:
        if self.loop is asyncio.get_running_loop():
            return await self._open_on_owner_loop()
        if not self.loop.is_running():
            raise RuntimeError("memory provider event loop is unavailable")
        future = asyncio.run_coroutine_threadsafe(self._open_on_owner_loop(), self.loop)
        return await asyncio.wrap_future(future)


class ProviderRegistry:
    """Chat and server loops may register providers from different threads."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self._sequence = count()

    def register(self, provider: Any, configuration: str, agent: str) -> ProviderRecord:
        with self._lock:
            record = self._records.get(provider)
            if record is None:
                record = ProviderRecord(
                    weakref.ref(provider), configuration, agent, next(self._sequence),
                    asyncio.get_running_loop(),
                )
                self._records[provider] = record
            return record

    def unregister(self, provider: Any) -> None:
        with self._lock:
            self._records.pop(provider, None)

    def contains(self, record: ProviderRecord) -> bool:
        with self._lock:
            provider = record.live_provider()
            return provider is not None and self._records.get(provider) is record

    def select(self, configuration: str, default_agent: str) -> ProviderRecord | None:
        with self._lock:
            candidates = []
            for record in self._records.values():
                if (
                    record.configuration == configuration
                    and record.agent == default_agent
                    and record.live_provider() is not None
                ):
                    candidates.append(record)
            return min(
                candidates,
                key=lambda record: (record.agent != default_agent, record.sequence),
                default=None,
            )


provider_registry = ProviderRegistry()
