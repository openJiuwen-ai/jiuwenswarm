# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded, level-aware writer thread for runtime trace records."""

from __future__ import annotations

import logging
import queue
import threading
import time

from jiuwenswarm.runtime_trace.config import (
    _CRITICAL_QUEUE_SIZE,
    _NORMAL_QUEUE_SIZE,
    _RETENTION_BATCH_TRACES,
    _RETENTION_SWEEP_SECONDS,
)
from jiuwenswarm.runtime_trace.consumer import _Pending
from jiuwenswarm.runtime_trace.store import RuntimeTraceStore, reclaim_history

logger = logging.getLogger(__name__)

# Terminals are evicted in this order when a channel is full: the exit code
# (worker.exited) is the evidence V-03 depends on, so it goes last.
_TERMINAL_EVICT_ORDER = (
    "worker.failed",
    "worker.unresponsive",
    "worker.ipc_lost",
    "worker.exited",
)

_TERMINAL_TAKE = 32
_EVENT_TAKE = 32
_NORMAL_TAKE = 64

_STATS_KEYS = (
    "accepted",
    "committed",
    "dropped",
    "evicted",
    "conflicts",
    "failed",
    "missing_meta",
    "rejected",
    "build_failed",
)


class RuntimeTraceWriter(threading.Thread):
    """Drain records and write them from one background thread.

    ``offer`` is called from ``SpanProcessor.on_end`` on a business thread, so
    it never encodes, compresses, or touches SQLite. Every channel is bounded;
    the root channel is the only one without a cap because a process has at
    most two roots.
    """

    def __init__(self, store: RuntimeTraceStore, *, retention_days: int) -> None:
        super().__init__(name=f"runtime-trace-writer-{store.role}", daemon=True)
        self._store = store
        self._retention_days = max(1, int(retention_days))
        self._normal: queue.Queue[_Pending] = queue.Queue(maxsize=_NORMAL_QUEUE_SIZE)
        self._critical_roots: list[_Pending] = []
        self._critical_terminal: list[_Pending] = []
        self._critical_events: list[_Pending] = []
        self._state_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._stats = {key: 0 for key in _STATS_KEYS}
        self._accepting = True
        self._stop_requested = threading.Event()
        self._work_available = threading.Event()
        self._meta_lock = threading.Lock()
        self._pending_instance: str | None = None
        self._pending_generation: int | None = None
        self._meta_dirty = False

    # -- public surface -------------------------------------------------

    def offer(self, item: _Pending, *, level: str) -> None:
        with self._state_lock:
            if not self._accepting:
                self._bump("rejected")
                return
            if level == "root":
                self._critical_roots.append(item)
            elif level == "terminal":
                self._offer_bounded(
                    self._critical_terminal,
                    item,
                    evict_order=_TERMINAL_EVICT_ORDER,
                )
            elif level == "event":
                self._offer_bounded(self._critical_events, item)
            else:
                try:
                    self._normal.put_nowait(item)
                except queue.Full:
                    self._bump("dropped")
                    return
            self._bump("accepted")
        self._work_available.set()

    def set_worker_meta(
        self, instance: str | None, generation: int | None
    ) -> None:
        with self._meta_lock:
            if instance is not None:
                self._pending_instance = instance
            if generation is not None:
                self._pending_generation = generation
            self._meta_dirty = True
        self._work_available.set()

    def request_stop(self) -> None:
        with self._state_lock:
            self._accepting = False
        self._stop_requested.set()
        self._work_available.set()

    def stats(self) -> dict[str, int]:
        with self._stats_lock:
            return dict(self._stats)

    # -- writer internals -----------------------------------------------

    def _bump(self, key: str, amount: int = 1) -> None:
        if amount == 0:
            return
        with self._stats_lock:
            self._stats[key] = self._stats.get(key, 0) + amount

    def _bump_many(self, values: dict[str, int]) -> None:
        with self._stats_lock:
            for key, amount in values.items():
                if amount:
                    self._stats[key] = self._stats.get(key, 0) + amount

    def _offer_bounded(
        self,
        channel: list[_Pending],
        item: _Pending,
        *,
        evict_order: tuple[str, ...] = (),
    ) -> None:
        if len(channel) >= _CRITICAL_QUEUE_SIZE:
            victim = 0
            for name in evict_order:
                found = next(
                    (
                        index
                        for index, pending in enumerate(channel)
                        if pending.name == name
                    ),
                    None,
                )
                if found is not None:
                    victim = found
                    break
            channel.pop(victim)
            self._bump("evicted")
            if self._stats["evicted"] % 16 == 1:
                logger.warning(
                    "runtime trace: %s channel full; evicting entries",
                    "terminal" if evict_order else "event",
                )
        channel.append(item)

    def _take(self, channel: list[_Pending], limit: int) -> list[_Pending]:
        taken = channel[:limit]
        del channel[:limit]
        return taken

    def _drain_batch(self) -> list[_Pending]:
        with self._state_lock:
            batch = list(self._critical_roots)
            self._critical_roots.clear()
            batch.extend(self._take(self._critical_terminal, _TERMINAL_TAKE))
            batch.extend(self._take(self._critical_events, _EVENT_TAKE))
            for _ in range(_NORMAL_TAKE):
                try:
                    batch.append(self._normal.get_nowait())
                except queue.Empty:
                    break
            return batch

    def _flush_meta(self) -> None:
        with self._meta_lock:
            if not self._meta_dirty:
                return
            instance = self._pending_instance
            generation = self._pending_generation
            self._meta_dirty = False
        self._store.update_worker_meta(instance, generation)

    def _write(self, batch: list[_Pending]) -> None:
        try:
            result = self._store.write_batch(batch)
        except Exception:  # noqa: BLE001 - persistence must never surface
            self._bump("failed")
            logger.warning("runtime trace: batch write failed", exc_info=True)
            return
        self._bump_many(
            {
                "committed": result["inserted"],
                "conflicts": result["conflicts"],
                "dropped": result["dropped"],
                "missing_meta": result["missing"],
            }
        )

    def _sweep(self) -> None:
        cutoff_ns = time.time_ns() - self._retention_days * 86400 * 10**9
        try:
            deleted = self._store.sweep(
                cutoff_ns, batch_traces=_RETENTION_BATCH_TRACES
            )
            if deleted:
                logger.info(
                    "runtime trace: removed %d expired span records", deleted
                )
        except Exception:  # noqa: BLE001
            logger.warning("runtime trace: active sweep failed", exc_info=True)
        try:
            reclaim_history(
                self._store.root,
                self._store.role,
                self._retention_days,
                exclude_run_id=self._store.run_id,
            )
        except Exception:  # noqa: BLE001
            logger.warning("runtime trace: history reclaim failed", exc_info=True)

    def run(self) -> None:  # noqa: C901 - one explicit writer loop
        try:
            self._store.open()
        except Exception:  # noqa: BLE001 - degrade to no local storage
            self._bump("build_failed")
            with self._state_lock:
                self._accepting = False
            logger.error(
                "runtime trace: store build failed; records will be dropped",
                exc_info=True,
            )
            return

        next_sweep = time.monotonic()
        while True:
            now = time.monotonic()
            if now >= next_sweep:
                self._sweep()
                next_sweep = now + _RETENTION_SWEEP_SECONDS
            self._flush_meta()
            batch = self._drain_batch()
            if batch:
                self._write(batch)
                continue
            if self._stop_requested.is_set():
                break
            now = time.monotonic()
            self._work_available.wait(timeout=min(1.0, max(0.0, next_sweep - now)))
            self._work_available.clear()

        # Final drain: request_stop rejects new offers, so this terminates.
        while True:
            self._flush_meta()
            batch = self._drain_batch()
            if not batch:
                break
            self._write(batch)
        try:
            self._store.close(time.time_ns())
        except Exception:  # noqa: BLE001
            logger.warning("runtime trace: store close failed", exc_info=True)
