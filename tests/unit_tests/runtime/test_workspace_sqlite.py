"""Workspace SQLite stores can use rollback journals on shared filesystems."""
import sqlite3

import pytest

from jiuwenswarm.agents.harness.common.memory import config as memory_config
from jiuwenswarm.agents.harness.common.memory.manager import MemoryIndexManager
from jiuwenswarm.common.sqlite_config import workspace_sqlite_journal_mode
from jiuwenswarm.server.runtime.session.session_message_store import SessionMessageStore


def test_mailbox_switches_existing_wal_to_delete_and_retains_data(tmp_path):
    path = tmp_path / "messages.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE existing (value TEXT)")
        connection.execute("INSERT INTO existing VALUES ('kept')")
    connection.close()

    store = SessionMessageStore(path, journal_mode="DELETE")
    store.ensure_schema()
    message, created = store.enqueue(
        owner_scope_id="user", source_session_id="source", source_title_snapshot="Source",
        source_request_id="request", source_tool_call_id="call", idempotency_key="call",
        target_session_id="target", content="hello",
    )
    assert created
    reopened = SessionMessageStore(path, journal_mode="DELETE")
    assert reopened.get(message.message_id).content == "hello"
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute("SELECT value FROM existing").fetchone()[0] == "kept"
    connection.close()


def test_memory_index_uses_workspace_journal_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(memory_config, "_load_config", lambda: {
        "workspace_sqlite": {"journal_mode": "DELETE"},
    })
    settings = memory_config.create_memory_settings(workspace_dir=str(tmp_path))
    manager = MemoryIndexManager("test", str(tmp_path), settings)
    connection = manager._open_database(str(tmp_path / "memory.db"))
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        connection.execute("CREATE TABLE existing(value TEXT)")
        connection.execute("INSERT INTO existing VALUES ('kept')")
        connection.commit()
    finally:
        connection.close()

    reopened = manager._open_database(str(tmp_path / "memory.db"))
    try:
        assert reopened.execute("SELECT value FROM existing").fetchone()[0] == "kept"
    finally:
        reopened.close()


@pytest.mark.parametrize("mode", ["MEMORY", "", True])
def test_mailbox_rejects_invalid_journal_mode(tmp_path, mode):
    with pytest.raises(ValueError, match="journal_mode"):
        SessionMessageStore(tmp_path / "messages.sqlite3", journal_mode=mode)


def test_unspecified_workspace_journal_preserves_existing_default():
    assert workspace_sqlite_journal_mode({}) is None
