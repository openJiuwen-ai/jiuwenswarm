"""Private SQLite file shared by the JiuwenMate repositories."""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path


_read_batches = threading.local()


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


class SQLiteStore:
    """Creates its tables on open; every multi-row change runs in one immediate transaction."""

    _schema = ""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        private_dir(self._db_path.parent)
        with closing(self._connect()) as conn:
            conn.executescript(self._schema)
        os.chmod(self._db_path, 0o600)

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def read_batch(self) -> Iterator[None]:
        """Reuse one read-only connection on this thread without holding an idle snapshot."""
        connections = getattr(_read_batches, "connections", None)
        if connections is None:
            connections = _read_batches.connections = {}
        key = self._db_path.resolve()
        if key in connections:
            yield
            return
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA query_only = ON")
            connections[key] = conn
            try:
                yield
            finally:
                del connections[key]

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        cached = None if write else getattr(_read_batches, "connections", {}).get(self._db_path.resolve())
        with (nullcontext(cached) if cached is not None else closing(self._connect())) as conn:
            if cached is not None and conn.in_transaction:
                yield conn
                return
            if not write and cached is None:
                conn.execute("PRAGMA query_only = ON")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
