"""Bounded per-process execution, operator-owned capacity and cancellation."""

from __future__ import annotations

from collections import deque
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
import os
import threading
import time


class SelectionBusy(RuntimeError):
    """Capacity exhausted: degrade or return a retryable busy result."""


class SelectionTimedOut(TimeoutError):
    """Request deadline expired or caller cancelled it."""


@dataclass
class WorkBudget:
    deadline: float
    cancelled: threading.Event

    def check(self):
        if self.cancelled.is_set() or time.monotonic() >= self.deadline:
            raise SelectionTimedOut("Skill selection deadline exceeded or cancelled")


budget: ContextVar[WorkBudget | None] = ContextVar(
    "flash_selection_budget", default=None
)
background: ContextVar[bool] = ContextVar("flash_selection_background", default=False)
build_gate: ContextVar[StartupGate | None] = ContextVar(
    "flash_selection_build_gate", default=None
)


class StartupLease:
    """An idempotent lease; the model boundary and finally may both close it."""

    def __init__(self, gate):
        self._gate = gate
        self._closed = False
        self._close_lock = threading.Lock()

    def close(self):
        with self._close_lock:
            if not self._closed:
                self._gate.release_startup()
                self._closed = True


class StartupGate:
    """Let speculative builds yield to Flash preparation, without starvation.

    This is cooperative scheduling, not thread cancellation or an I/O barrier.
    A worker already reading/parsing one item finishes that item before yielding.
    Every checkpoint has a bounded wait; an actual search waiting for an index
    bypasses the gate so another session cannot indefinitely block that search.
    """

    def __init__(self, *, max_pause_s=1.0):
        if max_pause_s < 0:
            raise ValueError("max_pause_s must be non-negative")
        self.max_pause_s = max_pause_s
        self._condition = threading.Condition()
        self._startups = self._demands = 0
        self._closed = False

    def startup(self):
        with self._condition:
            self._startups += 1
        return StartupLease(self)

    def release_startup(self):
        """Release one lease and wake builders under the gate's own lock."""
        with self._condition:
            self._startups -= 1
            self._condition.notify_all()

    @contextmanager
    def demand(self):
        with self._condition:
            self._demands += 1
            self._condition.notify_all()
        try:
            yield
        finally:
            with self._condition:
                self._demands -= 1
                self._condition.notify_all()

    def checkpoint(self):
        deadline = time.monotonic() + self.max_pause_s
        with self._condition:
            while self._startups and not self._demands and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def stats(self):
        with self._condition:
            return {"startups": self._startups, "demands": self._demands,
                    "closed": self._closed}


def checkpoint():
    current = budget.get()
    if current is not None:
        current.check()
    gate = build_gate.get()
    if gate is not None:
        gate.checkpoint()
    if current is not None:
        current.check()


class BoundedExecutor:
    """Fixed workers and a physically bounded queue with eager cancellation removal.

    ThreadPoolExecutor otherwise retains cancelled work items until a worker drains
    its unbounded queue. Idle workers here also release references to completed work.
    """

    def __init__(self, workers: int, queue_size: int, name: str):
        self.workers, self.queue_size, self.name = workers, queue_size, name
        self._condition = threading.Condition()
        self._queue = deque()
        self._threads: list[threading.Thread] = []
        self._closed = False
        self._active = self._rejected = self._completed = 0

    def submit(self, fn, *args, **kwargs) -> Future:
        context = copy_context()
        future = Future()
        item = (future, context, fn, args, kwargs)
        with self._condition:
            if (
                self._closed
                or len(self._queue) + self._active >= self.workers + self.queue_size
            ):
                self._rejected += 1
                raise SelectionBusy(f"{self.name} capacity exhausted")
            self._queue.append(item)
            if len(self._threads) < self.workers:
                thread = threading.Thread(
                    target=self._run,
                    name=f"{self.name}-{len(self._threads)}",
                    daemon=True,
                )
                self._threads.append(thread)
                thread.start()
            self._condition.notify()

        holder = [item]

        def remove_cancelled(completed):
            queued_item = holder.pop() if holder else None
            if completed.cancelled():
                with self._condition:
                    try:
                        self._queue.remove(queued_item)
                    except ValueError:
                        pass
                    self._condition.notify_all()

        future.add_done_callback(remove_cancelled)
        return future

    def _run(self):
        while True:
            with self._condition:
                while not self._queue and not self._closed:
                    self._condition.wait()
                if not self._queue:
                    return
                future, context, fn, args, kwargs = self._queue.popleft()
                if not future.set_running_or_notify_cancel():
                    del future, context, fn, args, kwargs
                    continue
                self._active += 1
            result = error = None
            try:
                result = context.run(fn, *args, **kwargs)
            except BaseException as exc:
                error = exc
            with self._condition:
                self._active -= 1
                self._completed += 1
            if error is None:
                future.set_result(result)
            else:
                future.set_exception(error)
            del future, context, fn, args, kwargs, result, error

    def stats(self):
        with self._condition:
            return {
                "active": self._active,
                "queued": len(self._queue),
                "rejected": self._rejected,
                "completed": self._completed,
                "capacity": self.workers + self.queue_size,
            }

    def shutdown(self, *, wait=True):
        with self._condition:
            self._closed = True
            queued = list(self._queue)
            self._queue.clear()
            self._condition.notify_all()
        for future, *_ in queued:
            future.cancel()
        if wait:
            for thread in self._threads:
                thread.join()


def _limit(name: str, default: int, maximum: int) -> int:
    value = int(os.getenv("FLASH_SKILL_" + name, str(default)))
    if not 1 <= value <= maximum:
        raise ValueError(f"FLASH_SKILL_{name} must be in [1, {maximum}]")
    return value


class SelectionRuntime:
    def __init__(
        self,
        *,
        query_workers=2,
        query_queue=16,
        build_workers=1,
        build_queue=8,
        max_requests=64,
        max_catalogs=16,
        gate=None,
    ):
        self.gate = gate if gate is not None else StartupGate()
        self.queries = BoundedExecutor(
            query_workers, query_queue, "flash-skill-query"
        )
        self.builds = BoundedExecutor(build_workers, build_queue, "flash-skill-build")
        self.max_catalogs = max_catalogs
        self._admission = threading.BoundedSemaphore(max_requests)
        self._lock = threading.Lock()
        self._requests = self._rejected = self._timeouts = 0

    @contextmanager
    def request(self):
        if not self._admission.acquire(blocking=False):
            with self._lock:
                self._rejected += 1
            raise SelectionBusy("Skill selection request capacity exhausted")
        with self._lock:
            self._requests += 1
        try:
            yield
        finally:
            with self._lock:
                self._requests -= 1
            self._admission.release()

    def timeout(self):
        with self._lock:
            self._timeouts += 1

    def stats(self):
        with self._lock:
            return {
                "requests": self._requests,
                "admission_rejected": self._rejected,
                "timeouts": self._timeouts,
                "queries": self.queries.stats(),
                "builds": self.builds.stats(),
            }

    def shutdown(self):
        # A build can be paused at a checkpoint while shutdown joins workers.
        self.gate.close()
        self.queries.shutdown()
        self.builds.shutdown()


_runtime = None
_runtime_lock = threading.Lock()
_startup_gate = StartupGate()


def get_startup_gate():
    """The Flash entry needs no executor/runtime when selection is disabled."""
    return _runtime.gate if _runtime is not None else _startup_gate


def get_runtime() -> SelectionRuntime:
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = SelectionRuntime(
                gate=_startup_gate,
                query_workers=_limit("QUERY_WORKERS", 2, 32),
                query_queue=_limit("QUERY_QUEUE", 16, 1024),
                build_workers=_limit("BUILD_WORKERS", 1, 4),
                build_queue=_limit("BUILD_QUEUE", 8, 128),
                max_requests=_limit("MAX_REQUESTS", 64, 4096),
                max_catalogs=_limit("MAX_CATALOGS", 16, 256),
            )
        return _runtime
