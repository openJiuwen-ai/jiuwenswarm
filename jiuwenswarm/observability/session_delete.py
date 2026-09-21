# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Process-local trajectory lifecycle for product Session deletion."""

from __future__ import annotations

import threading
from enum import Enum
from typing import Protocol, runtime_checkable


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
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._backend: TrajectorySessionDeleteBackend | None = None
        self._states: dict[str, _DeleteState] = {}
        self._operation_locks: dict[str, threading.Lock] = {}

    def set_backend(self, backend: TrajectorySessionDeleteBackend | None) -> None:
        """Replace the active routed-store backend without clearing tombstones."""
        with self._lock:
            self._backend = backend

    def begin(self, session_id: str) -> None:
        """Install a tombstone and synchronously drain the Session backend."""
        resolved = _require_session_id(session_id)
        operation_lock = self._operation_lock(resolved)
        with operation_lock:
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
        operation_lock = self._operation_lock(resolved)
        with operation_lock:
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
        operation_lock = self._operation_lock(resolved)
        with operation_lock:
            with self._lock:
                if self._states.get(resolved) is _DeleteState.COMMITTED:
                    return
                self._states[resolved] = _DeleteState.PREPARED
                backend = self._backend
            if backend is not None:
                backend.commit_session_delete(resolved)
            with self._lock:
                self._states[resolved] = _DeleteState.COMMITTED

    def accepts_records(self, session_id: str) -> bool:
        """Return whether new records may be routed for this Session."""
        resolved = str(session_id or "").strip()
        if not resolved:
            return False
        with self._lock:
            return resolved not in self._states

    def _operation_lock(self, session_id: str) -> threading.Lock:
        with self._lock:
            operation_lock = self._operation_locks.get(session_id)
            if operation_lock is None:
                operation_lock = threading.Lock()
                self._operation_locks[session_id] = operation_lock
            return operation_lock


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
    "set_trajectory_session_delete_backend",
    "trajectory_session_accepts_records",
]
