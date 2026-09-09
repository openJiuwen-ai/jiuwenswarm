"""Bounded registry of queued, running, and recently completed executions."""

from __future__ import annotations

import time
from collections import deque
from typing import Iterable

from jiuwenswarm.runtime.session.model import (
    SessionExecutionHandle,
    SessionExecutionSnapshot,
    SessionExecutionState,
)


class SessionExecutionRegistry:
    """Indexes executions without becoming the durable Session repository."""

    def __init__(self, *, terminal_capacity: int = 512, terminal_ttl: float = 900.0):
        if terminal_capacity < 0:
            raise ValueError("terminal_capacity must be non-negative")
        self._terminal_capacity = terminal_capacity
        self._terminal_ttl = max(0.0, terminal_ttl)
        self._executions: dict[str, SessionExecutionHandle] = {}
        self._by_session: dict[str, set[str]] = {}
        self._by_request: dict[tuple[str, str], set[str]] = {}
        self._terminal: deque[str] = deque()

    def register(self, handle: SessionExecutionHandle) -> None:
        self._evict_terminal()
        if handle.execution_id in self._executions:
            raise ValueError(f"duplicate execution_id: {handle.execution_id}")
        self._executions[handle.execution_id] = handle
        self._by_session.setdefault(handle.session_id, set()).add(handle.execution_id)
        self._by_request.setdefault(
            (handle.session_id, handle.request_id), set()
        ).add(handle.execution_id)

    @staticmethod
    def mark_running(handle: SessionExecutionHandle) -> None:
        if handle.state is not SessionExecutionState.QUEUED:
            return
        handle.state = SessionExecutionState.RUNNING
        handle.started_at = time.monotonic()

    @staticmethod
    def mark_awaiting_control(
        handle: SessionExecutionHandle, control_id: str
    ) -> None:
        if not handle.state.terminal:
            handle.waiting_control_id = control_id

    @staticmethod
    def mark_waiting(handle: SessionExecutionHandle) -> None:
        if handle.state.terminal:
            return
        handle.state = SessionExecutionState.WAITING_FOR_CONTROL
        handle.task = None

    @staticmethod
    def resume_waiting(handle: SessionExecutionHandle) -> None:
        if handle.state is SessionExecutionState.WAITING_FOR_CONTROL:
            handle.state = SessionExecutionState.RUNNING
        handle.waiting_control_id = None

    def mark_terminal(
        self,
        handle: SessionExecutionHandle,
        state: SessionExecutionState,
        *,
        error: BaseException | str | None = None,
    ) -> None:
        if not state.terminal:
            raise ValueError("terminal state required")
        if handle.state.terminal:
            return
        handle.state = state
        handle.finished_at = time.monotonic()
        if error is not None:
            handle.error = str(error)
        handle.task = None
        self._terminal.append(handle.execution_id)
        self._evict_terminal()

    def get(self, execution_id: str) -> SessionExecutionHandle | None:
        self._evict_terminal()
        return self._executions.get(execution_id)

    def select(
        self,
        *,
        session_id: str,
        request_id: str | None = None,
        execution_id: str | None = None,
        generation: int | None = None,
        active_only: bool = False,
    ) -> list[SessionExecutionHandle]:
        self._evict_terminal()
        if execution_id is not None:
            candidate_ids: Iterable[str] = (execution_id,)
        elif request_id is not None:
            candidate_ids = tuple(self._by_request.get((session_id, request_id), ()))
        else:
            candidate_ids = tuple(self._by_session.get(session_id, ()))
        selected: list[SessionExecutionHandle] = []
        for candidate_id in candidate_ids:
            handle = self._executions.get(candidate_id)
            if handle is None or handle.session_id != session_id:
                continue
            if generation is not None and handle.generation != generation:
                continue
            if active_only and handle.state.terminal:
                continue
            selected.append(handle)
        return sorted(selected, key=lambda item: item.created_at)

    def snapshots_for_session(
        self, session_id: str, *, generation: int | None = None
    ) -> tuple[SessionExecutionSnapshot, ...]:
        return tuple(
            handle.snapshot()
            for handle in self.select(session_id=session_id, generation=generation)
        )

    def active(self) -> tuple[SessionExecutionHandle, ...]:
        self._evict_terminal()
        return tuple(
            handle for handle in self._executions.values() if not handle.state.terminal
        )

    def _evict_terminal(self) -> None:
        now = time.monotonic()
        while self._terminal:
            execution_id = self._terminal[0]
            handle = self._executions.get(execution_id)
            expired = (
                handle is None
                or handle.finished_at is None
                or now - handle.finished_at > self._terminal_ttl
            )
            over_capacity = len(self._terminal) > self._terminal_capacity
            if not expired and not over_capacity:
                break
            self._terminal.popleft()
            if handle is not None and handle.state.terminal:
                self._remove(handle)

    def _remove(self, handle: SessionExecutionHandle) -> None:
        self._executions.pop(handle.execution_id, None)
        session_ids = self._by_session.get(handle.session_id)
        if session_ids is not None:
            session_ids.discard(handle.execution_id)
            if not session_ids:
                self._by_session.pop(handle.session_id, None)
        request_key = (handle.session_id, handle.request_id)
        request_ids = self._by_request.get(request_key)
        if request_ids is not None:
            request_ids.discard(handle.execution_id)
            if not request_ids:
                self._by_request.pop(request_key, None)
