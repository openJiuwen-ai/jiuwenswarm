"""The Web private conversations listed for each colleague.

Each conversation is a separate Session (``ConversationRef.web(conversation_id)``),
so it has its own history and window. This table only adds what the list shows: a
title and the time of the latest message. Conversation previews come from the latest
Request in the Session. Pin state is stored with the conversation. A conversation is written when its first
message is accepted, so opening a new one creates nothing. Deleting hides it from the
list; its Requests and window stay with the Session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError, require_identifier
from jiuwenswarm.server.jiuwenmate.sqlite_store import SQLiteStore

TITLE_CHARS = 20
MAX_TITLE_CHARS = 50

_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_conversations (
    namespace_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (namespace_id, agent_id, conversation_id)
);
CREATE INDEX IF NOT EXISTS web_conversations_by_activity
    ON web_conversations (namespace_id, agent_id, deleted_at, updated_at);
"""


def default_title(text: str) -> str:
    """The first line of the first message, cut to ``TITLE_CHARS`` characters."""
    first = " ".join(text.split())
    return first[:TITLE_CHARS] or "新对话"


@dataclass(frozen=True, kw_only=True)
class WebConversation:
    agent_id: str
    conversation_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    pinned: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "conversation_id": self.conversation_id,
            "title": self.title,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "pinned": self.pinned,
        }


class WebConversationStore(SQLiteStore):
    _schema = _SCHEMA

    def __init__(self, db_path):
        super().__init__(db_path)
        with self._transaction() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(web_conversations)")}
            if "pinned" not in columns:
                conn.execute("ALTER TABLE web_conversations ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")

    @staticmethod
    def _conversation(row) -> WebConversation:
        return WebConversation(
            agent_id=row["agent_id"],
            conversation_id=row["conversation_id"],
            title=row["title"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            pinned=bool(row["pinned"]),
        )

    def _row(self, conn, namespace_id: str, agent_id: str, conversation_id: str):
        return conn.execute(
            "SELECT * FROM web_conversations WHERE namespace_id = ? AND agent_id = ? AND conversation_id = ?",
            (namespace_id, agent_id, conversation_id),
        ).fetchone()

    def require_open(self, namespace_id: str, agent_id: str, conversation_id: str) -> None:
        """Refuse a conversation that was deleted; one that does not exist yet is open."""
        require_identifier("conversation_id", conversation_id)
        with self._transaction() as conn:
            row = self._row(conn, namespace_id, agent_id, conversation_id)
        if row is not None and row["deleted_at"] is not None:
            raise PersistentAgentError("conversation not found", code="NOT_FOUND")

    def touch(
        self, namespace_id: str, agent_id: str, conversation_id: str, *, text: str, now: datetime
    ) -> WebConversation:
        """Record a message: the first one names the conversation, each one moves it to the top."""
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO web_conversations "
                "(namespace_id, agent_id, conversation_id, title, created_at, updated_at, deleted_at, pinned) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, 0) "
                "ON CONFLICT (namespace_id, agent_id, conversation_id) DO UPDATE SET updated_at = excluded.updated_at",
                (namespace_id, agent_id, conversation_id, default_title(text), now.isoformat(), now.isoformat()),
            )
            row = self._row(conn, namespace_id, agent_id, conversation_id)
        return self._conversation(row)

    def adopt(
        self,
        namespace_id: str,
        agent_id: str,
        conversation_id: str,
        *,
        text: str,
        created_at: datetime,
        updated_at: datetime,
    ) -> None:
        """List a conversation that has messages from before this table; a known one is left unchanged."""
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO web_conversations "
                "(namespace_id, agent_id, conversation_id, title, created_at, updated_at, deleted_at, pinned) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, 0)",
                (
                    namespace_id,
                    agent_id,
                    conversation_id,
                    default_title(text),
                    created_at.isoformat(),
                    updated_at.isoformat(),
                ),
            )

    def known(self, namespace_id: str, agent_id: str, conversation_id: str) -> bool:
        with self._transaction() as conn:
            return self._row(conn, namespace_id, agent_id, conversation_id) is not None

    def list(self, namespace_id: str, agent_id: str) -> list[WebConversation]:
        """Conversations not deleted, latest message first."""
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM web_conversations WHERE namespace_id = ? AND agent_id = ? AND deleted_at IS NULL "
                "ORDER BY pinned DESC, updated_at DESC, created_at DESC",
                (namespace_id, agent_id),
            ).fetchall()
        return [self._conversation(row) for row in rows]

    def rename(self, namespace_id: str, agent_id: str, conversation_id: str, title: str) -> WebConversation:
        title = title.strip() if isinstance(title, str) else ""
        if not title or len(title) > MAX_TITLE_CHARS:
            raise PersistentAgentError("invalid title")
        with self._transaction() as conn:
            updated = conn.execute(
                "UPDATE web_conversations SET title = ? WHERE namespace_id = ? AND agent_id = ? "
                "AND conversation_id = ? AND deleted_at IS NULL",
                (title, namespace_id, agent_id, conversation_id),
            ).rowcount
            row = self._row(conn, namespace_id, agent_id, conversation_id)
        if not updated:
            raise PersistentAgentError("conversation not found", code="NOT_FOUND")
        return self._conversation(row)

    def set_pinned(
        self, namespace_id: str, agent_id: str, conversation_id: str, pinned: bool
    ) -> WebConversation:
        if not isinstance(pinned, bool):
            raise PersistentAgentError("pinned must be boolean")
        with self._transaction() as conn:
            updated = conn.execute(
                "UPDATE web_conversations SET pinned = ? WHERE namespace_id = ? AND agent_id = ? "
                "AND conversation_id = ? AND deleted_at IS NULL",
                (int(pinned), namespace_id, agent_id, conversation_id),
            ).rowcount
            row = self._row(conn, namespace_id, agent_id, conversation_id)
        if not updated:
            raise PersistentAgentError("conversation not found", code="NOT_FOUND")
        return self._conversation(row)

    def delete(self, namespace_id: str, agent_id: str, conversation_id: str, *, now: datetime) -> None:
        with self._transaction() as conn:
            deleted = conn.execute(
                "UPDATE web_conversations SET deleted_at = ? WHERE namespace_id = ? AND agent_id = ? "
                "AND conversation_id = ? AND deleted_at IS NULL",
                (now.isoformat(), namespace_id, agent_id, conversation_id),
            ).rowcount
        if not deleted:
            raise PersistentAgentError("conversation not found", code="NOT_FOUND")

    def purge_agent(self, namespace_id: str, agent_id: str) -> None:
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM web_conversations WHERE namespace_id = ? AND agent_id = ?", (namespace_id, agent_id)
            )
