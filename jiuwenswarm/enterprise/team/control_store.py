# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Enterprise team control records: ``TeamEntity`` + ``TeamMembership``.

Phase-1 of the enterprise Team plan. The existing ``TeamBindingStore``
(``bindings.json``) and ``TeamEntityStore`` (``.team-meta/team.yaml``) stay
untouched and keep ``team_name`` as the user-facing key; this module adds the
enterprise fields on top of them (stable ``team_id``, owner identity, lifecycle
``status``, runtime binding, human memberships).

Storage is a single SQLite file under the tenant workspace. The ``TeamControlStore``
protocol is the pluggable boundary; ``SqliteTeamControlStore`` is the default
implementation. All methods are synchronous and guarded by a process lock; async
callers wrap them in ``asyncio.to_thread`` when latency matters.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Protocol

from jiuwenswarm.common.utils import get_user_workspace_dir

# ---------------------------------------------------------------------------
# Status vocab (kept as plain strings so wire payloads need no mapping)
# ---------------------------------------------------------------------------

TEAM_STATUS_CREATING = "creating"
TEAM_STATUS_RUNNING = "running"
TEAM_STATUS_PAUSED = "paused"
TEAM_STATUS_STOPPING = "stopping"
TEAM_STATUS_STOPPED = "stopped"
TEAM_STATUS_DELETED = "deleted"

TEAM_STATUSES: frozenset[str] = frozenset(
    {
        TEAM_STATUS_CREATING,
        TEAM_STATUS_RUNNING,
        TEAM_STATUS_PAUSED,
        TEAM_STATUS_STOPPING,
        TEAM_STATUS_STOPPED,
        TEAM_STATUS_DELETED,
    }
)
#: Statuses that count towards team quota (reserved for the quota manager).
TEAM_ACTIVE_STATUSES: frozenset[str] = frozenset(
    {TEAM_STATUS_CREATING, TEAM_STATUS_RUNNING, TEAM_STATUS_PAUSED, TEAM_STATUS_STOPPING}
)

MEMBERSHIP_STATUS_PENDING = "pending"
MEMBERSHIP_STATUS_ACTIVE = "active"
MEMBERSHIP_STATUS_LEFT = "left"
MEMBERSHIP_STATUS_REVOKED = "revoked"
MEMBERSHIP_STATUS_EXPIRED = "expired"

MEMBERSHIP_STATUSES: frozenset[str] = frozenset(
    {
        MEMBERSHIP_STATUS_PENDING,
        MEMBERSHIP_STATUS_ACTIVE,
        MEMBERSHIP_STATUS_LEFT,
        MEMBERSHIP_STATUS_REVOKED,
        MEMBERSHIP_STATUS_EXPIRED,
    }
)

MEMBER_TYPE_HUMAN = "human"

#: Default permission set for phase-1 human members.
DEFAULT_HUMAN_PERMISSIONS: tuple[str, ...] = (
    "team:view",
    "team:send",
    "team:task:read",
    "team:task:claim",
    "team:member:leave",
)

RUNTIME_MODE_LOCAL = "local"


class TeamControlStoreError(ValueError):
    """Validation or persistence error for team control records."""

    def __init__(self, message: str, *, code: str = "BAD_REQUEST") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OwnerIdentity:
    """Routing identity of the user who created the team.

    ``group_id`` / ``bot_id`` are the enterprise routing scope; together with
    ``user_id`` they are what the gateway derives ``service_id`` / ``agent_id`` /
    ``workspace_key`` from. Personal edition leaves group/bot empty.
    """

    user_id: str = ""
    group_id: str = ""
    bot_id: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"user_id": self.user_id, "group_id": self.group_id, "bot_id": self.bot_id}

    def same_scope(self, other: "OwnerIdentity") -> bool:
        """Phase-1 "same tenant" check: same routing scope (group + bot)."""
        return self.group_id == other.group_id and self.bot_id == other.bot_id


@dataclass(frozen=True)
class TeamEntityRecord:
    team_id: str
    team_name: str
    owner: OwnerIdentity
    status: str = TEAM_STATUS_CREATING
    runtime_mode: str = RUNTIME_MODE_LOCAL
    template_id: str = ""
    team_session_id: str = ""
    runtime_team_name: str = ""
    agent_service_id: str = ""
    gateway_id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "team_id": self.team_id,
            "team_name": self.team_name,
            "owner_user_id": self.owner.user_id,
            "owner_group_id": self.owner.group_id,
            "owner_bot_id": self.owner.bot_id,
            "status": self.status,
            "runtime_mode": self.runtime_mode,
            "template_id": self.template_id,
            "team_session_id": self.team_session_id,
            "runtime_team_name": self.runtime_team_name,
            "agent_service_id": self.agent_service_id,
            "gateway_id": self.gateway_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "extra": dict(self.extra),
        }


@dataclass(frozen=True)
class TeamMembershipRecord:
    membership_id: str
    team_id: str
    identity: OwnerIdentity
    member_name: str
    member_type: str = MEMBER_TYPE_HUMAN
    display_name: str = ""
    permissions: tuple[str, ...] = DEFAULT_HUMAN_PERMISSIONS
    status: str = MEMBERSHIP_STATUS_PENDING
    expires_at: float | None = None
    joined_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "membership_id": self.membership_id,
            "team_id": self.team_id,
            "user_id": self.identity.user_id,
            "group_id": self.identity.group_id,
            "bot_id": self.identity.bot_id,
            "member_name": self.member_name,
            "member_type": self.member_type,
            "display_name": self.display_name,
            "permissions": list(self.permissions),
            "status": self.status,
            "expires_at": self.expires_at,
            "joined_at": self.joined_at,
            "updated_at": self.updated_at,
        }

    @property
    def is_active(self) -> bool:
        if self.status != MEMBERSHIP_STATUS_ACTIVE:
            return False
        if self.expires_at is not None and self.expires_at <= time.time():
            return False
        return True


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class TeamControlStore(Protocol):
    """Pluggable persistence boundary for team control records."""

    # -- entities --
    def create_entity(
        self,
        *,
        team_name: str,
        owner: OwnerIdentity,
        template_id: str = "",
        team_session_id: str = "",
        runtime_team_name: str = "",
        status: str = TEAM_STATUS_CREATING,
        team_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> TeamEntityRecord: ...

    def get_entity(self, team_id: str) -> TeamEntityRecord | None: ...

    def get_entity_by_name(self, team_name: str) -> TeamEntityRecord | None: ...

    def get_entity_by_session(self, session_id: str) -> TeamEntityRecord | None: ...

    def list_entities(
        self,
        *,
        owner: OwnerIdentity | None = None,
        include_deleted: bool = False,
    ) -> list[TeamEntityRecord]: ...

    def update_entity(self, team_id: str, **fields: Any) -> TeamEntityRecord | None: ...

    def set_status(self, team_id: str, status: str) -> TeamEntityRecord | None: ...

    def set_status_by_name(self, team_name: str, status: str) -> TeamEntityRecord | None: ...

    def delete_entity(self, team_id: str) -> bool: ...

    # -- memberships --
    def upsert_membership(
        self,
        *,
        team_id: str,
        identity: OwnerIdentity,
        member_name: str,
        display_name: str = "",
        member_type: str = MEMBER_TYPE_HUMAN,
        permissions: Iterable[str] | None = None,
        status: str = MEMBERSHIP_STATUS_PENDING,
        expires_at: float | None = None,
    ) -> TeamMembershipRecord: ...

    def get_membership(self, team_id: str, user_id: str) -> TeamMembershipRecord | None: ...

    def get_membership_by_member_name(
        self, team_id: str, member_name: str
    ) -> TeamMembershipRecord | None: ...

    def list_memberships(
        self, team_id: str, *, statuses: Iterable[str] | None = None
    ) -> list[TeamMembershipRecord]: ...

    def set_membership_status(
        self, membership_id: str, status: str
    ) -> TeamMembershipRecord | None: ...


# ---------------------------------------------------------------------------
# SQLite implementation
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS team_entity (
    team_id            TEXT PRIMARY KEY,
    team_name          TEXT NOT NULL UNIQUE,
    owner_user_id      TEXT NOT NULL DEFAULT '',
    owner_group_id     TEXT NOT NULL DEFAULT '',
    owner_bot_id       TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL,
    runtime_mode       TEXT NOT NULL DEFAULT 'local',
    template_id        TEXT NOT NULL DEFAULT '',
    team_session_id    TEXT NOT NULL DEFAULT '',
    runtime_team_name  TEXT NOT NULL DEFAULT '',
    agent_service_id   TEXT NOT NULL DEFAULT '',
    gateway_id         TEXT NOT NULL DEFAULT '',
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL,
    extra              TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_team_entity_owner
    ON team_entity (owner_group_id, owner_bot_id, owner_user_id);
CREATE INDEX IF NOT EXISTS idx_team_entity_session ON team_entity (team_session_id);

CREATE TABLE IF NOT EXISTS team_membership (
    membership_id  TEXT PRIMARY KEY,
    team_id        TEXT NOT NULL,
    user_id        TEXT NOT NULL,
    group_id       TEXT NOT NULL DEFAULT '',
    bot_id         TEXT NOT NULL DEFAULT '',
    member_name    TEXT NOT NULL,
    member_type    TEXT NOT NULL DEFAULT 'human',
    display_name   TEXT NOT NULL DEFAULT '',
    permissions    TEXT NOT NULL DEFAULT '[]',
    status         TEXT NOT NULL,
    expires_at     REAL,
    joined_at      REAL NOT NULL,
    updated_at     REAL NOT NULL,
    UNIQUE (team_id, user_id),
    UNIQUE (team_id, member_name)
);
CREATE INDEX IF NOT EXISTS idx_team_membership_team ON team_membership (team_id, status);
"""

_ENTITY_COLUMNS = (
    "team_id",
    "team_name",
    "owner_user_id",
    "owner_group_id",
    "owner_bot_id",
    "status",
    "runtime_mode",
    "template_id",
    "team_session_id",
    "runtime_team_name",
    "agent_service_id",
    "gateway_id",
    "created_at",
    "updated_at",
    "extra",
)
_ENTITY_UPDATABLE = frozenset(
    {
        "team_name",
        "status",
        "runtime_mode",
        "template_id",
        "team_session_id",
        "runtime_team_name",
        "agent_service_id",
        "gateway_id",
        "extra",
    }
)
_MEMBERSHIP_COLUMNS = (
    "membership_id",
    "team_id",
    "user_id",
    "group_id",
    "bot_id",
    "member_name",
    "member_type",
    "display_name",
    "permissions",
    "status",
    "expires_at",
    "joined_at",
    "updated_at",
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _row_to_entity(row: sqlite3.Row) -> TeamEntityRecord:
    try:
        extra = json.loads(row["extra"] or "{}")
    except (TypeError, ValueError):
        extra = {}
    return TeamEntityRecord(
        team_id=row["team_id"],
        team_name=row["team_name"],
        owner=OwnerIdentity(
            user_id=row["owner_user_id"],
            group_id=row["owner_group_id"],
            bot_id=row["owner_bot_id"],
        ),
        status=row["status"],
        runtime_mode=row["runtime_mode"],
        template_id=row["template_id"],
        team_session_id=row["team_session_id"],
        runtime_team_name=row["runtime_team_name"],
        agent_service_id=row["agent_service_id"],
        gateway_id=row["gateway_id"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        extra=extra if isinstance(extra, dict) else {},
    )


def _row_to_membership(row: sqlite3.Row) -> TeamMembershipRecord:
    try:
        perms = json.loads(row["permissions"] or "[]")
    except (TypeError, ValueError):
        perms = []
    return TeamMembershipRecord(
        membership_id=row["membership_id"],
        team_id=row["team_id"],
        identity=OwnerIdentity(
            user_id=row["user_id"], group_id=row["group_id"], bot_id=row["bot_id"]
        ),
        member_name=row["member_name"],
        member_type=row["member_type"],
        display_name=row["display_name"],
        permissions=tuple(str(p) for p in perms) if isinstance(perms, list) else (),
        status=row["status"],
        expires_at=row["expires_at"],
        joined_at=float(row["joined_at"]),
        updated_at=float(row["updated_at"]),
    )


class SqliteTeamControlStore:
    """Default ``TeamControlStore`` backed by a single SQLite file."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or get_user_workspace_dir() / "agent" / "teams" / "team_control.db"
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    @property
    def path(self) -> Path:
        return self._path

    # -- connection --

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

    # -- entities --

    def create_entity(
        self,
        *,
        team_name: str,
        owner: OwnerIdentity,
        template_id: str = "",
        team_session_id: str = "",
        runtime_team_name: str = "",
        status: str = TEAM_STATUS_CREATING,
        team_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> TeamEntityRecord:
        name = _clean(team_name)
        if not name:
            raise TeamControlStoreError("team_name is required", code="BAD_REQUEST")
        if status not in TEAM_STATUSES:
            raise TeamControlStoreError(f"invalid team status: {status}", code="BAD_REQUEST")
        now = time.time()
        record = TeamEntityRecord(
            team_id=_clean(team_id) or uuid.uuid4().hex,
            team_name=name,
            owner=owner,
            status=status,
            template_id=_clean(template_id),
            team_session_id=_clean(team_session_id),
            runtime_team_name=_clean(runtime_team_name),
            created_at=now,
            updated_at=now,
            extra=dict(extra or {}),
        )
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    f"INSERT INTO team_entity ({', '.join(_ENTITY_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in _ENTITY_COLUMNS)})",
                    (
                        record.team_id,
                        record.team_name,
                        owner.user_id,
                        owner.group_id,
                        owner.bot_id,
                        record.status,
                        record.runtime_mode,
                        record.template_id,
                        record.team_session_id,
                        record.runtime_team_name,
                        record.agent_service_id,
                        record.gateway_id,
                        record.created_at,
                        record.updated_at,
                        json.dumps(record.extra, ensure_ascii=False),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise TeamControlStoreError(
                    "team_name or team_id already exists", code="CONFLICT"
                ) from exc
        return record

    def get_entity(self, team_id: str) -> TeamEntityRecord | None:
        tid = _clean(team_id)
        if not tid:
            return None
        with self._lock:
            row = self._connect().execute(
                "SELECT * FROM team_entity WHERE team_id = ?", (tid,)
            ).fetchone()
        return _row_to_entity(row) if row else None

    def get_entity_by_name(self, team_name: str) -> TeamEntityRecord | None:
        name = _clean(team_name)
        if not name:
            return None
        with self._lock:
            row = self._connect().execute(
                "SELECT * FROM team_entity WHERE team_name = ?", (name,)
            ).fetchone()
        return _row_to_entity(row) if row else None

    def get_entity_by_session(self, session_id: str) -> TeamEntityRecord | None:
        sid = _clean(session_id)
        if not sid:
            return None
        with self._lock:
            row = self._connect().execute(
                "SELECT * FROM team_entity WHERE team_session_id = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (sid,),
            ).fetchone()
        return _row_to_entity(row) if row else None

    def list_entities(
        self,
        *,
        owner: OwnerIdentity | None = None,
        include_deleted: bool = False,
    ) -> list[TeamEntityRecord]:
        clauses: list[str] = []
        args: list[Any] = []
        if owner is not None:
            clauses.append("owner_user_id = ? AND owner_group_id = ? AND owner_bot_id = ?")
            args.extend([owner.user_id, owner.group_id, owner.bot_id])
        if not include_deleted:
            clauses.append("status != ?")
            args.append(TEAM_STATUS_DELETED)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._connect().execute(
                f"SELECT * FROM team_entity {where} ORDER BY updated_at DESC", args
            ).fetchall()
        return [_row_to_entity(r) for r in rows]

    def update_entity(self, team_id: str, **fields: Any) -> TeamEntityRecord | None:
        tid = _clean(team_id)
        if not tid:
            return None
        unknown = set(fields) - _ENTITY_UPDATABLE
        if unknown:
            raise TeamControlStoreError(f"unknown entity fields: {sorted(unknown)}")
        if "status" in fields and fields["status"] not in TEAM_STATUSES:
            raise TeamControlStoreError(f"invalid team status: {fields['status']}")
        if not fields:
            return self.get_entity(tid)
        values: dict[str, Any] = {}
        for key, value in fields.items():
            values[key] = (
                json.dumps(dict(value or {}), ensure_ascii=False) if key == "extra" else _clean(value)
            )
        values["updated_at"] = time.time()
        assignments = ", ".join(f"{k} = ?" for k in values)
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    f"UPDATE team_entity SET {assignments} WHERE team_id = ?",
                    [*values.values(), tid],
                )
            except sqlite3.IntegrityError as exc:
                raise TeamControlStoreError("team_name already exists", code="CONFLICT") from exc
            if cur.rowcount == 0:
                return None
        return self.get_entity(tid)

    def set_status(self, team_id: str, status: str) -> TeamEntityRecord | None:
        return self.update_entity(team_id, status=status)

    def set_status_by_name(self, team_name: str, status: str) -> TeamEntityRecord | None:
        entity = self.get_entity_by_name(team_name)
        if entity is None:
            return None
        return self.update_entity(entity.team_id, status=status)

    def delete_entity(self, team_id: str) -> bool:
        tid = _clean(team_id)
        if not tid:
            return False
        with self._lock:
            conn = self._connect()
            conn.execute("DELETE FROM team_membership WHERE team_id = ?", (tid,))
            cur = conn.execute("DELETE FROM team_entity WHERE team_id = ?", (tid,))
            return cur.rowcount > 0

    # -- memberships --

    def upsert_membership(
        self,
        *,
        team_id: str,
        identity: OwnerIdentity,
        member_name: str,
        display_name: str = "",
        member_type: str = MEMBER_TYPE_HUMAN,
        permissions: Iterable[str] | None = None,
        status: str = MEMBERSHIP_STATUS_PENDING,
        expires_at: float | None = None,
    ) -> TeamMembershipRecord:
        tid = _clean(team_id)
        name = _clean(member_name)
        if not tid or not name:
            raise TeamControlStoreError("team_id and member_name are required")
        if not _clean(identity.user_id):
            raise TeamControlStoreError("user_id is required")
        if status not in MEMBERSHIP_STATUSES:
            raise TeamControlStoreError(f"invalid membership status: {status}")
        perms = tuple(permissions) if permissions is not None else DEFAULT_HUMAN_PERMISSIONS
        now = time.time()
        with self._lock:
            conn = self._connect()
            existing = conn.execute(
                "SELECT * FROM team_membership WHERE team_id = ? AND user_id = ?",
                (tid, identity.user_id),
            ).fetchone()
            if existing is not None:
                record = replace(
                    _row_to_membership(existing),
                    identity=identity,
                    member_name=name,
                    display_name=_clean(display_name) or existing["display_name"],
                    member_type=member_type,
                    permissions=perms,
                    status=status,
                    expires_at=expires_at,
                    updated_at=now,
                )
                try:
                    conn.execute(
                        "UPDATE team_membership SET group_id=?, bot_id=?, member_name=?, "
                        "member_type=?, display_name=?, permissions=?, status=?, "
                        "expires_at=?, updated_at=? WHERE membership_id=?",
                        (
                            identity.group_id,
                            identity.bot_id,
                            record.member_name,
                            record.member_type,
                            record.display_name,
                            json.dumps(list(record.permissions)),
                            record.status,
                            record.expires_at,
                            record.updated_at,
                            record.membership_id,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise TeamControlStoreError(
                        f"member_name '{name}' is already taken", code="CONFLICT"
                    ) from exc
                return record
            record = TeamMembershipRecord(
                membership_id=uuid.uuid4().hex,
                team_id=tid,
                identity=identity,
                member_name=name,
                member_type=member_type,
                display_name=_clean(display_name),
                permissions=perms,
                status=status,
                expires_at=expires_at,
                joined_at=now,
                updated_at=now,
            )
            try:
                conn.execute(
                    f"INSERT INTO team_membership ({', '.join(_MEMBERSHIP_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in _MEMBERSHIP_COLUMNS)})",
                    (
                        record.membership_id,
                        record.team_id,
                        identity.user_id,
                        identity.group_id,
                        identity.bot_id,
                        record.member_name,
                        record.member_type,
                        record.display_name,
                        json.dumps(list(record.permissions)),
                        record.status,
                        record.expires_at,
                        record.joined_at,
                        record.updated_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise TeamControlStoreError(
                    f"member_name '{name}' is already taken", code="CONFLICT"
                ) from exc
            return record

    def get_membership(self, team_id: str, user_id: str) -> TeamMembershipRecord | None:
        tid, uid = _clean(team_id), _clean(user_id)
        if not tid or not uid:
            return None
        with self._lock:
            row = self._connect().execute(
                "SELECT * FROM team_membership WHERE team_id = ? AND user_id = ?", (tid, uid)
            ).fetchone()
        return _row_to_membership(row) if row else None

    def get_membership_by_member_name(
        self, team_id: str, member_name: str
    ) -> TeamMembershipRecord | None:
        tid, name = _clean(team_id), _clean(member_name)
        if not tid or not name:
            return None
        with self._lock:
            row = self._connect().execute(
                "SELECT * FROM team_membership WHERE team_id = ? AND member_name = ?",
                (tid, name),
            ).fetchone()
        return _row_to_membership(row) if row else None

    def list_memberships(
        self, team_id: str, *, statuses: Iterable[str] | None = None
    ) -> list[TeamMembershipRecord]:
        tid = _clean(team_id)
        if not tid:
            return []
        args: list[Any] = [tid]
        where = "team_id = ?"
        if statuses is not None:
            wanted = [s for s in statuses if s]
            if not wanted:
                return []
            where += f" AND status IN ({', '.join('?' for _ in wanted)})"
            args.extend(wanted)
        with self._lock:
            rows = self._connect().execute(
                f"SELECT * FROM team_membership WHERE {where} ORDER BY joined_at ASC", args
            ).fetchall()
        return [_row_to_membership(r) for r in rows]

    def set_membership_status(
        self, membership_id: str, status: str
    ) -> TeamMembershipRecord | None:
        mid = _clean(membership_id)
        if not mid:
            return None
        if status not in MEMBERSHIP_STATUSES:
            raise TeamControlStoreError(f"invalid membership status: {status}")
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                "UPDATE team_membership SET status = ?, updated_at = ? WHERE membership_id = ?",
                (status, time.time(), mid),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM team_membership WHERE membership_id = ?", (mid,)
            ).fetchone()
        return _row_to_membership(row) if row else None


# ---------------------------------------------------------------------------
# Default store singleton
# ---------------------------------------------------------------------------

_DEFAULT_STORE: TeamControlStore | None = None
_DEFAULT_STORE_LOCK = threading.Lock()


def get_team_control_store() -> TeamControlStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        with _DEFAULT_STORE_LOCK:
            if _DEFAULT_STORE is None:
                from jiuwenswarm.infrastructure.db.team_mysql_actor import use_team_mysql

                if use_team_mysql():
                    from jiuwenswarm.enterprise.team.control_mysql import (
                        MysqlTeamControlStore,
                    )

                    _DEFAULT_STORE = MysqlTeamControlStore()
                else:
                    _DEFAULT_STORE = SqliteTeamControlStore()
    return _DEFAULT_STORE


def set_team_control_store(store: TeamControlStore | None) -> None:
    """Override the process-wide store (tests / alternative backends)."""
    global _DEFAULT_STORE
    with _DEFAULT_STORE_LOCK:
        _DEFAULT_STORE = store


__all__ = [
    "DEFAULT_HUMAN_PERMISSIONS",
    "MEMBER_TYPE_HUMAN",
    "MEMBERSHIP_STATUS_ACTIVE",
    "MEMBERSHIP_STATUS_EXPIRED",
    "MEMBERSHIP_STATUS_LEFT",
    "MEMBERSHIP_STATUS_PENDING",
    "MEMBERSHIP_STATUS_REVOKED",
    "MEMBERSHIP_STATUSES",
    "OwnerIdentity",
    "RUNTIME_MODE_LOCAL",
    "SqliteTeamControlStore",
    "TEAM_ACTIVE_STATUSES",
    "TEAM_STATUS_CREATING",
    "TEAM_STATUS_DELETED",
    "TEAM_STATUS_PAUSED",
    "TEAM_STATUS_RUNNING",
    "TEAM_STATUS_STOPPED",
    "TEAM_STATUS_STOPPING",
    "TEAM_STATUSES",
    "TeamControlStore",
    "TeamControlStoreError",
    "TeamEntityRecord",
    "TeamMembershipRecord",
    "get_team_control_store",
    "set_team_control_store",
]
