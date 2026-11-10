"""JiuwenMate groups: one Swarm ``group_chat_id`` per platform chat.

Each colleague's Session in a group is keyed by this ``group_chat_id`` (see
``ExecutionRepository``), so every colleague in the same group reads the same
public record. Group chats of the personal edition are never read here.
"""

from __future__ import annotations

import sqlite3
import uuid
import re
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError, require_identifier
from jiuwenswarm.server.jiuwenmate.sqlite_store import SQLiteStore

SHARING_INVALIDATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS group_share_invalidations (
    namespace_id TEXT NOT NULL, request_id TEXT NOT NULL, agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL, invalidated_at TEXT NOT NULL,
    PRIMARY KEY(namespace_id, request_id)
);
CREATE INDEX IF NOT EXISTS group_share_invalidations_session
    ON group_share_invalidations(namespace_id, agent_id, session_id);
"""

_SCHEMA = SHARING_INVALIDATION_SCHEMA + """
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
CREATE TABLE IF NOT EXISTS group_dissolutions (
    namespace_id TEXT NOT NULL, group_chat_id TEXT NOT NULL,
    changed_at TEXT NOT NULL, source_ref TEXT NOT NULL,
    PRIMARY KEY(namespace_id, group_chat_id)
);
CREATE TABLE IF NOT EXISTS group_name_events (
    namespace_id TEXT NOT NULL, group_chat_id TEXT NOT NULL, changed_at TEXT NOT NULL,
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
                       *, observed_at: datetime, source_ref: str, visible_from: datetime | None = None,
                       message_time: datetime | None = None) -> None:
        """Admit a trusted group message; only a newer platform message can prove re-entry."""
        self.get(namespace_id, group_id)
        for value in (agent_id, app_id, source_ref):
            require_identifier("access identity", value)
        stamp = utc_time(observed_at).isoformat()
        with self._transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM group_dissolutions WHERE namespace_id=? AND group_chat_id=?",
                (namespace_id, group_id),
            ).fetchone():
                return
            row = conn.execute(
                "SELECT * FROM agent_group_access WHERE namespace_id=? AND agent_id=? AND group_id=? AND app_id=?",
                (namespace_id, agent_id, group_id, app_id),
            ).fetchone()
            if row is not None and row["status"] != "active":
                proof = utc_time(message_time).isoformat() if message_time is not None else None
                if proof is None or proof <= row["verified_at"]:
                    return
                if stamp < row["verified_at"]:
                    return
            if row is not None and row["status"] == "active" and message_time is not None:
                # A delayed removal must not override a newer authenticated message.
                proof = utc_time(message_time).isoformat()
                conn.execute(
                    "UPDATE agent_group_access SET verified_at=?,source_ref=?,revision=revision+1 "
                    "WHERE namespace_id=? AND agent_id=? AND group_id=? AND app_id=? AND verified_at<?",
                    (proof, source_ref, namespace_id, agent_id, group_id, app_id, proof),
                )
                return
            conn.execute(
                "INSERT INTO agent_group_access VALUES (?, ?, ?, ?, 'active', ?, ?, ?, 1) "
                "ON CONFLICT(namespace_id,agent_id,group_id,app_id) DO UPDATE SET status='active', "
                "visible_from=excluded.visible_from,verified_at=excluded.verified_at,source_ref=excluded.source_ref, "
                "revision=agent_group_access.revision+1 WHERE agent_group_access.status!='active' "
                "AND excluded.verified_at>=agent_group_access.verified_at",
                (
                    namespace_id,
                    agent_id,
                    group_id,
                    app_id,
                    utc_time(visible_from or message_time or observed_at).isoformat(),
                    stamp,
                    source_ref,
                ),
            )

    def update_membership(self, namespace_id: str, agent_id: str, group_id: str, app_id: str,
                          *, status: str, observed_at: datetime, source_ref: str,
                          expected_revision: int | None = None) -> None:
        """Apply trusted current membership facts; late lifecycle events cannot reverse a newer fact."""
        if status not in ("active", "left", "unknown"):
            raise PersistentAgentError("invalid group access status")
        self.get(namespace_id, group_id)
        for value in (agent_id, app_id, source_ref):
            require_identifier("access identity", value)
        stamp = utc_time(observed_at).isoformat()
        key = (namespace_id, agent_id, group_id, app_id)
        with self._transaction() as conn:
            dissolved = conn.execute(
                "SELECT changed_at FROM group_dissolutions WHERE namespace_id=? AND group_chat_id=?",
                (namespace_id, group_id),
            ).fetchone()
            if status == "active" and dissolved is not None:
                return
            row = conn.execute(
                "SELECT * FROM agent_group_access WHERE namespace_id=? AND agent_id=? AND group_id=? AND app_id=?",
                key,
            ).fetchone()
            # Positive events/probes can refresh an admitted binding, never admit or revive one.
            if status == "active" and (row is None or row["status"] != "active"):
                return
            if expected_revision is not None and (row is None or row["revision"] != expected_revision):
                return
            if row is not None and stamp < row["verified_at"]:
                return
            same_verification = row is not None and stamp == row["verified_at"]
            if same_verification and row["status"] != "active" and status == "active":
                return
            visible = (
                stamp if row is None or (row["status"] != "active" and status == "active") else row["visible_from"]
            )
            revision = 1 if row is None else row["revision"] + 1
            conn.execute("INSERT OR REPLACE INTO agent_group_access VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (*key, status, visible, stamp, source_ref, revision))
            if status == "left":
                remaining = conn.execute(
                    "SELECT 1 FROM agent_group_access WHERE namespace_id=? AND agent_id=? AND group_id=? "
                    "AND status='active'", (namespace_id, agent_id, group_id),
                ).fetchone()
                if remaining is None:
                    self._retire_sharing(conn, namespace_id, group_id, stamp, agent_id)

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

    def mark_dissolved(self, namespace_id: str, group_id: str, *, observed_at: datetime, source_ref: str) -> None:
        """Dissolution is terminal for this platform group, including all sharing authority."""
        self.get(namespace_id, group_id)
        require_identifier("dissolution source", source_ref)
        stamp = utc_time(observed_at).isoformat()
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO group_dissolutions VALUES (?, ?, ?, ?) "
                "ON CONFLICT(namespace_id,group_chat_id) DO UPDATE SET "
                "changed_at=excluded.changed_at,source_ref=excluded.source_ref "
                "WHERE excluded.changed_at>=group_dissolutions.changed_at",
                (namespace_id, group_id, stamp, source_ref),
            )
            conn.execute(
                "UPDATE agent_group_access SET status='left', verified_at=MAX(verified_at, ?), "
                "source_ref=?, revision=revision+1 WHERE namespace_id=? AND group_id=?",
                (stamp, "group:dissolved:" + source_ref, namespace_id, group_id),
            )
            self._retire_sharing(conn, namespace_id, group_id, stamp)

    @staticmethod
    def _retire_sharing(conn, namespace_id, group_id, stamp, agent_id=None):
        """Revoke receipts and grants in the same transaction as the membership tombstone."""
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='group_share_approvals'").fetchone() is None:
            return
        group = conn.execute(
            "SELECT platform,platform_chat_id FROM group_chats WHERE namespace_id=? AND group_chat_id=?",
            (namespace_id, group_id),
        ).fetchone()

        def affected(source_ids, target):
            return group_id in source_ids or (
                target.get("kind") == "group" and target.get("channel") == group["platform"]
                and target.get("id") == group["platform_chat_id"]
            )

        filter_sql = "namespace_id=?" + (" AND agent_id=?" if agent_id is not None else "")
        args = (namespace_id, agent_id) if agent_id is not None else (namespace_id,)
        # Use ingestion time for context invalidation: a delayed platform event
        # must also advance a window already cut over by a newer, unrelated event.
        boundary = max(stamp, datetime.now(timezone.utc).isoformat())

        def invalidate(request_id, agent, session_id):
            conn.execute(
                "INSERT OR IGNORE INTO group_share_invalidations VALUES (?,?,?,?,?)",
                (namespace_id, request_id, agent, session_id, boundary),
            )

        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='sessions'").fetchone():
            for table in ("group_share_request_windows", "requests"):
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone() is None:
                    continue
                condition = " AND s.agent_id=?" if agent_id is not None else ""
                params = (namespace_id, group_id, agent_id) if agent_id is not None else (namespace_id, group_id)
                rows = conn.execute(
                    f"SELECT r.request_id,r.agent_id,r.session_id FROM {table} r JOIN sessions s "
                    "ON s.namespace_id=r.namespace_id AND s.session_id=r.session_id "
                    "WHERE s.namespace_id=? AND s.kind='group' AND s.conversation_key=?" + condition, params,
                ).fetchall()
                for row in rows:
                    invalidate(row["request_id"], row["agent_id"], row["session_id"])
        for row in conn.execute("SELECT * FROM group_share_grants WHERE " + filter_sql + " AND status='active'", args):
            if affected([row["source_group_id"]], json.loads(row["target_json"])):
                conn.execute("UPDATE group_share_grants SET status='revoked',revoked_at=? WHERE grant_id=?",
                             (stamp, row["grant_id"]))
        for row in conn.execute("SELECT * FROM group_share_approvals WHERE " + filter_sql, args):
            sources = [item["group_id"] for item in json.loads(row["source_groups_json"])]
            if not affected(sources, json.loads(row["scope_json"])["target"]):
                continue
            session_id = json.loads(row["result_json"])["session_id"]
            invalidate(row["request_id"], row["agent_id"], session_id)
            # Leave pending rows for the normal timeout path to settle native tool confirmations.
            conn.execute("UPDATE group_share_approvals SET expires_at=MIN(expires_at,?) "
                         "WHERE approval_id=? AND status='pending'", (stamp, row["approval_id"]))

    def directory(self, namespace_id: str, agent_id: str) -> tuple[dict[str, str], ...]:
        """Return only this Agent's recorded group metadata, including confirmed departures."""
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT a.group_id, a.status, a.source_ref, a.verified_at, g.platform, d.name, x.changed_at "
                "FROM agent_group_access a JOIN group_chats g "
                "ON g.namespace_id=a.namespace_id AND g.group_chat_id=a.group_id "
                "LEFT JOIN group_details d ON d.namespace_id=a.namespace_id AND d.group_chat_id=a.group_id "
                "LEFT JOIN group_dissolutions x ON x.namespace_id=a.namespace_id AND x.group_chat_id=a.group_id "
                "WHERE a.namespace_id=? AND a.agent_id=? ORDER BY a.group_id, a.verified_at DESC, a.app_id",
                (namespace_id, agent_id),
            ).fetchall()
        by_group: dict[str, list] = {}
        for row in rows:
            by_group.setdefault(row["group_id"], []).append(row)
        result = []
        for group_id, bindings in by_group.items():
            active = [item for item in bindings if item["status"] == "active"]
            row = active[0] if len(active) == 1 else bindings[0]
            status = "unknown" if len(active) > 1 else row["status"]
            if row["changed_at"] is not None and (status != "active" or row["changed_at"] >= row["verified_at"]):
                status = "dissolved"
            result.append({"group_id": group_id, "platform": row["platform"],
                           "name": row["name"] or group_id, "status": status})
        return tuple(sorted(result, key=lambda item: (item["status"] != "active", item["platform"], item["group_id"])))

    def unresolved_accesses(self, namespace_id: str, agent_id: str) -> tuple[GroupAccess, ...]:
        """Return unknown memberships that are not known retired Bot bindings."""
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_group_access WHERE namespace_id=? AND agent_id=? "
                "AND status='unknown' AND source_ref NOT LIKE 'binding:%' ORDER BY group_id, app_id",
                (namespace_id, agent_id),
            ).fetchall()
        return tuple(self._access(row) for row in rows)

    def set_name(self, namespace_id: str, group_id: str, name: str, *, observed_at: datetime | None = None) -> None:
        self.get(namespace_id, group_id)
        if not isinstance(name, str) or not name.strip() or len(name) > 512:
            raise PersistentAgentError("invalid group name")
        with self._transaction() as conn:
            if observed_at is not None:
                stamp = utc_time(observed_at).isoformat()
                previous = conn.execute(
                    "SELECT changed_at FROM group_name_events WHERE namespace_id=? AND group_chat_id=?",
                    (namespace_id, group_id),
                ).fetchone()
                if previous is not None and stamp < previous["changed_at"]:
                    return
                conn.execute(
                    "INSERT OR REPLACE INTO group_name_events VALUES (?, ?, ?)", (namespace_id, group_id, stamp),
                )
            conn.execute("INSERT OR REPLACE INTO group_details VALUES (?, ?, ?)", (namespace_id, group_id, name))

    def find(self, namespace_id: str, platform: str, platform_chat_id: str) -> GroupChat | None:
        """Look up an existing platform group without recording an unrelated tenant event."""
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM group_chats WHERE namespace_id=? AND platform=? AND platform_chat_id=?",
                               (namespace_id, platform, platform_chat_id)).fetchone()
        return None if row is None else self._group(row)

    def has_binding(self, namespace_id: str, agent_id: str, group_id: str, app_id: str) -> bool:
        with self._transaction() as conn:
            return conn.execute(
                "SELECT 1 FROM agent_group_access WHERE namespace_id=? AND agent_id=? AND group_id=? AND app_id=?",
                (namespace_id, agent_id, group_id, app_id),
            ).fetchone() is not None

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
