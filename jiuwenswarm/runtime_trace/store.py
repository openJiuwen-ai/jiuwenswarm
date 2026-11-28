# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Run-scoped SQLite storage, lease ownership, and retention for runtime trace.

All heavy imports (sqlite3, portalocker) stay inside functions so importing
this module while the feature is disabled has no side effect.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import zlib
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_BUSY_TIMEOUT_MS = 5000
_SCHEMA_VERSION = 1
_PAYLOAD_COMPRESSION_LEVEL = 3
_MAINTENANCE_LOCK_NAME = ".maintenance.lock"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    run_id          TEXT PRIMARY KEY,
    role            TEXT NOT NULL,
    pid             INTEGER NOT NULL,
    started_at      INTEGER NOT NULL,
    last_write_at   INTEGER NOT NULL,
    closed_at       INTEGER,
    worker_instance TEXT,
    generation      INTEGER,
    schema_version  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS span_records (
    ingest_seq              INTEGER PRIMARY KEY,
    trace_id                TEXT NOT NULL,
    span_id                 TEXT NOT NULL,
    parent_span_id          TEXT,
    name                    TEXT NOT NULL DEFAULT '',
    outcome                 TEXT,
    start_ns                INTEGER NOT NULL,
    end_ns                  INTEGER NOT NULL,
    record_revision         INTEGER NOT NULL,
    observed_time_unix_nano INTEGER NOT NULL,
    raw_json                BLOB NOT NULL,
    created_at              INTEGER NOT NULL,
    UNIQUE(trace_id, span_id)
);
CREATE INDEX IF NOT EXISTS idx_span_trace ON span_records(trace_id, start_ns);
CREATE INDEX IF NOT EXISTS idx_span_end ON span_records(end_ns);

CREATE TABLE IF NOT EXISTS conflicts (
    trace_id   TEXT NOT NULL,
    span_id    TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(trace_id, span_id)
);
"""

_INSERT_SPAN_SQL = """
INSERT INTO span_records (
    trace_id, span_id, parent_span_id, name, outcome,
    start_ns, end_ns, record_revision, observed_time_unix_nano,
    raw_json, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(trace_id, span_id) DO NOTHING
"""

_INSERT_CONFLICT_SQL = """
INSERT OR IGNORE INTO conflicts (trace_id, span_id, created_at) VALUES (?, ?, ?)
"""

_DELETE_EXPIRED_SQL = """
DELETE FROM span_records
WHERE trace_id IN (
    SELECT trace_id
    FROM span_records
    GROUP BY trace_id
    HAVING MAX(end_ns) < ?
    LIMIT ?
)
"""


def sqlite_files(path: Path) -> tuple[Path, Path, Path]:
    """Return the main database and its WAL sidecars for *path*."""
    return path, Path(str(path) + "-wal"), Path(str(path) + "-shm")


def _open_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=_BUSY_TIMEOUT_MS / 1000.0)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


class RuntimeTraceStore:
    """Single-threaded writer for one process run."""

    def __init__(
        self,
        *,
        root: Path,
        role: str,
        run_id: str,
        pid: int,
        started_at_ns: int,
        retention_days: int,
    ) -> None:
        self.root = Path(root)
        self.role = role
        self.run_id = run_id
        self.pid = pid
        self.started_at_ns = started_at_ns
        self.retention_days = max(1, int(retention_days))
        self.role_dir = self.root / role
        self.database_path = self.role_dir / f"{run_id}.sqlite3"
        self.lease_path = self.role_dir / f"{run_id}.lease"
        self._conn: sqlite3.Connection | None = None
        self._lease: Any | None = None

    @property
    def path(self) -> Path:
        return self.database_path

    # -- lifecycle ------------------------------------------------------

    def open(self) -> None:
        """Create the run directory, take locks, and build the schema."""
        import portalocker

        from jiuwenswarm.runtime_trace.config import _BUILD_LOCK_TIMEOUT

        self.root.mkdir(parents=True, exist_ok=True)
        maintenance = portalocker.Lock(
            str(self.root / _MAINTENANCE_LOCK_NAME), timeout=_BUILD_LOCK_TIMEOUT
        )
        maintenance.acquire()
        try:
            self.role_dir.mkdir(parents=True, exist_ok=True)
            lease = portalocker.Lock(
                str(self.lease_path),
                mode="a",
                timeout=0,
                flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
            )
            lease.acquire()
            self._lease = lease
            self._conn = _open_connection(self.database_path)
            with self._conn:
                self._conn.executescript(_SCHEMA_SQL)
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO meta (
                        run_id, role, pid, started_at, last_write_at,
                        closed_at, worker_instance, generation, schema_version
                    ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?)
                    """,
                    (
                        self.run_id,
                        self.role,
                        self.pid,
                        self.started_at_ns,
                        self.started_at_ns,
                        _SCHEMA_VERSION,
                    ),
                )
        except Exception:
            self._release_lease()
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None
            raise
        finally:
            maintenance.release()

    def close(self, closed_at_ns: int) -> None:
        conn = self._conn
        if conn is not None:
            try:
                with conn:
                    conn.execute(
                        "UPDATE meta SET closed_at=?, last_write_at=? WHERE run_id=?",
                        (closed_at_ns, closed_at_ns, self.run_id),
                    )
            finally:
                conn.close()
                self._conn = None
        self._release_lease()

    def _release_lease(self) -> None:
        lease = self._lease
        self._lease = None
        if lease is None:
            return
        try:
            lease.release()
        except Exception:  # noqa: BLE001 - releasing a lease cannot fail business
            logger.debug("runtime trace: lease release failed", exc_info=True)

    # -- writes ---------------------------------------------------------

    def write_batch(self, items: list[Any]) -> dict[str, int]:
        """Compress and insert one bounded batch in a single transaction."""
        conn = self._conn
        if conn is None:
            raise RuntimeError("runtime trace store is not open")
        now_ns = time.time_ns()
        prepared: list[tuple[Any, bytes, Any]] = []
        dropped = 0
        missing = 0
        for pending in items:
            record = pending.record
            try:
                # The runtime schema has no content-addressed tables. A record
                # that references sequences cannot be stored faithfully.
                if record.sequences != ():
                    dropped += 1
                    continue
                raw = zlib.compress(record.raw_json, _PAYLOAD_COMPRESSION_LEVEL)
            except Exception:  # noqa: BLE001 - one bad record must not stop the batch
                dropped += 1
                continue
            prepared.append((record, raw, pending))
            if not pending.name or pending.outcome is None:
                missing += 1

        inserted = 0
        conflicts = 0
        with conn:
            for record, raw, pending in prepared:
                cursor = conn.execute(
                    _INSERT_SPAN_SQL,
                    (
                        record.trace_id,
                        record.span_id,
                        record.parent_span_id,
                        pending.name,
                        pending.outcome,
                        record.start_time_unix_nano,
                        record.end_time_unix_nano,
                        record.record_revision,
                        record.observed_time_unix_nano,
                        raw,
                        now_ns,
                    ),
                )
                if cursor.rowcount == 0:
                    conflicts += 1
                    conn.execute(
                        _INSERT_CONFLICT_SQL,
                        (record.trace_id, record.span_id, now_ns),
                    )
                else:
                    inserted += 1
            conn.execute(
                "UPDATE meta SET last_write_at=? WHERE run_id=?", (now_ns, self.run_id)
            )
        return {
            "inserted": inserted,
            "conflicts": conflicts,
            "dropped": dropped,
            "missing": missing,
        }

    def update_worker_meta(
        self, instance: str | None, generation: int | None
    ) -> None:
        conn = self._conn
        if conn is None:
            return
        with conn:
            if instance is not None:
                conn.execute(
                    "UPDATE meta SET worker_instance=? WHERE run_id=?",
                    (instance, self.run_id),
                )
            if generation is not None:
                conn.execute(
                    "UPDATE meta SET generation=? WHERE run_id=?",
                    (generation, self.run_id),
                )

    def heartbeat(self, now_ns: int) -> None:
        conn = self._conn
        if conn is None:
            return
        with conn:
            conn.execute(
                "UPDATE meta SET last_write_at=? WHERE run_id=?", (now_ns, self.run_id)
            )

    def sweep(self, cutoff_ns: int, *, batch_traces: int) -> int:
        """Delete whole traces whose latest span is older than *cutoff_ns*."""
        conn = self._conn
        if conn is None:
            return 0
        deleted = 0
        while True:
            with conn:
                cursor = conn.execute(_DELETE_EXPIRED_SQL, (cutoff_ns, batch_traces))
                changes = max(0, cursor.rowcount)
                conn.execute(
                    "UPDATE meta SET last_write_at=? WHERE run_id=?",
                    (time.time_ns(), self.run_id),
                )
            deleted += changes
            if changes == 0:
                break
        return deleted


def reclaim_history(
    root: Path,
    role: str,
    retention_days: int,
    *,
    exclude_run_id: str,
) -> int:
    """Remove expired databases owned by *role*; return the number reclaimed."""
    import portalocker

    from jiuwenswarm.runtime_trace.config import _MAINTENANCE_LOCK_TIMEOUT

    root = Path(root)
    role_dir = root / role
    if not role_dir.is_dir():
        return 0
    try:
        maintenance = portalocker.Lock(
            str(root / _MAINTENANCE_LOCK_NAME), timeout=_MAINTENANCE_LOCK_TIMEOUT
        )
        maintenance.acquire()
    except Exception:  # noqa: BLE001 - another process is sweeping; skip this round
        return 0

    removed = 0
    try:
        cutoff_ns = time.time_ns() - max(1, int(retention_days)) * 86400 * 10**9
        for database in list(role_dir.glob("*.sqlite3")):
            if database.stem == exclude_run_id:
                continue
            try:
                removed += _reclaim_one(database, cutoff_ns)
            except Exception:  # noqa: BLE001 - a damaged database is skipped
                logger.warning(
                    "runtime trace: reclaim failed for %s", database.name,
                    exc_info=True,
                )
    finally:
        maintenance.release()
    return removed


def _reclaim_one(database: Path, cutoff_ns: int) -> int:
    import portalocker

    lease_path = database.with_suffix(".lease")
    try:
        lease = portalocker.Lock(
            str(lease_path),
            mode="a",
            timeout=0,
            flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
        )
        lease.acquire()
    except Exception:  # noqa: BLE001 - a live writer owns this database
        return 0

    try:
        try:
            conn = sqlite3.connect(str(database), timeout=_BUSY_TIMEOUT_MS / 1000.0)
        except Exception:  # noqa: BLE001
            return 0
        expired = False
        try:
            try:
                row = conn.execute(
                    "SELECT closed_at, last_write_at FROM meta"
                ).fetchone()
            except Exception:  # noqa: BLE001 - damaged database: skip this round
                logger.warning(
                    "runtime trace: unreadable database %s; skipping", database.name
                )
                return 0

            if row is None:
                return 0
            closed_at, last_write_at = row[0], row[1]
            expired = (
                closed_at is not None and closed_at < cutoff_ns
            ) or (
                closed_at is None
                and last_write_at is not None
                and last_write_at < cutoff_ns
            )
            if not expired:
                return 0

            # A reclaim opens read-write: TRUNCATE checkpoint needs a writable
            # connection. It must finish before any file is deleted so Windows
            # releases its handle.
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:  # noqa: BLE001 - best effort before delete
                logger.debug(
                    "runtime trace: checkpoint failed for %s", database.name,
                    exc_info=True,
                )
        finally:
            conn.close()

        if not expired:
            return 0

        main, wal, shm = sqlite_files(database)
        try:
            for sidecar in (wal, shm):
                if sidecar.exists():
                    sidecar.unlink()
            main.unlink()
        except OSError:
            # Never leave a main database whose WAL was already removed when
            # deletion is partially blocked; retry next round.
            logger.warning(
                "runtime trace: could not delete %s; will retry", database.name
            )
            return 0
        return 1
    finally:
        try:
            lease.release()
        except Exception:  # noqa: BLE001
            logger.debug("runtime trace: reclaim lease release failed", exc_info=True)
