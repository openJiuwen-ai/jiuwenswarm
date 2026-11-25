"""Durable mention reservations and atomic per-group automatic execution limits."""

from openjiuwen.harness.persistent_agent import ExecutionContext

from jiuwenswarm.server.jiuwenmate.sqlite_store import SQLiteStore


class SwarmChatConsumption(SQLiteStore):
    _schema = """
    CREATE TABLE IF NOT EXISTS swarmchat_stops (group_id TEXT NOT NULL, root_id TEXT NOT NULL,
        PRIMARY KEY(group_id, root_id));
    CREATE TABLE IF NOT EXISTS swarmchat_mentions (
        group_id TEXT NOT NULL, message_id TEXT NOT NULL, member_name TEXT NOT NULL,
        root_id TEXT NOT NULL, automatic INTEGER NOT NULL, accepted INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(group_id, message_id, member_name)
    );
    CREATE TABLE IF NOT EXISTS swarmchat_scan (
        group_id TEXT NOT NULL, member_name TEXT NOT NULL,
        timestamp INTEGER NOT NULL, message_id TEXT NOT NULL,
        PRIMARY KEY(group_id, member_name)
    );
    """

    def watermark(self, group_id: str, member_name: str) -> tuple[int, str] | None:
        """Position of the last public message the recovery scan finished for this member."""
        with self._transaction(write=False) as conn:
            row = conn.execute("SELECT timestamp, message_id FROM swarmchat_scan WHERE group_id=? AND member_name=?",
                               (group_id, member_name)).fetchone()
        return None if row is None else (row["timestamp"], row["message_id"])

    def advance(self, group_id: str, member_name: str, timestamp: int, message_id: str) -> None:
        """Move the scan position forward only; an older position never rewinds it."""
        with self._transaction() as conn:
            row = conn.execute("SELECT timestamp, message_id FROM swarmchat_scan WHERE group_id=? AND member_name=?",
                               (group_id, member_name)).fetchone()
            if row is not None and (row["timestamp"], row["message_id"]) >= (timestamp, message_id):
                return
            conn.execute("INSERT OR REPLACE INTO swarmchat_scan VALUES(?,?,?,?)",
                         (group_id, member_name, timestamp, message_id))

    def reserve(self, group_id: str, message_id: str, member_name: str, root_id: str, automatic: bool) -> str:
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM swarmchat_stops WHERE group_id=? AND root_id=?",
                            (group_id, root_id)).fetchone():
                return "stopped"
            row = conn.execute("SELECT accepted FROM swarmchat_mentions WHERE group_id=? AND message_id=? "
                               "AND member_name=?", (group_id, message_id, member_name)).fetchone()
            if row is not None:
                return "accepted" if row["accepted"] else "reserved"
            count = conn.execute("SELECT COUNT(*) FROM swarmchat_mentions WHERE group_id=? AND root_id=? "
                                 "AND automatic=1", (group_id, root_id)).fetchone()[0]
            if automatic and count >= 5:
                conn.execute(
                    "INSERT INTO swarmchat_mentions(group_id,message_id,member_name,root_id,automatic,accepted) "
                    "VALUES(?,?,?,?,0,1)", (group_id, message_id, member_name, root_id),
                )
                return "limited"
            conn.execute("INSERT INTO swarmchat_mentions(group_id,message_id,member_name,root_id,automatic) "
                         "VALUES(?,?,?,?,?)", (group_id, message_id, member_name, root_id, int(automatic)))
            return "reserved"

    def stop(self, group_id: str, root_id: str) -> None:
        with self._transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO swarmchat_stops VALUES(?,?)", (group_id, root_id))

    def accept(self, group_id: str, message_id: str, member_name: str) -> None:
        with self._transaction() as conn:
            conn.execute("UPDATE swarmchat_mentions SET accepted=1 WHERE group_id=? AND message_id=? AND member_name=?",
                         (group_id, message_id, member_name))


class SwarmChatFileImports(SQLiteStore):
    _schema = """
    CREATE TABLE IF NOT EXISTS swarmchat_file_imports (
        namespace_id TEXT NOT NULL, agent_id TEXT NOT NULL, session_id TEXT NOT NULL,
        imported_path TEXT NOT NULL, group_id TEXT NOT NULL, source_path TEXT NOT NULL, digest TEXT NOT NULL,
        PRIMARY KEY(namespace_id, agent_id, session_id, imported_path)
    );
    """

    def record(
        self, execution: ExecutionContext, imported_path: str, group_id: str, source_path: str, digest: str,
    ) -> None:
        with self._transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO swarmchat_file_imports VALUES(?,?,?,?,?,?,?)", (
                execution.namespace_id, execution.agent_id, execution.session_id,
                imported_path, group_id, source_path, digest,
            ))

    def for_session(self, execution: ExecutionContext) -> list[dict]:
        with self._transaction(write=False) as conn:
            rows = conn.execute("SELECT * FROM swarmchat_file_imports WHERE namespace_id=? AND agent_id=? "
                                "AND session_id=?", (execution.namespace_id, execution.agent_id,
                                                    execution.session_id)).fetchall()
        return [dict(row) for row in rows]
