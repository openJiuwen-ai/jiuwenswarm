"""JiuwenMate groups: one Swarm ``group_chat_id`` per platform chat.

Each colleague's Session in a group is keyed by this ``group_chat_id`` (see
``ExecutionRepository``), so every colleague in the same group reads the same
public record. Group chats of the personal edition are never read here.
"""

from __future__ import annotations

import sqlite3
import uuid
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError, require_identifier
from jiuwenswarm.server.jiuwenmate.sqlite_store import SQLiteStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS group_chats (
    namespace_id TEXT NOT NULL,
    group_chat_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    platform_chat_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (namespace_id, group_chat_id),
    UNIQUE (namespace_id, platform, platform_chat_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS group_chat_directory_id ON group_chats(group_chat_id);
CREATE TABLE IF NOT EXISTS group_details (
    namespace_id TEXT NOT NULL, group_chat_id TEXT NOT NULL, name TEXT NOT NULL,
    PRIMARY KEY(namespace_id, group_chat_id)
);
CREATE TABLE IF NOT EXISTS agent_group_access (
    namespace_id TEXT NOT NULL, agent_id TEXT NOT NULL, group_id TEXT NOT NULL, app_id TEXT NOT NULL,
    status TEXT NOT NULL, visible_from TEXT NOT NULL, verified_at TEXT NOT NULL,
    source_ref TEXT NOT NULL, revision INTEGER NOT NULL,
    PRIMARY KEY(namespace_id, agent_id, group_id, app_id)
);
"""


@dataclass(frozen=True, kw_only=True)
class GroupChat:
    namespace_id: str
    group_chat_id: str
    platform: str
    platform_chat_id: str
    created_at: datetime


@dataclass(frozen=True, kw_only=True)
class GroupAccess:
    namespace_id: str
    agent_id: str
    group_id: str
    app_id: str
    status: str
    visible_from: datetime
    verified_at: datetime
    source_ref: str
    revision: int


def utc_time(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PersistentAgentError("a timezone-aware time is required")
    return value.astimezone(timezone.utc)


class GroupChatRepository(SQLiteStore):
    """Maps a platform chat to its Swarm group; the mapping never changes once created."""

    _schema = _SCHEMA

    def __init__(self, db_path: Path, *, id_factory: Callable[[], str] | None = None) -> None:
        try:
            super().__init__(db_path)
        except sqlite3.IntegrityError as exc:
            raise PersistentAgentError("group directory IDs conflict across namespaces", code="CONFLICT") from exc
        self._new_id = id_factory or (lambda: "grp_" + uuid.uuid4().hex)

    @staticmethod
    def _group(row: sqlite3.Row) -> GroupChat:
        return GroupChat(
            namespace_id=row["namespace_id"],
            group_chat_id=row["group_chat_id"],
            platform=row["platform"],
            platform_chat_id=row["platform_chat_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _access(row: sqlite3.Row) -> GroupAccess:
        return GroupAccess(
            **{
                **dict(row),
                "visible_from": datetime.fromisoformat(row["visible_from"]),
                "verified_at": datetime.fromisoformat(row["verified_at"]),
            }
        )

    def resolve(self, namespace_id: str, platform: str, platform_chat_id: str, *, now: datetime) -> GroupChat:
        """Return the group of a platform chat, creating it on first contact."""
        for name, value in (
            ("namespace_id", namespace_id),
            ("platform", platform),
            ("platform_chat_id", platform_chat_id),
        ):
            require_identifier(name, value)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM group_chats WHERE namespace_id = ? AND platform = ? AND platform_chat_id = ?",
                (namespace_id, platform, platform_chat_id),
            ).fetchone()
            if row is None:
                group_chat_id = self._new_id()
                if not isinstance(group_chat_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", group_chat_id):
                    raise PersistentAgentError("unsafe internal group ID")
                if conn.execute("SELECT 1 FROM group_chats WHERE group_chat_id = ?", (group_chat_id,)).fetchone():
                    raise PersistentAgentError("group directory ID already exists", code="CONFLICT")
                conn.execute(
                    "INSERT INTO group_chats VALUES (?, ?, ?, ?, ?)",
                    (namespace_id, group_chat_id, platform, platform_chat_id, now.isoformat()),
                )
                row = conn.execute(
                    "SELECT * FROM group_chats WHERE namespace_id = ? AND group_chat_id = ?",
                    (namespace_id, group_chat_id),
                ).fetchone()
        return self._group(row)

    def observe_access(self, namespace_id: str, agent_id: str, group_id: str, app_id: str,
                       *, observed_at: datetime, source_ref: str, visible_from: datetime | None = None) -> None:
        """Observe a current binding; only binding-retired grants can reopen, never a departed Bot."""
        self.get(namespace_id, group_id)
        for value in (agent_id, app_id, source_ref):
            require_identifier("access identity", value)
        stamp = utc_time(observed_at).isoformat()
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO agent_group_access VALUES (?, ?, ?, ?, 'active', ?, ?, ?, 1) "
                "ON CONFLICT(namespace_id,agent_id,group_id,app_id) DO UPDATE SET status='active', "
                "visible_from=excluded.visible_from,verified_at=excluded.verified_at,source_ref=excluded.source_ref, "
                "revision=agent_group_access.revision+1 WHERE agent_group_access.status='unknown' "
                "AND agent_group_access.source_ref LIKE 'binding:%' "
                "AND excluded.verified_at>=agent_group_access.verified_at",
                (namespace_id, agent_id, group_id, app_id, utc_time(visible_from or observed_at).isoformat(), stamp, source_ref),
            )

    def update_membership(self, namespace_id: str, agent_id: str, group_id: str, app_id: str,
                          *, status: str, observed_at: datetime, source_ref: str) -> None:
        """Apply trusted current membership facts; late lifecycle events cannot reverse a newer fact."""
        if status not in ("active", "left", "unknown"):
            raise PersistentAgentError("invalid group access status")
        self.get(namespace_id, group_id)
        for value in (agent_id, app_id, source_ref):
            require_identifier("access identity", value)
        stamp = utc_time(observed_at).isoformat()
        key = (namespace_id, agent_id, group_id, app_id)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM agent_group_access WHERE namespace_id=? AND agent_id=? AND group_id=? AND app_id=?",
                key,
            ).fetchone()
            if row is not None and stamp < row["verified_at"]:
                return
            if row is not None and stamp == row["verified_at"] and row["status"] != "active" and status == "active":
                return
            visible = stamp if row is None or (row["status"] != "active" and status == "active") else row["visible_from"]
            revision = 1 if row is None else row["revision"] + 1
            conn.execute("INSERT OR REPLACE INTO agent_group_access VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (*key, status, visible, stamp, source_ref, revision))

    def access_for(self, namespace_id: str, agent_id: str, group_id: str) -> GroupAccess:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_group_access WHERE namespace_id=? AND agent_id=? AND group_id=? "
                "ORDER BY verified_at DESC", (namespace_id, agent_id, group_id),
            ).fetchall()
        if not rows or sum(row["status"] == "active" for row in rows) > 1:
            raise PersistentAgentError("group access unavailable", code="FORBIDDEN")
        row = next((item for item in rows if item["status"] == "active"), rows[0])
        return GroupAccess(**{**dict(row), "visible_from": datetime.fromisoformat(row["visible_from"]),
                              "verified_at": datetime.fromisoformat(row["verified_at"])})

    def active_apps(self, namespace_id: str, agent_id: str, group_id: str) -> tuple[str, ...]:
        with self._transaction() as conn:
            rows = conn.execute("SELECT app_id FROM agent_group_access WHERE namespace_id=? AND agent_id=? "
                                "AND group_id=? AND status='active'", (namespace_id, agent_id, group_id)).fetchall()
        return tuple(row["app_id"] for row in rows)

    def active_accesses(self, namespace_id: str, agent_id: str) -> tuple[GroupAccess, ...]:
        """Return every active Agent-to-group/app binding in deterministic order."""
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_group_access WHERE namespace_id=? AND agent_id=? AND status='active' "
                "ORDER BY group_id, app_id",
                (namespace_id, agent_id),
            ).fetchall()
        return tuple(self._access(row) for row in rows)

    def active_group_members(self, namespace_id: str, group_id: str) -> tuple[GroupAccess, ...]:
        """Return active Agent bindings for one group without exposing other conversations."""
        self.get(namespace_id, group_id)
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_group_access WHERE namespace_id=? AND group_id=? AND status='active' "
                "ORDER BY agent_id, app_id",
                (namespace_id, group_id),
            ).fetchall()
        return tuple(self._access(row) for row in rows)

    def unresolved_accesses(self, namespace_id: str, agent_id: str) -> tuple[GroupAccess, ...]:
        """Return unknown memberships that are not known retired Bot bindings."""
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_group_access WHERE namespace_id=? AND agent_id=? "
                "AND status='unknown' AND source_ref NOT LIKE 'binding:%' ORDER BY group_id, app_id",
                (namespace_id, agent_id),
            ).fetchall()
        return tuple(self._access(row) for row in rows)

    def set_name(self, namespace_id: str, group_id: str, name: str) -> None:
        self.get(namespace_id, group_id)
        if not isinstance(name, str) or not name.strip() or len(name) > 512:
            raise PersistentAgentError("invalid group name")
        with self._transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO group_details VALUES (?, ?, ?)", (namespace_id, group_id, name))

    def name_of(self, namespace_id: str, group_id: str) -> str:
        self.get(namespace_id, group_id)
        with self._transaction() as conn:
            row = conn.execute("SELECT name FROM group_details WHERE namespace_id=? AND group_chat_id=?",
                               (namespace_id, group_id)).fetchone()
        return group_id if row is None else row["name"]

    def known_name_of(self, namespace_id: str, group_id: str) -> str | None:
        """Return an explicitly stored current name; an ID fallback is not a name mapping."""
        self.get(namespace_id, group_id)
        with self._transaction() as conn:
            row = conn.execute("SELECT name FROM group_details WHERE namespace_id=? AND group_chat_id=?",
                               (namespace_id, group_id)).fetchone()
        return None if row is None else row["name"]

    def get(self, namespace_id: str, group_chat_id: str) -> GroupChat:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM group_chats WHERE namespace_id = ? AND group_chat_id = ?",
                (namespace_id, group_chat_id),
            ).fetchone()
        if row is None:
            raise PersistentAgentError("group chat not found", code="NOT_FOUND")
        return self._group(row)
