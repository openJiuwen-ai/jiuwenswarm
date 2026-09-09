"""Process-level Celia MCP client sharing."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .client import CeliaMcpClient
from .config import CeliaConfig
from .errors import CeliaConfigError


@dataclass
class CeliaClientLease:
    key: tuple[str, ...]
    client: CeliaMcpClient
    released: bool = False


@dataclass
class _Entry:
    config: CeliaConfig
    client: CeliaMcpClient
    ref_count: int = 0


class CeliaClientManager:
    def __init__(self) -> None:
        self._entries: dict[tuple[str, ...], _Entry] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, config: CeliaConfig) -> CeliaClientLease:
        key = config.fingerprint
        async with self._lock:
            for entry in self._entries.values():
                if entry.config.db_identity == config.db_identity and entry.config.fingerprint != key:
                    raise CeliaConfigError("different Celia process configurations cannot share one DB")
            entry = self._entries.get(key)
            if entry is None:
                client = CeliaMcpClient(config)
                entry = _Entry(config=config, client=client)
                async def _clear_runtime_state() -> None:
                    from .fixed_context import get_fixed_context_cache
                    from .runtime_store import get_runtime_store

                    get_fixed_context_cache().clear()
                    get_runtime_store().clear_all()

                client.add_restart_callback(_clear_runtime_state)
                self._entries[key] = entry
            entry.ref_count += 1

        try:
            await entry.client.start()
        except Exception:
            async with self._lock:
                entry.ref_count -= 1
                if entry.ref_count <= 0:
                    self._entries.pop(key, None)
            raise
        return CeliaClientLease(key=key, client=entry.client)

    async def release(self, lease: CeliaClientLease) -> None:
        if lease.released:
            return
        lease.released = True
        async with self._lock:
            current = self._entries.get(lease.key)
            if current is None:
                return
            # Agent/session teardown must not kill the process: Celia owns the
            # overnight dream schedule. AgentServer shutdown calls close_all.
            current.ref_count = max(0, current.ref_count - 1)

    async def close_all(self) -> None:
        async with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        await asyncio.gather(*(entry.client.close() for entry in entries), return_exceptions=True)


_MANAGER = CeliaClientManager()


def get_celia_client_manager() -> CeliaClientManager:
    return _MANAGER
