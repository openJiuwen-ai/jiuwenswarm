# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""In-process key/value state reserved for connection-scoped Gateway data.

Entries are scoped to one browser connection, one chat session or the whole
process. Authentication tokens are not stored here: the browser owns them in
``localStorage`` and sends them back through ``request_context.headers``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

SCOPE_CONNECTION = "connection"
SCOPE_SESSION = "session"
SCOPE_GLOBAL = "global"

SCOPES = frozenset({SCOPE_CONNECTION, SCOPE_SESSION, SCOPE_GLOBAL})

#: Rendered instead of a sensitive value in logs and error messages.
MASK = "***"


@dataclass(frozen=True, slots=True)
class StoreEntry:
    """One stored value with its revision and expiry."""

    value: Any
    updated_at: int
    ttl_s: int | None = None
    version: int = 0

    def expired(self, now: int) -> bool:
        return self.ttl_s is not None and now >= self.updated_at + self.ttl_s


class UiStateStore:
    """Scope-aware KV with TTL and per-write versions."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._buckets: dict[tuple[str, str], dict[str, StoreEntry]] = {}
        self._version = 0

    def set(
        self,
        *,
        scope: str,
        scope_id: str,
        key: str,
        value: Any,
        ttl_s: int | None = None,
    ) -> StoreEntry:
        """Write or revoke one key and return the version written.

        ``value=None`` removes the stored value; the returned entry still
        carries the new version so the browser can order the revocation.
        """

        now = int(self._clock())
        self._version += 1
        bucket_key = (scope, scope_id)
        if value is None:
            bucket = self._buckets.get(bucket_key)
            if bucket is not None:
                bucket.pop(key, None)
                if not bucket:
                    self._buckets.pop(bucket_key, None)
            return StoreEntry(value=None, updated_at=now, ttl_s=None, version=self._version)
        entry = StoreEntry(value=value, updated_at=now, ttl_s=ttl_s, version=self._version)
        self._buckets.setdefault(bucket_key, {})[key] = entry
        return entry

    def get(self, *, scope: str, scope_id: str, key: str) -> StoreEntry | None:
        """Return one live entry, dropping it when its TTL has elapsed."""

        bucket = self._buckets.get((scope, scope_id))
        if not bucket:
            return None
        entry = bucket.get(key)
        if entry is None:
            return None
        if entry.expired(int(self._clock())):
            bucket.pop(key, None)
            if not bucket:
                self._buckets.pop((scope, scope_id), None)
            return None
        return entry

    def snapshot(self, *, scope: str, scope_id: str) -> Mapping[str, StoreEntry]:
        """Return a copy of one live bucket."""

        self.purge()
        bucket = self._buckets.get((scope, scope_id), {})
        return MappingProxyType(dict(bucket))

    def purge(self) -> int:
        """Drop expired entries and return how many were removed."""

        now = int(self._clock())
        removed = 0
        for bucket_key, bucket in list(self._buckets.items()):
            for key, entry in list(bucket.items()):
                if entry.expired(now):
                    bucket.pop(key, None)
                    removed += 1
            if not bucket:
                self._buckets.pop(bucket_key, None)
        return removed

    def drop_connection(self, connection_id: str) -> None:
        """Drop every connection-scoped value of one browser connection."""

        if connection_id:
            self._buckets.pop((SCOPE_CONNECTION, connection_id), None)

    def drop_session(self, session_id: str) -> None:
        """Drop every session-scoped value of one chat."""

        if session_id:
            self._buckets.pop((SCOPE_SESSION, session_id), None)

    def clear(self) -> None:
        """Drop every scope; used on shutdown."""

        self._buckets.clear()
