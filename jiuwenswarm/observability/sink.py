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
from dataclasses import dataclass, replace

from jiuwenswarm.observability.config import (
    TrajectoryStoreSettings,
    session_database_path,
)
from jiuwenswarm.observability.models import (
    CommittedTraceUpdate,
    OtlpSpanRecordLike,
    OtlpSpanSnapshotRecordLike,
    StreamFrameData,
    StreamFrameRecordLike,
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
# A flush window of a fast stream holds far more frames than it holds spans,
# and each is small, so frames are drained well past the record batch size
# rather than left to accumulate a growing lag.
_MAX_FRAMES_PER_BATCH = 512

# What a queued item is, so the router can hand it to the right sink entry
# point. Three kinds share one queue because they share one routing decision:
# which session owns the record.
_RECORD_FINAL = "final"
_RECORD_SNAPSHOT = "snapshot"
_RECORD_FRAME = "frame"

_QueuedPayload = OtlpSpanRecordLike | OtlpSpanSnapshotRecordLike | StreamFrameRecordLike
_QueuedRecord = tuple[_QueuedPayload, str]


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
        # Frames get their own queue rather than sharing the record queue. A
        # fast stream produces them far faster than spans end, and sharing
        # would let a burst of frames push a final record out -- trading a
        # replaceable increment for the one record that is authoritative.
        self._frame_queue: queue.Queue[StreamFrameRecordLike] = queue.Queue(
            maxsize=settings.queue_size,
        )
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
        self._dropped_frames = 0

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
        encoded lazily on first read. Payload validation, copying, hashing, and
        SQLite work stay on the writer thread so a full queue remains a
        constant-cost decision and no payload is encoded before it is needed.
        """
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
        """Accept a live snapshot using identity-keyed latest-wins coalescing.

        The payload stays unread here: a snapshot this method later coalesces
        away must never cost an OTLP encode. See ``consume`` for where the
        payload is validated instead.
        """
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

    def consume_stream_frame(self, record: StreamFrameRecordLike) -> None:
        """Accept one model-stream frame; frames are appended, never merged.

        Nothing coalesces here, unlike ``consume_snapshot``: a frame states an
        increment no later frame restates, so dropping one leaves a hole a
        reader cannot fill until the span ends and its complete output
        arrives.
        """
        if not _record_owner_is_consistent(record):
            self._increment("failed")
            return
        with self._state_lock:
            if not self._accepting:
                self._increment("dropped")
                return
            try:
                self._frame_queue.put_nowait(record)
            except queue.Full:
                self._increment("dropped")
                self._increment("dropped_frames")
                logger.warning(
                    "Trajectory frame queue is full; the live stream will show a gap "
                    "until the span's complete output arrives: trace_id=%s span_id=%s",
                    getattr(record, "trace_id", None),
                    getattr(record, "span_id", None),
                )
                return
            self._work_available.set()
        self._increment("accepted")

    def request_stop(self) -> None:
        """Stop accepting and ask the writer to drain, without waiting for it.

        Lets a caller holding several sinks start every drain before waiting on
        any of them, so their shutdowns overlap instead of running back to back.
        """
        with self._state_lock:
            self._accepting = False
        self._stop_requested.set()
        self._work_available.set()

    def close(self, *, timeout: float = 15.0) -> bool:
        """Stop accepting, drain queued records, and close SQLite.

        Args:
            timeout: Maximum seconds to wait for the writer thread.

        Returns:
            ``True`` when the writer drained and stopped before the timeout.
        """
        self.request_stop()
        with self._state_lock:
            thread = self._thread
        if thread is None:
            return True
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
                queued=(
                    self._queue.qsize()
                    + len(self._snapshot_pending)
                    + self._frame_queue.qsize()
                ),
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
                and self._frame_queue.empty()
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
                or not self._frame_queue.empty()
            ):
                self._writing.set()
                try:
                    batch = self._take_batch(flush_timeout)
                    frames = self._take_frame_batch()
                    if batch or frames:
                        self._write_batch(batch, frames)
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

    def _take_frame_batch(self) -> list[StreamFrameRecordLike]:
        """Drain the frames that arrived, keeping their emission order.

        The batch is capped well above ``batch_size`` because frames are
        small and must not be left behind: a stream produces many per flush
        window, and deferring them would show the reader an answer that lags
        further behind the longer the model talks.
        """
        frames: list[StreamFrameRecordLike] = []
        limit = max(self.settings.batch_size, _MAX_FRAMES_PER_BATCH)
        while len(frames) < limit:
            try:
                frames.append(self._frame_queue.get_nowait())
            except queue.Empty:
                break
        if not self._frame_queue.empty():
            self._work_available.set()
        return frames

    def _has_preempting_work(self) -> bool:
        """Whether a final record or a frame is waiting to preempt a debounce.

        Returns:
            True when either queue holds work that must not wait on a snapshot.
        """
        return not self._queue.empty() or not self._frame_queue.empty()

    def _wait_for_snapshot_coalescing(self, timeout: float) -> None:
        """Debounce provisional snapshots, letting finals and frames preempt.

        Frames preempt the wait because they are what a reader is watching in
        real time: holding them for a snapshot debounce would make a live
        answer arrive a whole flush window late.
        """
        with self._state_lock:
            has_snapshots = bool(self._snapshot_pending)
        if not has_snapshots or self._has_preempting_work() or self._stop_requested.is_set():
            return

        deadline = time.monotonic() + max(0.001, timeout)
        while not self._stop_requested.is_set():
            self._work_available.clear()
            if self._has_preempting_work():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._work_available.wait(timeout=remaining)

    def _write_batch(
        self,
        batch: list[tuple[OtlpSpanRecordLike | OtlpSpanSnapshotRecordLike, bool]],
        frame_batch: list[StreamFrameRecordLike] | None = None,
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
            frames: list[StreamFrameData] = []
            for frame in frame_batch or ():
                try:
                    frames.append(StreamFrameData.from_core_frame(frame))
                except (AttributeError, TypeError, ValueError, OverflowError) as exc:
                    self._increment("failed")
                    logger.warning("Trajectory frame rejected by writer: %s", exc)
            if not records and not frames:
                return
            result = self._write_records_with_retry(records, frames)
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
        frames: list[StreamFrameData] | None = None,
    ):
        """Retry transient SQLite contention without creating an infinite drain."""
        for attempt, delay in enumerate((*_WRITE_RETRY_DELAYS_SECONDS, None)):
            try:
                return self._store.write_records(records, frames or ())
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
            elif counter == "dropped_frames":
                self._dropped_frames += amount
            else:
                raise ValueError(f"unknown trajectory counter: {counter}")


@dataclass(slots=True)
class _SessionWriter:
    """One session's writer plus the clock deciding when to retire it.

    The router feeds ``sink`` directly from its own thread. Both hand-offs are
    bounded non-blocking enqueues, so a thread of its own here would only move
    records between two queues.
    """

    sink: TrajectoryRecordSink
    last_activity: float
    stopping: bool = False


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
        self._writers: dict[str, _SessionWriter] = {}
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
        self._consume(record, kind=_RECORD_FINAL)

    def consume_snapshot(self, record: OtlpSpanSnapshotRecordLike) -> None:
        """Accept a provisional Core snapshot using one constant-cost enqueue."""
        self._consume(record, kind=_RECORD_SNAPSHOT)

    def consume_stream_frame(self, record: StreamFrameRecordLike) -> None:
        """Accept one model-stream frame using one constant-cost enqueue."""
        self._consume(record, kind=_RECORD_FRAME)

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
            sinks = tuple(writer.sink for writer in self._writers.values())
        # Ask every writer to drain before waiting on any of them, so the
        # session shutdowns overlap within the one deadline.
        for sink in sinks:
            sink.request_stop()
        stopped = True
        for sink in sinks:
            stopped = sink.close(timeout=max(0.0, deadline - time.monotonic())) and stopped
        if stopped:
            with self._state_lock:
                self._thread = None
        return stopped

    def stats(self) -> TraceSinkStats:
        """Aggregate ingress and child-writer diagnostics."""
        with self._routes_lock:
            sinks = tuple(writer.sink for writer in self._writers.values())
            orphan_count = len(self._orphan_pending)
        child_stats = [sink.stats() for sink in sinks]
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
            sinks = tuple(writer.sink for writer in self._writers.values())
        for sink in sinks:
            sink.set_commit_callback(on_commit)

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
            writer = self._writers.get(resolved)
            if writer is not None:
                writer.stopping = True
                writer.sink.request_stop()
        if writer is None:
            return
        if writer.sink.close(timeout=max(0.0, deadline - time.monotonic())):
            with self._routes_lock:
                if self._writers.get(resolved) is writer:
                    self._writers.pop(resolved)
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
        record: _QueuedPayload,
        *,
        kind: str,
    ) -> None:
        is_final = kind == _RECORD_FINAL
        raw_session_id = str(getattr(record, "session_id", "") or "")
        session_id = raw_session_id.strip()
        trace_id = str(getattr(record, "trace_id", "") or "").strip().lower()
        span_id = str(getattr(record, "span_id", "") or "").strip().lower()
        malformed_identity = (
            (session_id and session_id != raw_session_id) or not trace_id or not span_id
        )
        # Only identity is validated here. Agent Core encodes ``raw_json`` lazily
        # on first read, so touching it on this thread would drag the full OTLP
        # encode back onto the caller — normally the event loop — and would pay
        # it even for snapshots the writer later coalesces away. The payload is
        # type-checked in TraceRecordData.from_core_* on the writer thread, where
        # _write_batch already rejects a bad record without failing its batch.
        if malformed_identity:
            self._increment("failed")
            return
        if not _record_owner_is_consistent(record):
            self._increment("failed")
            return
        if session_id and not trajectory_session_accepts_records(session_id):
            self._increment("dropped")
            if is_final:
                self._increment("dropped_final")
            return
        with self._state_lock:
            if session_id and not trajectory_session_accepts_records(session_id):
                self._increment("dropped")
                if is_final:
                    self._increment("dropped_final")
                return
            if not self._accepting:
                self._increment("dropped")
                return
            with self._ingress_condition:
                try:
                    self._queue.put_nowait((session_id, (record, kind)))
                except queue.Full:
                    self._increment("dropped")
                    if is_final:
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
                    writer = self._writers.get(session_id)
                    if writer is None:
                        writer = self._create_writer(session_id)
                        self._writers[session_id] = writer
                    writer.last_activity = time.monotonic()
                    routed_items = self._take_orphans_locked(trace_id)
                    routed_items.append(item)
                    sink = writer.sink
                # Hand off outside the routes lock: each consume is a bounded
                # non-blocking enqueue that counts its own drops.
                for routed_record, routed_kind in routed_items:
                    if routed_kind == _RECORD_SNAPSHOT:
                        sink.consume_snapshot(routed_record)
                    elif routed_kind == _RECORD_FRAME:
                        sink.consume_stream_frame(routed_record)
                    else:
                        sink.consume(routed_record)
            finally:
                self._complete_ingress(session_id)
                self._queue.task_done()
        self._drop_all_orphans()

    def _create_writer(self, session_id: str) -> _SessionWriter:
        """Open one session's writer, keeping a failed one out of the retry path."""
        session_settings = replace(
            self.settings,
            database_path=session_database_path(self.settings.database_path, session_id),
        )
        sink = self._sink_factory(session_settings, self._on_commit)
        try:
            sink.start()
        except Exception:
            # Keep the failed sink registered. It rejects records through its own
            # counters, whereas retrying start per record would stall routing for
            # every other session behind a repeated initialization timeout.
            logger.exception("Trajectory session writer initialization failed")
        return _SessionWriter(sink=sink, last_activity=time.monotonic())

    def _retire_idle_routes(self) -> None:
        now = time.monotonic()
        with self._routes_lock:
            candidates: list[tuple[str, _SessionWriter]] = []
            for session_id, writer in self._writers.items():
                idle_long_enough = (
                    now - writer.last_activity >= _SESSION_WRITER_IDLE_SECONDS
                    and writer.sink.is_idle()
                )
                if writer.stopping or idle_long_enough:
                    candidates.append((session_id, writer))
            for _session_id, writer in candidates:
                writer.stopping = True
                writer.sink.request_stop()
        for session_id, writer in candidates:
            # Only forget a writer that actually stopped: a second writer on the
            # same session would open a second connection to the same database.
            if not writer.sink.close(timeout=1.0):
                continue
            with self._routes_lock:
                if self._writers.get(session_id) is writer:
                    self._writers.pop(session_id)

    def _buffer_orphan(self, item: _QueuedRecord) -> None:
        """Hold a sessionless child until a session-owned span reveals its route."""
        record, kind = item
        identity: tuple[str, ...] = (
            str(getattr(record, "trace_id", "") or "").strip().lower(),
            str(getattr(record, "span_id", "") or "").strip().lower(),
        )
        if kind == _RECORD_FRAME:
            # A span identifies a record, but not a frame: frames are additive,
            # so keying them by span alone would make each new one evict its
            # predecessor and leave a hole in the stream.
            identity = (*identity, str(getattr(record, "sequence", 0)))
        with self._routes_lock:
            previous = self._orphan_pending.pop(identity, None)
            if previous is None and len(self._orphan_pending) >= self.settings.queue_size:
                _evicted_identity, evicted = self._orphan_pending.popitem(last=False)
                self._increment("dropped")
                if evicted[1] == _RECORD_FINAL:
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
        final_count = sum(1 for _record, kind in orphans if kind == _RECORD_FINAL)
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
