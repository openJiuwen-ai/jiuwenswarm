"""Per-entity asyncio locks used by the A2A outbound repositories."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass
class _LockEntry:
    lock: asyncio.Lock
    users: int = 0


class KeyedLockPool:
    """Create one lock per active key and evict it after the last user exits."""

    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._entries: dict[str, _LockEntry] = {}

    @property
    def size(self) -> int:
        return len(self._entries)

    async def _acquire_entry(self, key: str) -> tuple[str, _LockEntry]:
        normalized = str(key or "").strip()
        if not normalized:
            raise ValueError("lock key cannot be empty")
        async with self._guard:
            entry = self._entries.get(normalized)
            if entry is None:
                entry = _LockEntry(lock=asyncio.Lock())
                self._entries[normalized] = entry
            entry.users += 1
            return normalized, entry

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        normalized, entry = await self._acquire_entry(key)
        try:
            async with entry.lock:
                yield
        finally:
            async with self._guard:
                entry.users -= 1
                if entry.users == 0 and not entry.lock.locked():
                    self._entries.pop(normalized, None)


__all__ = ["KeyedLockPool"]
