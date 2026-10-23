"""Durable cumulative input/output token totals for a product Session."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Any

_MAX_TOKEN_COUNT = (1 << 53) - 1
_CATEGORIES = ("main", "others")


def _token_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= _MAX_TOKEN_COUNT else None


def classify_usage_owner(
    attribution: Mapping[str, Any] | None,
    *,
    fallback_role: str | None = None,
) -> str:
    """Put a model call into the main Agent or collaborator total."""
    values = attribution if isinstance(attribution, Mapping) else {}
    role = str(values.get("role") or fallback_role or "").strip().lower()
    if str(values.get("delegation_id") or "").strip():
        return "others"
    if role == "leader":
        return "main"
    if role == "teammate":
        return "others"
    depth = values.get("depth")
    if isinstance(depth, int) and not isinstance(depth, bool) and depth > 0:
        return "others"
    if any(str(values.get(key) or "").strip() for key in ("parent_invocation_id", "delegation_id")):
        return "others"
    path = values.get("agent_path")
    if isinstance(path, Sequence) and not isinstance(path, (str, bytes)) and len(path) > 1:
        return "others"
    if str(values.get("member_name") or "").strip():
        return "others"
    return "main"


class SessionTokenUsageLedger:
    """SQLite ledger keyed by Core's model-call request ID."""

    def __init__(self, session_dir: Path, session_id: str) -> None:
        self._path = Path(session_dir) / "session_token_usage.sqlite3"
        self._session_id = session_id

    def record(
        self,
        request_id: str,
        category: str,
        input_tokens: Any,
        output_tokens: Any,
    ) -> None:
        normalized_request_id = str(request_id or "").strip()
        if not normalized_request_id:
            raise ValueError("request_id is required")
        if category not in _CATEGORIES:
            raise ValueError("unsupported usage category")

        safe_input = _token_count(input_tokens)
        safe_output = _token_count(output_tokens)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self._path, timeout=10, isolation_level=None)) as connection:
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_events (
                    request_id TEXT PRIMARY KEY,
                    category TEXT NOT NULL CHECK (category IN ('main', 'others')),
                    input_tokens INTEGER,
                    output_tokens INTEGER
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    revision INTEGER NOT NULL
                )
                """
            )
            connection.execute("INSERT OR IGNORE INTO usage_state VALUES (1, 0)")
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO usage_events
                    (request_id, category, input_tokens, output_tokens)
                VALUES (?, ?, ?, ?)
                """,
                (normalized_request_id, category, safe_input, safe_output),
            )
            if cursor.rowcount:
                connection.execute("UPDATE usage_state SET revision = revision + 1 WHERE singleton = 1")
            connection.commit()

    def snapshot(self) -> dict[str, Any]:
        if not self._path.exists():
            return self._empty_snapshot()
        with closing(sqlite3.connect(self._path, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            table_rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            tables = {row[0] for row in table_rows}
            if not {"usage_events", "usage_state"}.issubset(tables):
                return self._empty_snapshot()
            return self._snapshot(connection)

    def _snapshot(self, connection: sqlite3.Connection) -> dict[str, Any]:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT revision FROM usage_state WHERE singleton = 1"
        ).fetchone()
        revision = int(row["revision"]) if row is not None else 0
        totals: dict[str, dict[str, int | None]] = {}
        for category in _CATEGORIES:
            aggregate = connection.execute(
                """
                SELECT COUNT(*) AS calls_total,
                       COUNT(input_tokens) AS input_calls,
                       SUM(input_tokens) AS input_tokens,
                       COUNT(output_tokens) AS output_calls,
                       SUM(output_tokens) AS output_tokens
                FROM usage_events WHERE category = ?
                """,
                (category,),
            ).fetchone()
            calls_total = int(aggregate["calls_total"])
            totals[category] = {
                "input_tokens": (
                    int(aggregate["input_tokens"]) if aggregate["input_tokens"] is not None
                    else 0 if calls_total == 0 else None
                ),
                "output_tokens": (
                    int(aggregate["output_tokens"]) if aggregate["output_tokens"] is not None
                    else 0 if calls_total == 0 else None
                ),
                "input_calls": int(aggregate["input_calls"]),
                "output_calls": int(aggregate["output_calls"]),
                "calls_total": calls_total,
            }
        return {
            "schema_version": 1,
            "revision": revision,
            "session_id": self._session_id,
            "main": totals["main"],
            "others": totals["others"],
        }

    def _empty_snapshot(self) -> dict[str, Any]:
        empty = {
            "input_tokens": None,
            "output_tokens": None,
            "input_calls": 0,
            "output_calls": 0,
            "calls_total": 0,
        }
        return {
            "schema_version": 1,
            "revision": 0,
            "session_id": self._session_id,
            "main": dict(empty),
            "others": dict(empty),
        }


def record_session_token_usage(
    *,
    session_id: str,
    request_id: str,
    attribution: Mapping[str, Any] | None,
    input_tokens: Any,
    output_tokens: Any,
    fallback_role: str | None = None,
) -> None:
    """Record one provider result under the durable product Session."""
    from jiuwenswarm.server.runtime.session import lifecycle, session_history

    session_dir, error = session_history.resolve_session_dir(session_id)
    if error or session_dir is None:
        raise ValueError("invalid session_id")
    with lifecycle.resource_lock("session", session_id):
        lifecycle.write_guard(session_id)
        ledger = SessionTokenUsageLedger(session_dir, session_id)
        ledger.record(
            request_id,
            classify_usage_owner(attribution, fallback_role=fallback_role),
            input_tokens,
            output_tokens,
        )


def get_session_token_usage(session_id: str) -> dict[str, Any]:
    """Read the durable cumulative usage snapshot for a product Session."""
    from jiuwenswarm.server.runtime.session import session_history

    history_path = session_history.get_read_history_path(session_id)
    return SessionTokenUsageLedger(history_path.parent, session_id).snapshot()
