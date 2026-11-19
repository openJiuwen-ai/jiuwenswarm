"""Short-lived opaque cursors for bounded cross-group search pages."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime
from typing import Any

from jiuwenswarm.server.jiuwenmate.group_chat_repository import utc_time
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError, require_identifier
from jiuwenswarm.server.jiuwenmate.sqlite_store import SQLiteStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS group_search_cursors (
    token_hash TEXT PRIMARY KEY,
    namespace_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    query_digest TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS group_search_cursor_expiry ON group_search_cursors(expires_at);
"""
_QUERY_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_STATE_BYTES = 65536


class GroupSearchCursorRepository(SQLiteStore):
    """Persists cursor hashes and search metadata, never the bearer token or messages."""

    _schema = _SCHEMA

    def issue(
        self,
        namespace_id: str,
        agent_id: str,
        query_digest: str,
        state: dict[str, Any],
        *,
        now: datetime,
        expires_at: datetime,
    ) -> str:
        require_identifier("namespace_id", namespace_id)
        require_identifier("agent_id", agent_id)
        if not isinstance(query_digest, str) or not _QUERY_DIGEST.fullmatch(query_digest):
            raise PersistentAgentError("invalid group search query digest")
        if not isinstance(state, dict):
            raise PersistentAgentError("invalid group search cursor state")
        created = utc_time(now)
        expiry = utc_time(expires_at)
        if expiry <= created:
            raise PersistentAgentError("group search cursor expiry must be in the future")
        try:
            state_json = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise PersistentAgentError("invalid group search cursor state") from exc
        if len(state_json.encode("utf-8")) > _MAX_STATE_BYTES:
            raise PersistentAgentError("group search cursor state is too large")

        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self._transaction() as conn:
            conn.execute("DELETE FROM group_search_cursors WHERE expires_at<=?", (created.isoformat(),))
            conn.execute(
                "INSERT INTO group_search_cursors VALUES (?,?,?,?,?,?,?)",
                (token_hash, namespace_id, agent_id, query_digest, state_json, created.isoformat(), expiry.isoformat()),
            )
        return token

    def load(
        self,
        token: str,
        namespace_id: str,
        agent_id: str,
        query_digest: str,
        *,
        now: datetime,
    ) -> dict[str, Any] | None:
        if not isinstance(token, str) or not token or len(token) > 512:
            return None
        require_identifier("namespace_id", namespace_id)
        require_identifier("agent_id", agent_id)
        if not isinstance(query_digest, str) or not _QUERY_DIGEST.fullmatch(query_digest):
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now_stamp = utc_time(now).isoformat()
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM group_search_cursors WHERE token_hash=? AND expires_at<=?",
                (token_hash, now_stamp),
            )
            row = conn.execute(
                "SELECT state_json FROM group_search_cursors WHERE token_hash=? AND namespace_id=? "
                "AND agent_id=? AND query_digest=? AND expires_at>?",
                (token_hash, namespace_id, agent_id, query_digest, now_stamp),
            ).fetchone()
            if row is None:
                return None
            try:
                state = json.loads(row["state_json"])
            except (TypeError, ValueError):
                conn.execute("DELETE FROM group_search_cursors WHERE token_hash=?", (token_hash,))
                return None
            if not isinstance(state, dict):
                conn.execute("DELETE FROM group_search_cursors WHERE token_hash=?", (token_hash,))
                return None
            return state
