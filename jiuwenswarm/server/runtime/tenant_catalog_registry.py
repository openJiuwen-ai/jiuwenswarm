"""Synced and reloaded agent catalog keyed by request-side ``(service_id, agent_id)``.

Tip bags stay in ``local_env_config``; this module is catalog / warmup / chat-guard source.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from threading import RLock
from typing import Any

logger = logging.getLogger(__name__)


def catalog_cache_key(agent_id: str, service_id: str) -> tuple[str, str]:
    """Stable catalog/pool-style key — not tip bag key.

    Tuple avoids ``f"{agent_id}_{service_id}"`` delimiter collisions.
    """
    return (agent_id, service_id)


@dataclass
class TenantAgentSpec:
    """One catalog agent entry (logical env keys in ``env``)."""

    service_id: str
    agent_id: str
    config: dict[str, Any] = field(default_factory=dict)
    env: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)
    revision: str | None = None
    content_hash: str | None = None

    @property
    def cache_key(self) -> tuple[str, str]:
        return catalog_cache_key(self.agent_id, self.service_id)


# Backward-compatible alias used in earlier plan drafts.
TenantCatalogSpec = TenantAgentSpec


class TenantCatalogRegistry:
    """In-process catalog of synced or explicitly reloaded agents."""

    _instance: TenantCatalogRegistry | None = None

    def __init__(self) -> None:
        self._lock = RLock()
        self._by_ids: dict[tuple[str, str], TenantAgentSpec] = {}

    @classmethod
    def get_instance(cls) -> TenantCatalogRegistry:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    @classmethod
    def reset_for_tests(cls) -> None:
        cls.reset_instance()

    def upsert(self, spec: TenantAgentSpec) -> TenantAgentSpec:
        key = (str(spec.service_id), str(spec.agent_id))
        with self._lock:
            self._by_ids[key] = spec
        return spec

    def remove(self, service_id: str, agent_id: str) -> bool:
        with self._lock:
            return self._by_ids.pop((str(service_id), str(agent_id)), None) is not None

    def clear_service(self, service_id: str) -> int:
        sid = str(service_id)
        with self._lock:
            keys = [k for k in self._by_ids if k[0] == sid]
            for k in keys:
                del self._by_ids[k]
            return len(keys)

    def get(self, service_id: str, agent_id: str) -> TenantAgentSpec | None:
        with self._lock:
            return self._by_ids.get((str(service_id), str(agent_id)))

    def contains(self, service_id: str, agent_id: str) -> bool:
        return self.get(service_id, agent_id) is not None

    def list_ids(self, service_id: str | None = None) -> list[str]:
        """Return agent_id list (optionally filtered by service_id)."""
        with self._lock:
            if service_id is None:
                return [aid for (_, aid) in self._by_ids]
            sid = str(service_id)
            return [aid for (s, aid) in self._by_ids if s == sid]

    def list_pairs(self, service_id: str | None = None) -> list[tuple[str, str]]:
        with self._lock:
            if service_id is None:
                return list(self._by_ids.keys())
            sid = str(service_id)
            return [k for k in self._by_ids if k[0] == sid]

    def snapshot(self) -> dict[tuple[str, str], TenantAgentSpec]:
        with self._lock:
            return dict(self._by_ids)
