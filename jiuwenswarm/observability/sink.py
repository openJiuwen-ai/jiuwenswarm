# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded AgentServer consumer and SQLite writer for Core OTLP records."""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import replace

from jiuwenswarm.observability.config import (
    TrajectoryStoreSettings,
    session_database_path,
)
from jiuwenswarm.observability.models import (
    CommittedTraceUpdate,
    OtlpSpanRecordLike,
    OtlpSpanSnapshotRecordLike,
    TraceRecordData,
    TraceSinkStats,
)
from jiuwenswarm.observability.session_delete import trajectory_session_accepts_records
from jiuwenswarm.observability.store import TrajectoryStore

logger = logging.getLogger(__name__)

CommitCallback = Callable[[tuple[CommittedTraceUpdate, ...]], None]
_RETENTION_INTERVAL_SECONDS = 3600
# One retry keeps the two five-second SQLite busy waits below the default
# fifteen-second shutdown deadline, including the retry delay.
_WRITE_RETRY_DELAYS_SECONDS = (0.05,)
_SESSION_WRITER_IDLE_SECONDS = 300.0

_QueuedRecord = tuple[OtlpSpanRecordLike | OtlpSpanSnapshotRecordLike, bool]


def _record_owner_is_consistent(record: OtlpSpanRecordLike) -> bool:
    """Reject a subagent record whose execution session belongs to another chat."""
    owner = str(getattr(record, "session_id", "") or "").strip()
    subject_session = str(
        getattr(record, "execution_subject_session_id", "") or ""
    ).strip()
    if not owner or not subject_session or subject_session == owner:
        return True
    return subject_session.startswith(f"{owner}_sub_")


class TrajectoryRecordSink:
    """Fast Core consumer backed by a bounded queue and one writer thread."""

    def __init__(
        self,
        settings: TrajectoryStoreSettings,
        *,
        on_commit: CommitCallback | None = None,
        store: TrajectoryStore | None = None,
    ) -> None:
        self.settings = settings
        self._on_commit = on_commit
        self._store = store or TrajectoryStore(
            settings.database_path,
            retention_days=settings.retention_days,
        )
        self._queue: queue.Queue[OtlpSpanRecordLike] = queue.Queue(
            maxsize=settings.queue_size,
        )
        self._snapshot_pending: OrderedDict[
            tuple[str, str], OtlpSpanSnapshotRecordLike
        ] = OrderedDict()
        self._work_available = threading.Event()
        self._state_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._writing = threading.Event()
        self._thread: threading.Thread | None = None
        self._startup_error: BaseException | None = None
        self._accepting = False
        self._accepted = 0
        self._committed = 0
        self._dropped = 0
        self._failed = 0
        self._conflicts = 0
        self._coalesced = 0
        self._evicted_provisional = 0
        self._dropped_final = 0
        self._stale_ignored = 0

    def start(self, *, timeout: float = 10.0) -> None:
        """Initialize SQLite on the writer thread and enable fast consumption.

        Args:
            timeout: Maximum seconds to wait for schema initialization.

        Raises:
            RuntimeError: If the writer cannot initialize or times out.
        """
        with self._state_lock:
            if self._thread is not None:
                if self._startup_error is not None:
                    raise RuntimeError("Trajectory writer failed to initialize") from self._startup_error
                return
            self._startup_error = None
            self._stop_requested.clear()
            self._ready.clear()
            thread = threading.Thread(
                target=self._writer_main,
                name="trajectory-record-writer",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        if not self._ready.wait(timeout=max(0.1, float(timeout))):
            self._stop_requested.set()
            raise RuntimeError("Trajectory writer initialization timed out")
        if self._startup_error is not None:
            raise RuntimeError("Trajectory writer failed to initialize") from self._startup_error
        with self._state_lock:
            if (
                self._thread is not thread
                or self._stop_requested.is_set()
                or not thread.is_alive()
            ):
                raise RuntimeError("Trajectory writer stopped during initialization")
            self._accepting = True

    def consume(self, record: OtlpSpanRecordLike) -> None:
        """Atomically accept one frozen Core record without storage work.

        Core's processor publishes an immutable record whose ``raw_json`` is
        already ``bytes``. Validation, copying, hashing, and SQLite work stay
        on the writer thread so a full queue remains a constant-cost decision.
        """
        if not isinstance(getattr(record, "raw_json", None), bytes):
            self._increment("failed")
            logger.warning("Trajectory record rejected before queueing: raw_json must be bytes")
            return
        if not _record_owner_is_consistent(record):
            self._increment("failed")
            logger.warning(
                "Trajectory record rejected because execution subject belongs "
                "to another session: session_id=%s subject_session_id=%s",
                getattr(record, "session_id", None),
                getattr(record, "execution_subject_session_id", None),
            )
            return
        with self._state_lock:
            if not self._accepting:
                self._increment("dropped")
                logger.warning("Trajectory record dropped because the sink is not accepting records")
                return
            identity = (
                str(getattr(record, "trace_id", "") or "").strip().lower(),
                str(getattr(record, "span_id", "") or "").strip().lower(),
            )
            if identity in self._snapshot_pending:
                self._snapshot_pending.pop(identity, None)
                self._increment("coalesced")
            try:
                # Keep this non-blocking enqueue under the same lock used by
                # close(): every accepted record is therefore visible before
                # the writer receives its stop request.
                self._queue.put_nowait(record)
            except queue.Full:
                self._increment("dropped")
                self._increment("dropped_final")
                logger.warning(
                    "Trajectory queue is full; SQLite fan-out dropped the record "
                    "while other configured exporters remain unaffected: "
                    "trace_id=%s span_id=%s",
                    getattr(record, "trace_id", None),
                    getattr(record, "span_id", None),
                )
                return
            self._work_available.set()
        self._increment("accepted")

    def consume_snapshot(self, record: OtlpSpanSnapshotRecordLike) -> None:
        """Accept a live snapshot using identity-keyed latest-wins coalescing."""
        if not isinstance(getattr(record, "raw_json", None), bytes):
            self._increment("failed")
            logger.warning("Trajectory snapshot rejected before queueing: raw_json must be bytes")
            return
        if not _record_owner_is_consistent(record):
            self._increment("failed")
            return
        identity = (
            str(getattr(record, "trace_id", "") or "").strip().lower(),
            str(getattr(record, "span_id", "") or "").strip().lower(),
        )
        try:
            revision = int(getattr(record, "record_revision"))
        except (TypeError, ValueError, OverflowError):
            self._increment("failed")
            logger.warning("Trajectory snapshot rejected before queueing: invalid record_revision")
            return
        if not identity[0] or not identity[1] or revision < 1:
            self._increment("failed")
            logger.warning("Trajectory snapshot rejected before queueing: invalid identity or revision")
            return
        with self._state_lock:
            if not self._accepting:
                self._increment("dropped")
                return
            current = self._snapshot_pending.get(identity)
            if current is not None:
                current_revision = int(getattr(current, "record_revision", 0))
                if revision <= current_revision:
                    self._increment("stale_ignored")
                    return
                self._snapshot_pending[identity] = record
                self._snapshot_pending.move_to_end(identity)
                self._increment("coalesced")
                self._work_available.set()
                self._increment("accepted")
                return
            if self._queue.qsize() + len(self._snapshot_pending) >= self.settings.queue_size:
                if self._snapshot_pending:
                    self._snapshot_pending.popitem(last=False)
                    self._increment("evicted_provisional")
                    self._increment("dropped")
                else:
                    self._increment("dropped")
                    return
            self._snapshot_pending[identity] = record
            self._work_available.set()
        self._increment("accepted")

    def close(self, *, timeout: float = 15.0) -> bool:
        """Stop accepting, drain queued records, and close SQLite.

        Args:
            timeout: Maximum seconds to wait for the writer thread.

        Returns:
            ``True`` when the writer drained and stopped before the timeout.
        """
        with self._state_lock:
            self._accepting = False
            thread = self._thread
        if thread is None:
            return True
        self._stop_requested.set()
        self._work_available.set()
        thread.join(timeout=max(0.1, float(timeout)))
        stopped = not thread.is_alive()
        if not stopped:
            logger.warning(
                "Trajectory writer shutdown timed out with %d queued records",
                self._queue.qsize(),
            )
            return False
        with self._state_lock:
            self._thread = None
        return True

    def stats(self) -> TraceSinkStats:
        """Return an atomic snapshot of sink counters."""
        with self._stats_lock:
            return TraceSinkStats(
                accepted=self._accepted,
                committed=self._committed,
                dropped=self._dropped,
                failed=self._failed,
                conflicts=self._conflicts,
                queued=self._queue.qsize() + len(self._snapshot_pending),
                coalesced=self._coalesced,
                evicted_provisional=self._evicted_provisional,
                dropped_final=self._dropped_final,
                stale_ignored=self._stale_ignored,
            )

    def set_commit_callback(self, on_commit: CommitCallback | None) -> None:
        """Replace the post-commit notification callback atomically."""
        with self._state_lock:
            self._on_commit = on_commit

    def is_idle(self) -> bool:
        """Return whether no record is queued or being committed."""
        with self._state_lock:
            return (
                self._queue.empty()
                and not self._snapshot_pending
                and not self._writing.is_set()
            )

    def _writer_main(self) -> None:
        try:
            self._store.initialize()
            try:
                self._store.delete_expired()
            except Exception:
                logger.exception("Initial trajectory retention cleanup failed")
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
            logger.exception("Trajectory writer initialization failed")
            return
        self._ready.set()
        next_retention_at = time.monotonic() + _RETENTION_INTERVAL_SECONDS
        flush_timeout = self.settings.flush_interval_ms / 1000
        try:
            while (
                not self._stop_requested.is_set()
                or not self._queue.empty()
                or bool(self._snapshot_pending)
            ):
                self._writing.set()
                try:
                    batch = self._take_batch(flush_timeout)
                    if batch:
                        self._write_batch(batch)
                finally:
                    self._writing.clear()
                if time.monotonic() >= next_retention_at:
                    try:
                        self._store.delete_expired()
                    except Exception:
                        logger.exception("Trajectory retention cleanup failed")
                    next_retention_at = time.monotonic() + _RETENTION_INTERVAL_SECONDS
        finally:
            self._store.close()

    def _take_batch(
        self,
        timeout: float,
    ) -> list[tuple[OtlpSpanRecordLike | OtlpSpanSnapshotRecordLike, bool]]:
        self._work_available.wait(timeout=max(0.001, timeout))
        self._work_available.clear()
        self._wait_for_snapshot_coalescing(timeout)
        batch: list[tuple[OtlpSpanRecordLike | OtlpSpanSnapshotRecordLike, bool]] = []
        while len(batch) < self.settings.batch_size:
            try:
                batch.append((self._queue.get_nowait(), True))
            except queue.Empty:
                break
        with self._state_lock:
            while len(batch) < self.settings.batch_size and self._snapshot_pending:
                _identity, snapshot = self._snapshot_pending.popitem(last=False)
                batch.append((snapshot, False))
            if not self._queue.empty() or self._snapshot_pending:
                self._work_available.set()
        return batch

    def _wait_for_snapshot_coalescing(self, timeout: float) -> None:
        """Debounce provisional snapshots while letting final records preempt."""
        with self._state_lock:
            has_snapshots = bool(self._snapshot_pending)
        if (
            not has_snapshots
            or not self._queue.empty()
            or self._stop_requested.is_set()
        ):
            return

        deadline = time.monotonic() + max(0.001, timeout)
        while not self._stop_requested.is_set():
            self._work_available.clear()
            if not self._queue.empty():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._work_available.wait(timeout=remaining)

    def _write_batch(
        self,
        batch: list[tuple[OtlpSpanRecordLike | OtlpSpanSnapshotRecordLike, bool]],
    ) -> None:
        try:
            records: list[TraceRecordData] = []
            for record, queued_final in batch:
                try:
                    if queued_final:
                        records.append(TraceRecordData.from_core_record(record))
                    else:
                        records.append(TraceRecordData.from_core_snapshot(record))
                except (AttributeError, TypeError, ValueError, OverflowError) as exc:
                    self._increment("failed")
                    logger.warning("Trajectory record rejected by writer: %s", exc)
            if not records:
                return
            result = self._write_records_with_retry(records)
        except Exception:
            self._increment("failed", len(records))
            logger.exception(
                "Trajectory batch commit failed; records remain available only through existing exporters"
            )
        else:
            self._increment("committed", result.inserted)
            self._increment("conflicts", result.conflicts)
            with self._state_lock:
                on_commit = self._on_commit
            if result.updates and on_commit is not None:
                try:
                    on_commit(result.updates)
                except Exception:
                    logger.exception("Trajectory commit notification callback failed")
        finally:
            for _record, queued_final in batch:
                if queued_final:
                    self._queue.task_done()

    def _write_records_with_retry(
        self,
        records: list[TraceRecordData],
    ):
        """Retry transient SQLite contention without creating an infinite drain."""
        for attempt, delay in enumerate((*_WRITE_RETRY_DELAYS_SECONDS, None)):
            try:
                return self._store.write_records(records)
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                retryable = "locked" in message or "busy" in message
                if not retryable or delay is None:
                    raise
                logger.warning(
                    "Trajectory SQLite commit contention; retrying batch: attempt=%d",
                    attempt + 1,
                )
                time.sleep(delay)
        raise AssertionError("unreachable trajectory write retry state")

    def _increment(self, counter: str, amount: int = 1) -> None:
        with self._stats_lock:
            if counter == "accepted":
                self._accepted += amount
            elif counter == "committed":
                self._committed += amount
            elif counter == "dropped":
                self._dropped += amount
            elif counter == "failed":
                self._failed += amount
            elif counter == "conflicts":
                self._conflicts += amount
            elif counter == "coalesced":
                self._coalesced += amount
            elif counter == "evicted_provisional":
                self._evicted_provisional += amount
            elif counter == "dropped_final":
                self._dropped_final += amount
            elif counter == "stale_ignored":
                self._stale_ignored += amount
            else:
                raise ValueError(f"unknown trajectory counter: {counter}")


class _SessionRoute:
    """Start and feed one session writer without blocking the router thread."""

    def __init__(
        self,
        settings: TrajectoryStoreSettings,
        session_id: str,
        on_commit: CommitCallback | None,
        sink_factory: Callable[
            [TrajectoryStoreSettings, CommitCallback | None], TrajectoryRecordSink
        ],
    ) -> None:
        session_settings = replace(
            settings,
            database_path=session_database_path(settings.database_path, session_id),
        )
        self._sink = sink_factory(session_settings, on_commit)
        self._queue: queue.Queue[_QueuedRecord] = queue.Queue(
            maxsize=settings.queue_size,
        )
        self._stop_requested = threading.Event()
        self._state_lock = threading.Lock()
        self._accepting = True
        self._last_activity = time.monotonic()
        self._thread = threading.Thread(
            target=self._run,
            name=f"trajectory-session-route-{session_settings.database_path.stem[:12]}",
            daemon=True,
        )
        self._thread.start()

    def offer(self, item: _QueuedRecord) -> bool:
        """Offer one item without waiting for startup or SQLite."""
        with self._state_lock:
            if not self._accepting:
                return False
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                return False
            self._last_activity = time.monotonic()
            return True

    def can_retire(self, now: float) -> bool:
        """Return whether this route has been fully idle long enough."""
        with self._state_lock:
            return (
                not self._accepting
                or (
                    now - self._last_activity >= _SESSION_WRITER_IDLE_SECONDS
                    and self._queue.empty()
                    and self._sink.is_idle()
                )
            )

    def request_stop(self) -> None:
        """Request a drain without waiting for the route thread."""
        with self._state_lock:
            self._accepting = False
            self._stop_requested.set()

    def join(self, timeout: float) -> bool:
        """Wait for the route and its child sink to drain."""
        self._thread.join(timeout=max(0.0, timeout))
        return not self._thread.is_alive()

    def stats(self) -> TraceSinkStats:
        """Return child writer counters plus route backlog."""
        child = self._sink.stats()
        return TraceSinkStats(
            accepted=child.accepted,
            committed=child.committed,
            dropped=child.dropped,
            failed=child.failed,
            conflicts=child.conflicts,
            queued=child.queued + self._queue.qsize(),
            coalesced=child.coalesced,
            evicted_provisional=child.evicted_provisional,
            dropped_final=child.dropped_final,
            stale_ignored=child.stale_ignored,
        )

    def set_commit_callback(self, on_commit: CommitCallback | None) -> None:
        """Replace the callback used by this child writer."""
        self._sink.set_commit_callback(on_commit)

    def _run(self) -> None:
        try:
            self._sink.start()
        except Exception:
            with self._state_lock:
                self._accepting = False
            logger.exception("Trajectory session writer initialization failed")
            while True:
                try:
                    record, snapshot = self._queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    if snapshot:
                        self._sink.consume_snapshot(record)
                    else:
                        self._sink.consume(record)
                finally:
                    self._queue.task_done()
            return
        try:
            while not self._stop_requested.is_set() or not self._queue.empty():
                try:
                    record, snapshot = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    if snapshot:
                        self._sink.consume_snapshot(record)
                    else:
                        self._sink.consume(record)
                finally:
                    self._queue.task_done()
        finally:
            with self._state_lock:
                self._accepting = False
            self._sink.close()


class TrajectorySessionSinkRouter:
    """Non-blocking process sink routing each session to an isolated writer."""

    def __init__(
        self,
        settings: TrajectoryStoreSettings,
        *,
        on_commit: CommitCallback | None = None,
        sink_factory: Callable[
            [TrajectoryStoreSettings, CommitCallback | None], TrajectoryRecordSink
        ] | None = None,
    ) -> None:
        self.settings = settings
        self._on_commit = on_commit
        self._sink_factory = sink_factory or _create_session_sink
        self._queue: queue.Queue[tuple[str, _QueuedRecord]] = queue.Queue(
            maxsize=settings.queue_size,
        )
        self._routes: dict[str, _SessionRoute] = {}
        self._orphan_pending: OrderedDict[
            tuple[str, str], _QueuedRecord
        ] = OrderedDict()
        self._routes_lock = threading.Lock()
        self._ingress_condition = threading.Condition()
        self._ingress_pending: dict[str, int] = {}
        self._state_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._accepting = False
        self._accepted = 0
        self._dropped = 0
        self._failed = 0
        self._dropped_final = 0

    def start(self, *, timeout: float = 10.0) -> None:
        """Start the lightweight routing thread without opening SQLite."""
        with self._state_lock:
            if self._thread is not None:
                return
            self._stop_requested.clear()
            self._ready.clear()
            thread = threading.Thread(
                target=self._run,
                name="trajectory-session-router",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        if not self._ready.wait(timeout=max(0.1, float(timeout))):
            self._stop_requested.set()
            raise RuntimeError("Trajectory session router initialization timed out")
        with self._state_lock:
            if self._thread is not thread or not thread.is_alive():
                raise RuntimeError("Trajectory session router stopped during initialization")
            self._accepting = True

    def consume(self, record: OtlpSpanRecordLike) -> None:
        """Accept a final Core record using one constant-cost enqueue."""
        self._consume(record, snapshot=False)

    def consume_snapshot(self, record: OtlpSpanSnapshotRecordLike) -> None:
        """Accept a provisional Core snapshot using one constant-cost enqueue."""
        self._consume(record, snapshot=True)

    def close(self, *, timeout: float = 15.0) -> bool:
        """Stop accepting and drain the router and every session writer."""
        deadline = time.monotonic() + max(0.1, float(timeout))
        with self._state_lock:
            self._accepting = False
            thread = self._thread
        if thread is None:
            return True
        self._stop_requested.set()
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            return False
        with self._routes_lock:
            routes = tuple(self._routes.values())
        for route in routes:
            route.request_stop()
        stopped = True
        for route in routes:
            stopped = route.join(max(0.0, deadline - time.monotonic())) and stopped
        if stopped:
            with self._state_lock:
                self._thread = None
        return stopped

    def stats(self) -> TraceSinkStats:
        """Aggregate ingress and child-writer diagnostics."""
        with self._routes_lock:
            routes = tuple(self._routes.values())
            orphan_count = len(self._orphan_pending)
        child_stats = [route.stats() for route in routes]
        with self._stats_lock:
            accepted = self._accepted
            dropped = self._dropped
            failed = self._failed
            dropped_final = self._dropped_final
        return TraceSinkStats(
            accepted=accepted,
            committed=sum(item.committed for item in child_stats),
            dropped=dropped + sum(item.dropped for item in child_stats),
            failed=failed + sum(item.failed for item in child_stats),
            conflicts=sum(item.conflicts for item in child_stats),
            queued=(
                self._queue.qsize()
                + orphan_count
                + sum(item.queued for item in child_stats)
            ),
            coalesced=sum(item.coalesced for item in child_stats),
            evicted_provisional=sum(item.evicted_provisional for item in child_stats),
            dropped_final=dropped_final + sum(item.dropped_final for item in child_stats),
            stale_ignored=sum(item.stale_ignored for item in child_stats),
        )

    def set_commit_callback(self, on_commit: CommitCallback | None) -> None:
        """Replace the callback for existing and future session writers."""
        with self._state_lock:
            self._on_commit = on_commit
        with self._routes_lock:
            routes = tuple(self._routes.values())
        for route in routes:
            route.set_commit_callback(on_commit)

    def begin_session_delete(self, session_id: str, *, timeout: float = 15.0) -> None:
        """Drain and close one Session route after its tombstone is installed."""
        resolved = str(session_id or "").strip()
        if not resolved:
            raise ValueError("session_id is required")
        deadline = time.monotonic() + max(0.1, float(timeout))
        with self._ingress_condition:
            while self._ingress_pending.get(resolved, 0) > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        "Trajectory Session ingress did not drain before deletion"
                    )
                self._ingress_condition.wait(timeout=remaining)
        with self._routes_lock:
            route = self._routes.get(resolved)
            if route is not None:
                route.request_stop()
        if route is None:
            return
        if route.join(max(0.0, deadline - time.monotonic())):
            with self._routes_lock:
                if self._routes.get(resolved) is route:
                    self._routes.pop(resolved)
            return
        raise RuntimeError("Trajectory Session writer did not stop before deletion")

    @staticmethod
    def abort_session_delete(session_id: str) -> None:
        """Allow a rolled-back Session to lazily create a fresh route."""
        if not str(session_id or "").strip():
            raise ValueError("session_id is required")

    def commit_session_delete(self, session_id: str) -> None:
        """Idempotently delete one closed Session database and its sidecars."""
        resolved = str(session_id or "").strip()
        if not resolved:
            raise ValueError("session_id is required")
        self.begin_session_delete(resolved)
        database_path = session_database_path(self.settings.database_path, resolved)
        for candidate in (
            database_path,
            database_path.with_name(f"{database_path.name}-wal"),
            database_path.with_name(f"{database_path.name}-shm"),
        ):
            candidate.unlink(missing_ok=True)

    def _consume(
        self,
        record: OtlpSpanRecordLike | OtlpSpanSnapshotRecordLike,
        *,
        snapshot: bool,
    ) -> None:
        raw_session_id = str(getattr(record, "session_id", "") or "")
        session_id = raw_session_id.strip()
        trace_id = str(getattr(record, "trace_id", "") or "").strip().lower()
        span_id = str(getattr(record, "span_id", "") or "").strip().lower()
        malformed_identity = (
            (session_id and session_id != raw_session_id) or not trace_id or not span_id
        )
        if malformed_identity or not isinstance(getattr(record, "raw_json", None), bytes):
            self._increment("failed")
            return
        if not _record_owner_is_consistent(record):
            self._increment("failed")
            return
        if session_id and not trajectory_session_accepts_records(session_id):
            self._increment("dropped")
            if not snapshot:
                self._increment("dropped_final")
            return
        with self._state_lock:
            if session_id and not trajectory_session_accepts_records(session_id):
                self._increment("dropped")
                if not snapshot:
                    self._increment("dropped_final")
                return
            if not self._accepting:
                self._increment("dropped")
                return
            with self._ingress_condition:
                try:
                    self._queue.put_nowait((session_id, (record, snapshot)))
                except queue.Full:
                    self._increment("dropped")
                    if not snapshot:
                        self._increment("dropped_final")
                    return
                self._ingress_pending[session_id] = (
                    self._ingress_pending.get(session_id, 0) + 1
                )
        self._increment("accepted")

    def _run(self) -> None:
        self._ready.set()
        while not self._stop_requested.is_set() or not self._queue.empty():
            try:
                session_id, item = self._queue.get(timeout=0.1)
            except queue.Empty:
                self._retire_idle_routes()
                continue
            try:
                if not session_id:
                    self._buffer_orphan(item)
                    continue
                trace_id = str(getattr(item[0], "trace_id", "") or "").strip().lower()
                with self._routes_lock:
                    route = self._routes.get(session_id)
                    if route is None:
                        route = _SessionRoute(
                            self.settings,
                            session_id,
                            self._on_commit,
                            self._sink_factory,
                        )
                        self._routes[session_id] = route
                    routed_items = self._take_orphans_locked(trace_id)
                    routed_items.append(item)
                    for routed_item in routed_items:
                        if route.offer(routed_item):
                            continue
                        self._increment("dropped")
                        if not routed_item[1]:
                            self._increment("dropped_final")
            finally:
                self._complete_ingress(session_id)
                self._queue.task_done()
        self._drop_all_orphans()

    def _retire_idle_routes(self) -> None:
        now = time.monotonic()
        with self._routes_lock:
            candidates = [
                (session_id, route)
                for session_id, route in self._routes.items()
                if route.can_retire(now)
            ]
            for _session_id, route in candidates:
                route.request_stop()
        for session_id, route in candidates:
            if not route.join(1.0):
                continue
            with self._routes_lock:
                if self._routes.get(session_id) is route:
                    self._routes.pop(session_id)

    def _buffer_orphan(self, item: _QueuedRecord) -> None:
        """Hold a sessionless child until a session-owned span reveals its route."""
        record, _snapshot = item
        identity = (
            str(getattr(record, "trace_id", "") or "").strip().lower(),
            str(getattr(record, "span_id", "") or "").strip().lower(),
        )
        with self._routes_lock:
            previous = self._orphan_pending.pop(identity, None)
            if previous is None and len(self._orphan_pending) >= self.settings.queue_size:
                _evicted_identity, evicted = self._orphan_pending.popitem(last=False)
                self._increment("dropped")
                if not evicted[1]:
                    self._increment("dropped_final")
            self._orphan_pending[identity] = item

    def _take_orphans_locked(self, trace_id: str) -> list[_QueuedRecord]:
        identities = [
            identity for identity in self._orphan_pending if identity[0] == trace_id
        ]
        return [self._orphan_pending.pop(identity) for identity in identities]

    def _drop_all_orphans(self) -> None:
        with self._routes_lock:
            orphans = tuple(self._orphan_pending.values())
            self._orphan_pending.clear()
        if not orphans:
            return
        self._increment("dropped", len(orphans))
        final_count = sum(1 for _record, snapshot in orphans if not snapshot)
        if final_count:
            self._increment("dropped_final", final_count)

    def _complete_ingress(self, session_id: str) -> None:
        with self._ingress_condition:
            remaining = self._ingress_pending.get(session_id, 0) - 1
            if remaining > 0:
                self._ingress_pending[session_id] = remaining
            else:
                self._ingress_pending.pop(session_id, None)
            self._ingress_condition.notify_all()

    def _increment(self, counter: str, amount: int = 1) -> None:
        with self._stats_lock:
            if counter == "accepted":
                self._accepted += amount
            elif counter == "dropped":
                self._dropped += amount
            elif counter == "failed":
                self._failed += amount
            elif counter == "dropped_final":
                self._dropped_final += amount
            else:
                raise ValueError(f"unknown trajectory router counter: {counter}")


def _create_session_sink(
    settings: TrajectoryStoreSettings,
    on_commit: CommitCallback | None,
) -> TrajectoryRecordSink:
    return TrajectoryRecordSink(settings, on_commit=on_commit)


__all__ = [
    "CommitCallback",
    "TrajectoryRecordSink",
    "TrajectorySessionSinkRouter",
]
