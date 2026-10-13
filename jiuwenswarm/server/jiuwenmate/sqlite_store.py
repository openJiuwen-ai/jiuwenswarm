"""Private SQLite file shared by the JiuwenMate repositories."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path


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

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
