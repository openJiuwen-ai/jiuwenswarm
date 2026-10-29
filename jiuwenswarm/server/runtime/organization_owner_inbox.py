# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Ordered, explicitly acknowledged results for a stopped organization Owner."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator

from jiuwenswarm.common.utils import get_agent_root_dir


class OwnerInbox:
    """Durable receipt identities and invocation-fenced, FIFO acknowledgements."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS owner_inbox_consumers (
                    session_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                    enabled_at REAL NOT NULL, delivery_id TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(session_id, owner_id)
                );
                CREATE TABLE IF NOT EXISTS owner_inbox_results (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL, payload TEXT NOT NULL,
                    created_at REAL NOT NULL, delivery_id TEXT NOT NULL DEFAULT '',
                    acknowledged_at REAL
                );
                CREATE INDEX IF NOT EXISTS owner_inbox_pending
                    ON owner_inbox_results(session_id, owner_id, acknowledged_at, sequence);
            """)
            conn.execute("BEGIN IMMEDIATE")
            with conn:
                yield conn

    def enabled_at(self, session_id: str, owner_id: str) -> float | None:
        if not self.path.exists():
            return None
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT enabled_at FROM owner_inbox_consumers WHERE session_id=? AND owner_id=?",
                (session_id, owner_id),
            ).fetchone()
            return float(row[0]) if row else None

    def enable(self, session_id: str, owner_id: str, enabled_at: float | None = None) -> None:
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO owner_inbox_consumers(session_id,owner_id,enabled_at) VALUES(?,?,?)",
                (session_id, owner_id, time.time() if enabled_at is None else enabled_at),
            )

    def append(self, session_id: str, owner_id: str, payload: dict[str, Any], event_key: str = "") -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        identity = json.dumps([session_id, owner_id, event_key or encoded], ensure_ascii=False)
        message_id = hashlib.sha256(identity.encode()).hexdigest()
        with self._transaction() as conn:
            enabled = conn.execute(
                "SELECT 1 FROM owner_inbox_consumers WHERE session_id=? AND owner_id=?", (session_id, owner_id),
            ).fetchone()
            if enabled is None:
                raise ValueError("Owner inbox is not enabled")
            conn.execute(
                "INSERT OR IGNORE INTO owner_inbox_results"
                "(message_id,session_id,owner_id,payload,created_at) VALUES(?,?,?,?,?)",
                (message_id, session_id, owner_id, encoded, time.time()),
            )
            if payload.get("kind") == "leader_message":
                conn.execute(
                    "UPDATE owner_inbox_results SET payload=? WHERE message_id=? AND acknowledged_at IS NULL",
                    (encoded, message_id),
                )
        return message_id

    def disable(self, session_id: str, owner_id: str) -> None:
        """Roll back a failed first stop without deleting any captured results."""
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM owner_inbox_consumers WHERE session_id=? AND owner_id=?", (session_id, owner_id),
            )

    def begin_delivery(self, session_id: str, owner_id: str) -> str:
        """A new admitted invocation fences acknowledgements from an older one."""
        delivery_id = uuid.uuid4().hex
        with self._transaction() as conn:
            conn.execute(
                "UPDATE owner_inbox_consumers SET delivery_id=? WHERE session_id=? AND owner_id=?",
                (delivery_id, session_id, owner_id),
            )
        return delivery_id

    def get_result(self, session_id: str, owner_id: str, message_id: str) -> dict[str, Any] | None:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM owner_inbox_results WHERE session_id=? AND owner_id=? AND message_id=?",
                (session_id, owner_id, message_id),
            ).fetchone()
            if row is None:
                return None
            return {**dict(row), "payload": json.loads(row["payload"])}

    def next_result(self, session_id: str, owner_id: str, delivery_id: str) -> dict[str, Any] | None:
        with self._transaction() as conn:
            consumer = conn.execute(
                "SELECT delivery_id FROM owner_inbox_consumers WHERE session_id=? AND owner_id=?",
                (session_id, owner_id),
            ).fetchone()
            if consumer is None or consumer[0] != delivery_id:
                return None
            row = conn.execute(
                "SELECT * FROM owner_inbox_results WHERE session_id=? AND owner_id=?"
                " AND acknowledged_at IS NULL ORDER BY sequence LIMIT 1", (session_id, owner_id),
            ).fetchone()
            if row is None or row["delivery_id"] == delivery_id:
                return None
            conn.execute(
                "UPDATE owner_inbox_results SET delivery_id=? WHERE message_id=?", (delivery_id, row["message_id"]),
            )
            return {
                "message_id": row["message_id"], "sequence": row["sequence"],
                "delivery_id": delivery_id, "payload": json.loads(row["payload"]),
            }

    def acknowledge(self, session_id: str, owner_id: str, delivery_id: str, message_id: str) -> bool:
        """Acknowledge only the current head; an already acknowledged id is idempotent."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM owner_inbox_results WHERE session_id=? AND owner_id=? AND message_id=?",
                (session_id, owner_id, message_id),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown Owner inbox receipt")
            if row["acknowledged_at"] is not None:
                return False
            consumer = conn.execute(
                "SELECT delivery_id FROM owner_inbox_consumers WHERE session_id=? AND owner_id=?",
                (session_id, owner_id),
            ).fetchone()
            current_delivery = consumer is not None and consumer[0] == delivery_id
            if not delivery_id or not current_delivery or row["delivery_id"] != delivery_id:
                raise ValueError("Stale or undelivered Owner inbox receipt")
            head = conn.execute(
                "SELECT message_id FROM owner_inbox_results WHERE session_id=? AND owner_id=?"
                " AND acknowledged_at IS NULL ORDER BY sequence LIMIT 1", (session_id, owner_id),
            ).fetchone()
            if head[0] != message_id:
                raise ValueError("Owner inbox receipts must be handled in FIFO order")
            conn.execute(
                "UPDATE owner_inbox_results SET acknowledged_at=? WHERE message_id=?", (time.time(), message_id),
            )
        return True

    def release_result(self, session_id: str, owner_id: str, delivery_id: str, message_id: str) -> None:
        """Undo an unconfirmed context injection, without modifying another delivery."""
        with self._transaction() as conn:
            conn.execute(
                "UPDATE owner_inbox_results SET delivery_id='' WHERE session_id=? AND owner_id=?"
                " AND delivery_id=? AND message_id=? AND acknowledged_at IS NULL",
                (session_id, owner_id, delivery_id, message_id),
            )


def owner_inbox() -> OwnerInbox:
    return OwnerInbox(get_agent_root_dir() / "organization-owner-inbox.sqlite3")
