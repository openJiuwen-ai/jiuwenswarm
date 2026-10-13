"""JiuwenMate groups: one Swarm ``group_chat_id`` per platform chat.

Each colleague's Session in a group is keyed by this ``group_chat_id`` (see
``ExecutionRepository``), so every colleague in the same group reads the same
public record. Group chats of the personal edition are never read here.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
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
"""


@dataclass(frozen=True, kw_only=True)
class GroupChat:
    namespace_id: str
    group_chat_id: str
    platform: str
    platform_chat_id: str
    created_at: datetime


class GroupChatRepository(SQLiteStore):
    """Maps a platform chat to its Swarm group; the mapping never changes once created."""

    _schema = _SCHEMA

    def __init__(self, db_path: Path, *, id_factory: Callable[[], str] | None = None) -> None:
        super().__init__(db_path)
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
                conn.execute(
                    "INSERT INTO group_chats VALUES (?, ?, ?, ?, ?)",
                    (namespace_id, group_chat_id, platform, platform_chat_id, now.isoformat()),
                )
                row = conn.execute(
                    "SELECT * FROM group_chats WHERE namespace_id = ? AND group_chat_id = ?",
                    (namespace_id, group_chat_id),
                ).fetchone()
        return self._group(row)

    def get(self, namespace_id: str, group_chat_id: str) -> GroupChat:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM group_chats WHERE namespace_id = ? AND group_chat_id = ?",
                (namespace_id, group_chat_id),
            ).fetchone()
        if row is None:
            raise PersistentAgentError("group chat not found", code="NOT_FOUND")
        return self._group(row)
