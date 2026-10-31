# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``TeamRouteBinding`` persistence for the Gateway.

A binding records which AgentServer currently hosts a team runtime::

    team_session_id  -> endpoint (http base) / agent_service_id (pod id)
    team_id          -> same row, filled in once ``team.runtime_ready`` reports it
    owner identity   -> (group_id, bot_id, user_id) used to derive the tenant ids
                        for requests from *other* members

Phase 1 keeps agent-runtime's ``session_id`` affinity as the source of truth;
the binding is a fast path + explicit ``team_id`` mapping + owner carrier. When
the endpoint fails the caller falls back to ``route()`` and refreshes the row
(``route_version`` increments). Rows are keyed by ``team_session_id`` because a
binding exists before the team id is known (first ``chat.send`` of a session).

Storage: SQLite file (default ``<workspace>/gateway/team_route_bindings.db``),
behind the ``TeamRouteBindingStore`` protocol.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from jiuwenswarm.common.local_env_config import read_env
from jiuwenswarm.common.utils import get_user_workspace_dir

ROUTE_STATUS_CREATING = "creating"
ROUTE_STATUS_ACTIVE = "active"
ROUTE_STATUS_UNHEALTHY = "unhealthy"
ROUTE_STATUS_INACTIVE = "inactive"


@dataclass(frozen=True)
class TeamRouteBinding:
    team_session_id: str
    endpoint: str
    agent_service_id: str = ""
    route_request_id: str = ""
    team_id: str = ""
    owner_user_id: str = ""
    owner_group_id: str = ""
    owner_bot_id: str = ""
    gateway_id: str = ""
    status: str = ROUTE_STATUS_ACTIVE
    route_version: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0
    last_heartbeat_at: float = 0.0

    @property
    def has_owner(self) -> bool:
        return bool(self.owner_user_id and self.owner_group_id and self.owner_bot_id)

    def owner_triple(self) -> tuple[str, str, str]:
        return self.owner_group_id, self.owner_bot_id, self.owner_user_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "team_session_id": self.team_session_id,
            "team_id": self.team_id,
            "endpoint": self.endpoint,
            "agent_service_id": self.agent_service_id,
            "route_request_id": self.route_request_id,
            "owner_user_id": self.owner_user_id,
            "owner_group_id": self.owner_group_id,
            "owner_bot_id": self.owner_bot_id,
            "gateway_id": self.gateway_id,
            "status": self.status,
            "route_version": self.route_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_heartbeat_at": self.last_heartbeat_at,
        }


class TeamRouteBindingStore(Protocol):
    def get_by_session(self, team_session_id: str) -> TeamRouteBinding | None: ...

    def get_by_team_id(self, team_id: str) -> TeamRouteBinding | None: ...

    def upsert(
        self,
        *,
        team_session_id: str,
        endpoint: str,
        agent_service_id: str = "",
        route_request_id: str = "",
        team_id: str | None = None,
        owner: tuple[str, str, str] | None = None,
        gateway_id: str | None = None,
        status: str = ROUTE_STATUS_ACTIVE,
    ) -> TeamRouteBinding: ...

    def touch(self, team_session_id: str) -> None: ...

    def mark_status(self, team_session_id: str, status: str) -> TeamRouteBinding | None: ...

    def delete(self, team_session_id: str) -> bool: ...

    def list_all(self) -> list[TeamRouteBinding]: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS team_route_binding (
    team_session_id   TEXT PRIMARY KEY,
    team_id           TEXT NOT NULL DEFAULT '',
    endpoint          TEXT NOT NULL,
    agent_service_id  TEXT NOT NULL DEFAULT '',
    route_request_id  TEXT NOT NULL DEFAULT '',
    owner_user_id     TEXT NOT NULL DEFAULT '',
    owner_group_id    TEXT NOT NULL DEFAULT '',
    owner_bot_id      TEXT NOT NULL DEFAULT '',
    gateway_id        TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL,
    route_version     INTEGER NOT NULL DEFAULT 1,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    last_heartbeat_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_team_route_team_id ON team_route_binding (team_id);
"""


def _row(row: sqlite3.Row) -> TeamRouteBinding:
    return TeamRouteBinding(
        team_session_id=row["team_session_id"],
        team_id=row["team_id"],
        endpoint=row["endpoint"],
        agent_service_id=row["agent_service_id"],
        route_request_id=row["route_request_id"],
        owner_user_id=row["owner_user_id"],
        owner_group_id=row["owner_group_id"],
        owner_bot_id=row["owner_bot_id"],
        gateway_id=row["gateway_id"],
        status=row["status"],
        route_version=int(row["route_version"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        last_heartbeat_at=float(row["last_heartbeat_at"]),
    )


def _clean(value: Any) -> str:
    return str(value or "").strip()


class SqliteTeamRouteBindingStore:
    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            explicit = read_env("GATEWAY_TEAM_ROUTE_DB", "").strip()
            path = Path(explicit) if explicit else (
                get_user_workspace_dir() / "gateway" / "team_route_bindings.db"
            )
        self._path = path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._path), check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(_SCHEMA)
            self._conn = conn
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def get_by_session(self, team_session_id: str) -> TeamRouteBinding | None:
        sid = _clean(team_session_id)
        if not sid:
            return None
        with self._lock:
            row = self._connect().execute(
                "SELECT * FROM team_route_binding WHERE team_session_id = ?", (sid,)
            ).fetchone()
        return _row(row) if row else None

    def get_by_team_id(self, team_id: str) -> TeamRouteBinding | None:
        tid = _clean(team_id)
        if not tid:
            return None
        with self._lock:
            row = self._connect().execute(
                "SELECT * FROM team_route_binding WHERE team_id = ? ORDER BY updated_at DESC LIMIT 1",
                (tid,),
            ).fetchone()
        return _row(row) if row else None

    def upsert(
        self,
        *,
        team_session_id: str,
        endpoint: str,
        agent_service_id: str = "",
        route_request_id: str = "",
        team_id: str | None = None,
        owner: tuple[str, str, str] | None = None,
        gateway_id: str | None = None,
        status: str = ROUTE_STATUS_ACTIVE,
    ) -> TeamRouteBinding:
        sid = _clean(team_session_id)
        ep = _clean(endpoint)
        if not sid or not ep:
            raise ValueError("team_session_id and endpoint are required")
        now = time.time()
        with self._lock:
            conn = self._connect()
            existing_row = conn.execute(
                "SELECT * FROM team_route_binding WHERE team_session_id = ?", (sid,)
            ).fetchone()
            if existing_row is None:
                binding = TeamRouteBinding(
                    team_session_id=sid,
                    endpoint=ep,
                    agent_service_id=_clean(agent_service_id),
                    route_request_id=_clean(route_request_id),
                    team_id=_clean(team_id),
                    owner_group_id=_clean(owner[0]) if owner else "",
                    owner_bot_id=_clean(owner[1]) if owner else "",
                    owner_user_id=_clean(owner[2]) if owner else "",
                    gateway_id=_clean(gateway_id),
                    status=status,
                    route_version=1,
                    created_at=now,
                    updated_at=now,
                    last_heartbeat_at=now,
                )
                conn.execute(
                    "INSERT INTO team_route_binding (team_session_id, team_id, endpoint, "
                    "agent_service_id, route_request_id, owner_user_id, owner_group_id, "
                    "owner_bot_id, gateway_id, status, route_version, created_at, updated_at, "
                    "last_heartbeat_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        binding.team_session_id, binding.team_id, binding.endpoint,
                        binding.agent_service_id, binding.route_request_id,
                        binding.owner_user_id, binding.owner_group_id, binding.owner_bot_id,
                        binding.gateway_id, binding.status, binding.route_version,
                        binding.created_at, binding.updated_at, binding.last_heartbeat_at,
                    ),
                )
                return binding

            existing = _row(existing_row)
            endpoint_changed = existing.endpoint != ep or (
                _clean(agent_service_id) and existing.agent_service_id != _clean(agent_service_id)
            )
            # Owner is write-once: the first identity that bound the session owns
            # the team; later members routing through must never overwrite it.
            set_owner = owner is not None and _clean(owner[2]) and not existing.has_owner
            binding = replace(
                existing,
                endpoint=ep,
                agent_service_id=_clean(agent_service_id) or existing.agent_service_id,
                route_request_id=_clean(route_request_id) or existing.route_request_id,
                team_id=_clean(team_id) if team_id is not None and _clean(team_id) else existing.team_id,
                owner_group_id=_clean(owner[0]) if set_owner else existing.owner_group_id,
                owner_bot_id=_clean(owner[1]) if set_owner else existing.owner_bot_id,
                owner_user_id=_clean(owner[2]) if set_owner else existing.owner_user_id,
                gateway_id=_clean(gateway_id) if gateway_id is not None else existing.gateway_id,
                status=status,
                route_version=existing.route_version + (1 if endpoint_changed else 0),
                updated_at=now,
                last_heartbeat_at=now,
            )
            conn.execute(
                "UPDATE team_route_binding SET team_id=?, endpoint=?, agent_service_id=?, "
                "route_request_id=?, owner_user_id=?, owner_group_id=?, owner_bot_id=?, "
                "gateway_id=?, status=?, route_version=?, updated_at=?, last_heartbeat_at=? "
                "WHERE team_session_id=?",
                (
                    binding.team_id, binding.endpoint, binding.agent_service_id,
                    binding.route_request_id, binding.owner_user_id, binding.owner_group_id,
                    binding.owner_bot_id, binding.gateway_id, binding.status,
                    binding.route_version, binding.updated_at, binding.last_heartbeat_at,
                    sid,
                ),
            )
            return binding

    def touch(self, team_session_id: str) -> None:
        sid = _clean(team_session_id)
        if not sid:
            return
        with self._lock:
            self._connect().execute(
                "UPDATE team_route_binding SET last_heartbeat_at = ? WHERE team_session_id = ?",
                (time.time(), sid),
            )

    def mark_status(self, team_session_id: str, status: str) -> TeamRouteBinding | None:
        sid = _clean(team_session_id)
        if not sid:
            return None
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                "UPDATE team_route_binding SET status = ?, updated_at = ? WHERE team_session_id = ?",
                (status, time.time(), sid),
            )
            if cur.rowcount == 0:
                return None
        return self.get_by_session(sid)

    def delete(self, team_session_id: str) -> bool:
        sid = _clean(team_session_id)
        if not sid:
            return False
        with self._lock:
            cur = self._connect().execute(
                "DELETE FROM team_route_binding WHERE team_session_id = ?", (sid,)
            )
            return cur.rowcount > 0

    def list_all(self) -> list[TeamRouteBinding]:
        with self._lock:
            rows = self._connect().execute(
                "SELECT * FROM team_route_binding ORDER BY updated_at DESC"
            ).fetchall()
        return [_row(r) for r in rows]


class InMemoryTeamRouteBindingStore(SqliteTeamRouteBindingStore):
    """SQLite ``:memory:`` variant for tests / ephemeral gateways."""

    def __init__(self) -> None:  # noqa: D107
        super().__init__(path=Path(":memory:"))

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            self._conn = conn
        return self._conn


_DEFAULT: TeamRouteBindingStore | None = None
_DEFAULT_LOCK = threading.Lock()


def get_team_route_binding_store() -> TeamRouteBindingStore:
    global _DEFAULT
    if _DEFAULT is None:
        with _DEFAULT_LOCK:
            if _DEFAULT is None:
                from jiuwenswarm.infrastructure.db.team_mysql_actor import use_team_mysql

                if use_team_mysql():
                    from jiuwenswarm.enterprise.team.route_binding_mysql import (
                        MysqlTeamRouteBindingStore,
                    )

                    _DEFAULT = MysqlTeamRouteBindingStore()
                else:
                    _DEFAULT = SqliteTeamRouteBindingStore()
    return _DEFAULT


def set_team_route_binding_store(store: TeamRouteBindingStore | None) -> None:
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = store


__all__ = [
    "InMemoryTeamRouteBindingStore",
    "ROUTE_STATUS_ACTIVE",
    "ROUTE_STATUS_CREATING",
    "ROUTE_STATUS_INACTIVE",
    "ROUTE_STATUS_UNHEALTHY",
    "SqliteTeamRouteBindingStore",
    "TeamRouteBinding",
    "TeamRouteBindingStore",
    "get_team_route_binding_store",
    "set_team_route_binding_store",
]
