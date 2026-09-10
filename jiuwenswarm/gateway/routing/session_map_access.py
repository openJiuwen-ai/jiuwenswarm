# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SessionMap Repository process entry + sync SessionStorage adapter."""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING, Any

from jiuwenswarm.edition import is_enterprise
from jiuwenswarm.gateway.routing.session_map_repository import SessionMapRepository
from jiuwenswarm.gateway.routing.session_storage import SessionStorage
from jiuwenswarm.gateway.storage.async_bridge import run_awaitable

if TYPE_CHECKING:
    from jiuwenswarm.gateway.routing.session_map import Session

_repo: SessionMapRepository | None = None


def session_map_read_through_enabled(cfg: dict[str, Any] | None = None) -> bool:
    """Enterprise / multi-replica need read-through; personal single-node keeps local cache."""
    if is_enterprise():
        return True
    try:
        replicas = int(os.getenv("GATEWAY_REPLICAS", "1") or "1")
    except ValueError:
        replicas = 1
    return replicas > 1


def set_session_map_repository(repo: SessionMapRepository | None) -> None:
    global _repo
    _repo = repo


def get_session_map_repository() -> SessionMapRepository | None:
    return _repo


def clear_session_map_repository() -> None:
    set_session_map_repository(None)


class PersistentSessionStorage(SessionStorage):
    """Sync SessionStorage backed by SessionMapRepository (run_awaitable bridge).

    personal 单机（``read_through=False``）：与 ``LocalSessionStorage`` 一样预加载并缓存，
    落盘仍经 Repository → ``.checkpoint/session_map.json``。
    enterprise / 多副本（``read_through=True``）：每次 ``get`` / ``get_all`` 回源，避免 P-04。
    """

    def __init__(
        self,
        repo: SessionMapRepository,
        *,
        read_through: bool = False,
    ) -> None:
        self._repo = repo
        self._read_through = read_through
        self._mapping: dict[str, Session] = {}
        self._lock = threading.Lock()
        self.load()

    def load(self) -> None:
        if self._read_through:
            return
        mapping = run_awaitable(self._repo.list_all())
        with self._lock:
            self._mapping = dict(mapping or {})

    def save(self, session: "Session") -> None:
        return None

    def get(self, identity_key: str) -> "Session | None":
        if not self._read_through:
            with self._lock:
                cached = self._mapping.get(identity_key)
            if cached is not None:
                return cached
        sess = run_awaitable(self._repo.get(identity_key))
        if sess is not None and not self._read_through:
            with self._lock:
                self._mapping[identity_key] = sess
        return sess

    def set(self, identity_key: str, session: "Session") -> None:
        saved = run_awaitable(self._repo.upsert(identity_key, session))
        if not self._read_through:
            with self._lock:
                self._mapping[identity_key] = saved

    def remove(self, identity_key: str) -> None:
        run_awaitable(self._repo.delete(identity_key))
        if not self._read_through:
            with self._lock:
                self._mapping.pop(identity_key, None)

    def get_all(self) -> dict[str, "Session"]:
        if self._read_through:
            mapping = run_awaitable(self._repo.list_all())
            return dict(mapping or {})
        with self._lock:
            return dict(self._mapping)


__all__ = [
    "PersistentSessionStorage",
    "clear_session_map_repository",
    "get_session_map_repository",
    "session_map_read_through_enabled",
    "set_session_map_repository",
]
