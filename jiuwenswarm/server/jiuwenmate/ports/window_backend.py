"""``PersistentPorts.window_backend``: Session state records and window archives.

core owns the content and key layout (``pa:{agent_id}:{session_id}:...``); this
host only stores bytes. Every Execution receives a ``SessionWindowBackend``
bound to its own namespace, Agent and Session, and every row is scoped by those
columns, so a key that merely names another Session never reaches its data.
The rows live in the private JiuwenMate SQLite file, outside any Agent
workspace.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.store.base_kv_store import BasedKVStorePipeline, BaseKVStore

from jiuwenswarm.server.jiuwenmate.execution_models import PersistentContractError
from jiuwenswarm.server.jiuwenmate.models import require_identifier
from jiuwenswarm.server.jiuwenmate.sqlite_store import SQLiteStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS window_entries (
    namespace_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value BLOB NOT NULL,
    is_text INTEGER NOT NULL,
    expires_at REAL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (namespace_id, agent_id, session_id, key)
);
"""

_SCOPE = "namespace_id = ? AND agent_id = ? AND session_id = ?"
# Archive bodies are the only entries retention removes; ``archive_index`` and ``state`` stay.
_ARCHIVE_GLOB = "*:archive:*"

Scope = tuple[str, str, str]


def _encode(value: Any) -> tuple[bytes, int]:
    if isinstance(value, str):
        return value.encode("utf-8"), 1
    if isinstance(value, bytes):
        return value, 0
    raise PersistentContractError("window value must be str or bytes")


def _decode(row: sqlite3.Row) -> str | bytes:
    value = bytes(row["value"])
    return value.decode("utf-8") if row["is_text"] else value


def _live(row: sqlite3.Row | None, now: float) -> bool:
    return row is not None and (row["expires_at"] is None or row["expires_at"] > now)


class WindowBackendStore(SQLiteStore):
    """Owns the ``window_entries`` table; hands out Session-bound backends and runs cleanup."""

    _schema = _SCHEMA

    def __init__(self, db_path: Path, *, clock: Callable[[], float] = time.time) -> None:
        super().__init__(db_path)
        self._clock = clock

    def bind(self, namespace_id: str, agent_id: str, session_id: str, *, generation: str = '') -> SessionWindowBackend:
        for name, value in (("namespace_id", namespace_id), ("agent_id", agent_id), ("session_id", session_id)):
            require_identifier(name, value)
        if generation:
            require_identifier('window generation', generation)
        return SessionWindowBackend(self, (namespace_id, agent_id, session_id), generation=generation)

    def purge_agent(self, namespace_id: str, agent_id: str) -> int:
        """Delete every Session's window data of one colleague."""
        with self._transaction() as conn:
            return conn.execute(
                "DELETE FROM window_entries WHERE namespace_id = ? AND agent_id = ?", (namespace_id, agent_id)
            ).rowcount

    def purge_session(self, namespace_id: str, agent_id: str, session_id: str) -> int:
        with self._transaction() as conn:
            return conn.execute(
                f"DELETE FROM window_entries WHERE {_SCOPE}", (namespace_id, agent_id, session_id)
            ).rowcount

    def purge_expired(self, *, archive_retention: timedelta) -> int:
        """Drop entries past their expiry and archive bodies not rewritten within the retention period."""
        now = self._clock()
        cutoff = now - archive_retention.total_seconds()
        with self._transaction() as conn:
            return conn.execute(
                "DELETE FROM window_entries WHERE (expires_at IS NOT NULL AND expires_at <= ?)"
                " OR (key GLOB ? AND updated_at < ?)",
                (now, _ARCHIVE_GLOB, cutoff),
            ).rowcount

    # The methods below run in a worker thread on behalf of one bound backend.

    def get(self, scope: Scope, keys: Sequence[str]) -> list[str | bytes | None]:
        now = self._clock()
        with self._transaction() as conn:
            return [self._read(conn, scope, key, now) for key in keys]

    @staticmethod
    def _read(conn: sqlite3.Connection, scope: Scope, key: str, now: float) -> str | bytes | None:
        row = conn.execute(f"SELECT * FROM window_entries WHERE {_SCOPE} AND key = ?", (*scope, key)).fetchone()
        return _decode(row) if _live(row, now) else None

    @staticmethod
    def _write(
        conn: sqlite3.Connection, scope: Scope, key: str, value: str | bytes, expires_at: float | None, now: float
    ) -> None:
        data, is_text = _encode(value)
        conn.execute(
            "INSERT INTO window_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (namespace_id, agent_id, session_id, key) DO UPDATE SET"
            " value = excluded.value, is_text = excluded.is_text,"
            " expires_at = excluded.expires_at, updated_at = excluded.updated_at",
            (*scope, key, data, is_text, expires_at, now),
        )

    def set(self, scope: Scope, key: str, value: str | bytes) -> None:
        with self._transaction() as conn:
            self._write(conn, scope, key, value, None, self._clock())

    def exclusive_set(self, scope: Scope, key: str, value: str | bytes, expiry: int | None) -> bool:
        now = self._clock()
        with self._transaction() as conn:
            row = conn.execute(
                f"SELECT expires_at FROM window_entries WHERE {_SCOPE} AND key = ?", (*scope, key)
            ).fetchone()
            if _live(row, now):
                return False
            self._write(conn, scope, key, value, None if expiry is None else now + expiry, now)
            return True

    def delete(self, scope: Scope, keys: Sequence[str]) -> int:
        with self._transaction() as conn:
            return sum(
                conn.execute(f"DELETE FROM window_entries WHERE {_SCOPE} AND key = ?", (*scope, key)).rowcount
                for key in keys
            )

    def get_by_prefix(self, scope: Scope, prefix: str) -> dict[str, str | bytes]:
        now = self._clock()
        with self._transaction() as conn:
            rows = conn.execute(
                f"SELECT * FROM window_entries WHERE {_SCOPE} AND substr(key, 1, ?) = ? ORDER BY key",
                (*scope, len(prefix), prefix),
            ).fetchall()
        return {row["key"]: _decode(row) for row in rows if _live(row, now)}

    def delete_by_prefix(self, scope: Scope, prefix: str) -> int:
        with self._transaction() as conn:
            return conn.execute(
                f"DELETE FROM window_entries WHERE {_SCOPE} AND substr(key, 1, ?) = ?", (*scope, len(prefix), prefix)
            ).rowcount

    def execute(self, scope: Scope, operations: Sequence[tuple[Any, ...]]) -> list[Any]:
        now = self._clock()
        results: list[Any] = []
        with self._transaction() as conn:
            for name, key, *args in operations:
                if name == "set":
                    self._write(conn, scope, key, args[0], None, now)
                    results.append(True)
                elif name == "get":
                    results.append(self._read(conn, scope, key, now))
                else:
                    results.append(self._read(conn, scope, key, now) is not None)
        return results


class SessionWindowBackend(BaseKVStore):
    """The ``BaseKVStore`` one Execution sees: only its own Session's keys exist."""

    def __init__(self, store: WindowBackendStore, scope: Scope, *, generation: str = '') -> None:
        self._store = store
        self._scope = scope
        self._key_prefix = f"pa:{scope[1]}:{scope[2]}:"
        self._storage_prefix = self._key_prefix + (f'grant_epoch:{generation}:' if generation else '')

    @property
    def scope(self) -> Scope:
        """(namespace_id, agent_id, session_id) this backend is bound to."""
        return self._scope

    def _key(self, key: Any) -> str:
        if not isinstance(key, str) or not key.startswith(self._key_prefix):
            raise PersistentContractError("window key is outside this Session")
        return self._storage_prefix + key[len(self._key_prefix):]

    def _prefix(self, prefix: str) -> str:
        if not isinstance(prefix, str):
            raise PersistentContractError('window key prefix must be str')
        if self._key_prefix.startswith(prefix):
            return self._storage_prefix
        return self._key(prefix)

    async def set(self, key: str, value: str | bytes) -> None:
        await asyncio.to_thread(self._store.set, self._scope, self._key(key), value)

    async def exclusive_set(self, key: str, value: str | bytes, expiry: int | None = None) -> bool:
        return await asyncio.to_thread(self._store.exclusive_set, self._scope, self._key(key), value, expiry)

    async def get(self, key: str) -> str | bytes | None:
        return (await asyncio.to_thread(self._store.get, self._scope, [self._key(key)]))[0]

    async def exists(self, key: str) -> bool:
        return await self.get(key) is not None

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._store.delete, self._scope, [self._key(key)])

    async def get_by_prefix(self, prefix: str) -> dict[str, str | bytes]:
        if not isinstance(prefix, str):
            raise PersistentContractError("window key prefix must be str")
        rows = await asyncio.to_thread(self._store.get_by_prefix, self._scope, self._prefix(prefix))
        visible = {}
        for key, value in rows.items():
            suffix = key[len(self._storage_prefix):]
            if not suffix.startswith("grant_epoch:"):
                visible[self._key_prefix + suffix] = value
        return visible

    async def delete_by_prefix(self, prefix: str, batch_size: int | None = None) -> None:
        if not isinstance(prefix, str):
            raise PersistentContractError("window key prefix must be str")
        await asyncio.to_thread(self._store.delete_by_prefix, self._scope, self._prefix(prefix))

    async def mget(self, keys: list[str]) -> list[str | bytes | None]:
        return await asyncio.to_thread(self._store.get, self._scope, [self._key(key) for key in keys])

    async def batch_delete(self, keys: list[str], batch_size: int | None = None) -> int:
        return await asyncio.to_thread(self._store.delete, self._scope, [self._key(key) for key in keys])

    def pipeline(self) -> BasedKVStorePipeline:
        """Queued set / get / exists run together in one transaction."""
        async def execute(operations: list[tuple[Any, ...]]) -> list[Any]:
            return await asyncio.to_thread(self._store.execute, self._scope, operations)

        return _Pipeline(self._key, execute)


class _Pipeline(BasedKVStorePipeline):
    def __init__(
        self, storage_key: Callable[[Any], str], execute: Callable[[list[tuple[Any, ...]]], Awaitable[list[Any]]]
    ) -> None:
        self._storage_key = storage_key
        super().__init__(execute)

    async def set(self, key: str, value: str | bytes, ttl: int | None = None) -> None:
        _encode(value)
        await super().set(self._storage_key(key), value, ttl)

    async def get(self, key: str) -> None:
        await super().get(self._storage_key(key))

    async def exists(self, key: str) -> None:
        await super().exists(self._storage_key(key))
