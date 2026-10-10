# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cron run ledger (issue #5018, design §3.3.2).

A small standalone SQLite database (``cron_run_ledger.db``, next to the
checkpoint dir by default) that records the lifecycle of every cron run:
``running → finished | failed | tripped``, plus ``orphaned`` for rows left
``running`` by a previous process.

Write failures must never block a run: every mutation is best-effort with a
warning, and the in-memory budget in :class:`RunBudget` remains authoritative
for the live run.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from jiuwenswarm.common.utils import logger

STATE_RUNNING = "running"
STATE_FINISHED = "finished"
STATE_FAILED = "failed"
STATE_TRIPPED = "tripped"
STATE_ORPHANED = "orphaned"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cron_runs (
    run_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    sid TEXT NOT NULL,
    state TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    trip_reason TEXT,
    shell_seconds REAL DEFAULT 0,
    sleep_seconds REAL DEFAULT 0,
    sleep_calls INTEGER DEFAULT 0,
    model_calls INTEGER DEFAULT 0,
    reaped INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cron_runs_sid ON cron_runs (sid);
CREATE INDEX IF NOT EXISTS idx_cron_runs_state ON cron_runs (state);
"""


def default_ledger_path() -> Path:
    """Ledger lives next to the checkpoint db (``~/.jiuwenswarm/agent/.checkpoint/``)."""
    base = os.environ.get("JIUWENSWARM_HOME") or os.path.join(os.path.expanduser("~"), ".jiuwenswarm")
    return Path(base) / "agent" / ".checkpoint" / "cron_run_ledger.db"


class CronRunLedger:
    """Thread-safe, fail-open ledger over SQLite."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._path = Path(path) if path else default_ledger_path()
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._degraded = False

    @property
    def degraded(self) -> bool:
        return self._degraded

    def _connect(self) -> sqlite3.Connection | None:
        if self._conn is not None:
            return self._conn
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._path), timeout=2.0, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
            self._conn = conn
            self._degraded = False
            return conn
        except Exception as exc:  # noqa: BLE001 — ledger failure degrades, never blocks
            logger.warning("[cron_guard] ledger unavailable at %s: %s (degraded: memory-only)", self._path, exc)
            self._degraded = True
            return None

    # -- mutations ---------------------------------------------------------

    def start_run(self, run_id: str, job_id: str, sid: str) -> None:
        conn = self._connect()
        if conn is None:
            return
        try:
            with self._lock:
                conn.execute(
                    "INSERT OR REPLACE INTO cron_runs (run_id, job_id, sid, state, started_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (run_id, job_id, sid, STATE_RUNNING, time.time()),
                )
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[cron_guard] ledger start_run(%s) failed: %s", run_id, exc)

    def update_budget(self, run_id: str, snapshot: dict[str, Any]) -> None:
        conn = self._connect()
        if conn is None:
            return
        try:
            with self._lock:
                conn.execute(
                    "UPDATE cron_runs SET shell_seconds=?, sleep_seconds=?, sleep_calls=?, model_calls=?"
                    " WHERE run_id=?",
                    (
                        float(snapshot.get("shell_seconds", 0.0)),
                        float(snapshot.get("sleep_seconds", 0.0)),
                        int(snapshot.get("sleep_calls", 0)),
                        int(snapshot.get("model_calls", 0)),
                        run_id,
                    ),
                )
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[cron_guard] ledger update_budget(%s) failed: %s", run_id, exc)

    def finish_run(
        self,
        run_id: str,
        state: str,
        trip_reason: str | None = None,
        reaped: bool = False,
        snapshot: dict[str, Any] | None = None,
    ) -> None:
        conn = self._connect()
        if conn is None:
            return
        try:
            snap = snapshot or {}
            with self._lock:
                cur = conn.execute(
                    "UPDATE cron_runs SET state=?, ended_at=?, trip_reason=?, reaped=?,"
                    " shell_seconds=?, sleep_seconds=?, sleep_calls=?, model_calls=?"
                    " WHERE run_id=?",
                    (
                        state,
                        time.time(),
                        trip_reason,
                        1 if reaped else 0,
                        float(snap.get("shell_seconds", 0.0)),
                        float(snap.get("sleep_seconds", 0.0)),
                        int(snap.get("sleep_calls", 0)),
                        int(snap.get("model_calls", 0)),
                        run_id,
                    ),
                )
                if cur.rowcount == 0:
                    # Run row missing (start write failed earlier) — insert terminal row.
                    conn.execute(
                        "INSERT OR REPLACE INTO cron_runs (run_id, job_id, sid, state, started_at,"
                        " ended_at, trip_reason, reaped) VALUES (?, '', '', ?, ?, ?, ?, ?)",
                        (run_id, state, time.time(), trip_reason, 1 if reaped else 0),
                    )
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[cron_guard] ledger finish_run(%s) failed: %s", run_id, exc)

    def mark_orphans(self) -> int:
        """At startup: every leftover ``running`` row becomes ``orphaned``.

        Returns the number of rows transitioned (0 when degraded).
        """
        conn = self._connect()
        if conn is None:
            return 0
        try:
            with self._lock:
                cur = conn.execute(
                    "UPDATE cron_runs SET state=?, ended_at=?, trip_reason=? WHERE state=?",
                    (STATE_ORPHANED, time.time(), "process_restart", STATE_RUNNING),
                )
                conn.commit()
                return cur.rowcount
        except Exception as exc:  # noqa: BLE001
            logger.warning("[cron_guard] ledger mark_orphans failed: %s", exc)
            return 0

    # -- queries -----------------------------------------------------------

    def lookup_run(self, run_id: str) -> Optional[dict[str, Any]]:
        return self._query_one("SELECT * FROM cron_runs WHERE run_id=?", (run_id,))

    def latest_for_sid(self, sid: str, exclude_run_id: str | None = None) -> Optional[dict[str, Any]]:
        if exclude_run_id:
            return self._query_one(
                "SELECT * FROM cron_runs WHERE sid=? AND run_id!=? ORDER BY started_at DESC LIMIT 1",
                (sid, exclude_run_id),
            )
        return self._query_one(
            "SELECT * FROM cron_runs WHERE sid=? ORDER BY started_at DESC LIMIT 1",
            (sid,),
        )

    def _query_one(self, sql: str, params: tuple) -> Optional[dict[str, Any]]:
        conn = self._connect()
        if conn is None:
            return None
        try:
            with self._lock:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(sql, params)
                row = cur.fetchone()
                return dict(row) if row is not None else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("[cron_guard] ledger query failed: %s", exc)
            return None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    pass
                self._conn = None


_ledger: CronRunLedger | None = None
_ledger_lock = threading.Lock()


def get_run_ledger(path: str | os.PathLike[str] | None = None) -> CronRunLedger:
    """Process-wide ledger accessor (lazy singleton)."""
    global _ledger
    with _ledger_lock:
        if _ledger is None:
            cfg_path = None
            if path is None:
                try:
                    from .config import get_cron_guard_config

                    cfg_path = (get_cron_guard_config().get("budget_ledger") or {}).get("path")
                except Exception:  # noqa: BLE001
                    cfg_path = None
            _ledger = CronRunLedger(path or cfg_path)
        return _ledger


def sync_budget_to_ledger(ctx: Any) -> None:
    """Best-effort write of a run's budget snapshot to the ledger (never raises).

    Single shared helper for the sleep guard and the budget rail so both stay
    in step (run_id + snapshot format).
    """
    try:
        get_run_ledger().update_budget(ctx.run_id, ctx.budget.snapshot())
    except Exception as exc:  # noqa: BLE001
        logger.debug("[cron_guard] ledger sync failed: %s", exc)


def reset_run_ledger() -> None:
    """Testing hook: drop the singleton."""
    global _ledger
    with _ledger_lock:
        if _ledger is not None:
            _ledger.close()
        _ledger = None
