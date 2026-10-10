# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Process-local trajectory lifecycle for product Session deletion."""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from jiuwenswarm.observability.config import (
    database_files,
    load_trajectory_store_settings,
    session_database_path,
)

logger = logging.getLogger(__name__)

# Committed tombstones kept to refuse late records for deleted sessions. Late
# records arrive within seconds of a deletion, so only the most recent
# deletions need one; a prepared deletion is never evicted.
_MAX_COMMITTED_TOMBSTONES = 1024


@runtime_checkable
class TrajectorySessionDeleteBackend(Protocol):
    """Storage backend capable of deleting one Session atomically."""

    def begin_session_delete(self, session_id: str) -> None:
        """Drain the Session writer after the process tombstone is installed."""
        ...

    def abort_session_delete(self, session_id: str) -> None:
        """Restore the Session writer after product deletion fails."""
        ...

    def commit_session_delete(self, session_id: str) -> None:
        """Close and delete all database files owned by the Session."""
        ...


class _DeleteState(str, Enum):
    PREPARED = "prepared"
    COMMITTED = "committed"


class TrajectorySessionDeleteLifecycle:
    """Coordinate Session tombstones with an optional routed store backend.

    The process tombstone is installed before the backend is asked to drain.
    Consequently records arriving during or after deletion cannot reopen a
    Session database. An abort always removes that tombstone, including when
    backend rollback itself reports an error.

    Without a backend -- the trajectory runtime starts lazily on the first
    request and is absent when the UI store is disabled -- a commit still
    deletes the Session's database files, which exist from earlier runs.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._backend: TrajectorySessionDeleteBackend | None = None
        self._database_root: Path | None = None
        self._states: OrderedDict[str, _DeleteState] = OrderedDict()
        # Per-session operation lock plus the number of operations holding or
        # waiting for it, so the entry goes away once nobody needs it.
        self._operation_locks: dict[str, tuple[threading.Lock, int]] = {}

    def set_backend(self, backend: TrajectorySessionDeleteBackend | None) -> None:
        """Replace the active routed-store backend without clearing tombstones."""
        with self._lock:
            self._backend = backend

    def set_database_root(self, database_root: Path | None) -> None:
        """Remember where session databases live for backend-less deletion.

        Kept across runtime shutdown on purpose: the files outlive the runtime.
        """
        with self._lock:
            self._database_root = None if database_root is None else Path(database_root)

    def begin(self, session_id: str) -> None:
        """Install a tombstone and synchronously drain the Session backend."""
        resolved = _require_session_id(session_id)
        with self._operation(resolved):
            with self._lock:
                state = self._states.get(resolved)
                if state is not None:
                    return
                self._states[resolved] = _DeleteState.PREPARED
                backend = self._backend
            try:
                if backend is not None:
                    backend.begin_session_delete(resolved)
            except Exception:
                with self._lock:
                    self._states.pop(resolved, None)
                raise

    def abort(self, session_id: str) -> None:
        """Roll back a prepared deletion and allow the Session to write again."""
        resolved = _require_session_id(session_id)
        with self._operation(resolved):
            with self._lock:
                if self._states.get(resolved) is not _DeleteState.PREPARED:
                    return
                backend = self._backend
            try:
                if backend is not None:
                    backend.abort_session_delete(resolved)
            finally:
                with self._lock:
                    self._states.pop(resolved, None)

    def commit(self, session_id: str) -> None:
        """Delete Session storage and retain the tombstone after success."""
        resolved = _require_session_id(session_id)
        with self._operation(resolved):
            with self._lock:
                if self._states.get(resolved) is _DeleteState.COMMITTED:
                    return
                self._states[resolved] = _DeleteState.PREPARED
                backend = self._backend
                database_root = self._database_root
            if backend is not None:
                backend.commit_session_delete(resolved)
            else:
                _delete_session_database(resolved, database_root)
            with self._lock:
                self._states[resolved] = _DeleteState.COMMITTED
                self._states.move_to_end(resolved)
                self._evict_committed_tombstones()

    def accepts_records(self, session_id: str) -> bool:
        """Return whether new records may be routed for this Session."""
        resolved = str(session_id or "").strip()
        if not resolved:
            return False
        with self._lock:
            return resolved not in self._states

    @contextmanager
    def _operation(self, session_id: str) -> Iterator[None]:
        with self._lock:
            operation_lock, users = self._operation_locks.get(
                session_id, (threading.Lock(), 0)
            )
            self._operation_locks[session_id] = (operation_lock, users + 1)
        try:
            with operation_lock:
                yield
        finally:
            with self._lock:
                _lock, users = self._operation_locks[session_id]
                if users <= 1:
                    self._operation_locks.pop(session_id)
                else:
                    self._operation_locks[session_id] = (operation_lock, users - 1)

    def _evict_committed_tombstones(self) -> None:
        committed = [
            session_id
            for session_id, state in self._states.items()
            if state is _DeleteState.COMMITTED
        ]
        for session_id in committed[: max(0, len(committed) - _MAX_COMMITTED_TOMBSTONES)]:
            self._states.pop(session_id)


def _delete_session_database(session_id: str, database_root: Path | None) -> None:
    root = database_root
    if root is None:
        root = load_trajectory_store_settings().database_path
    for candidate in database_files(session_database_path(root, session_id)):
        candidate.unlink(missing_ok=True)


def _require_session_id(session_id: str) -> str:
    resolved = str(session_id or "").strip()
    if not resolved:
        raise ValueError("session_id is required")
    return resolved


trajectory_session_delete_lifecycle = TrajectorySessionDeleteLifecycle()


def set_trajectory_session_delete_backend(
    backend: TrajectorySessionDeleteBackend | None,
) -> None:
    """Attach the active routed trajectory store to Session deletion."""
    trajectory_session_delete_lifecycle.set_backend(backend)


def set_trajectory_session_database_root(database_root: Path | None) -> None:
    """Record where session databases live for deletion without a backend."""
    trajectory_session_delete_lifecycle.set_database_root(database_root)


def begin_trajectory_session_delete(session_id: str) -> None:
    """Prepare deletion of one Session's trajectory store."""
    trajectory_session_delete_lifecycle.begin(session_id)


def abort_trajectory_session_delete(session_id: str) -> None:
    """Abort deletion of one Session's trajectory store."""
    trajectory_session_delete_lifecycle.abort(session_id)


def commit_trajectory_session_delete(session_id: str) -> None:
    """Commit deletion of one Session's trajectory store."""
    trajectory_session_delete_lifecycle.commit(session_id)


def trajectory_session_accepts_records(session_id: str) -> bool:
    """Return whether the Session is not tombstoned for deletion."""
    return trajectory_session_delete_lifecycle.accepts_records(session_id)


__all__ = [
    "TrajectorySessionDeleteBackend",
    "TrajectorySessionDeleteLifecycle",
    "abort_trajectory_session_delete",
    "begin_trajectory_session_delete",
    "commit_trajectory_session_delete",
    "set_trajectory_session_database_root",
    "set_trajectory_session_delete_backend",
    "trajectory_session_accepts_records",
]
