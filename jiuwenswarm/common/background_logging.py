# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bound Worker log buffering and keep sink IO off the business loop."""

from __future__ import annotations

import copy
import logging
import queue
import sys
import threading
from logging.handlers import QueueHandler


class BackgroundLogWriter:
    """A daemon sink owner with nonblocking admission and bounded shutdown."""

    def __init__(self, capacity: int = 512) -> None:
        self.queue: queue.Queue = queue.Queue(maxsize=capacity)
        self.dropped = 0
        self.sink_failures: dict[str, int] = {"emit": 0, "close": 0}
        self.diagnostic_failures = 0
        self._stopping = threading.Event()
        self._handlers: set[logging.Handler] = set()
        self._owners: dict[logging.Handler, int] = {}
        self._retired: set[logging.Handler] = set()
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run, name="worker-log-writer", daemon=True,
        )
        self._thread.start()

    def wrap(self, logger: logging.Logger) -> None:
        handlers = tuple(logger.handlers)
        if not handlers or all(isinstance(handler, _QueuedHandler) for handler in handlers):
            return
        targets = tuple(handler for handler in handlers if not isinstance(handler, _QueuedHandler))
        with self._lock:
            self._handlers.update(targets)
            for handler in targets:
                self._owners[handler] = self._owners.get(handler, 0) + 1
                self._retired.discard(handler)
        proxy = _QueuedHandler(self, targets)
        proxy.setLevel(min(handler.level for handler in targets))
        logger.handlers = [handler for handler in handlers if isinstance(handler, _QueuedHandler)] + [proxy]

    def submit(self, handlers: tuple[logging.Handler, ...], record: logging.LogRecord) -> None:
        with self._lock:
            if self._stopping.is_set():
                return
            try:
                self.queue.put_nowait((handlers, record))
            except queue.Full:
                # Falling back to synchronous emission would recreate the stall.
                self.dropped += 1

    def retire(self, handlers: tuple[logging.Handler, ...]) -> None:
        with self._lock:
            for handler in handlers:
                owners = self._owners.get(handler, 0) - 1
                if owners > 0:
                    self._owners[handler] = owners
                else:
                    self._owners.pop(handler, None)
                    self._retired.add(handler)

    def stop(self, timeout: float = 2.0) -> bool:
        with self._lock:
            self._stopping.set()
        self._thread.join(timeout=max(0.0, timeout))
        return not self._thread.is_alive()

    def _run(self) -> None:
        reported_drops = 0
        while not self._stopping.is_set() or not self.queue.empty():
            try:
                handlers, record = self.queue.get(timeout=0.05)
            except queue.Empty:
                self._close_retired()
                continue
            try:
                self._emit(handlers, record)
                if self.dropped > reported_drops:
                    summary = logging.LogRecord(
                        "jiuwenswarm.server.worker.logging", logging.WARNING,
                        __file__, 0, "Worker log queue full: dropped=%s", (self.dropped,), None,
                    )
                    reported_drops = self.dropped
                    self._emit(handlers, summary)
            finally:
                self.queue.task_done()
            if self.queue.empty():
                self._close_retired()
        with self._lock:
            self._retired.update(self._handlers)
        self._close_retired()

    def _emit(self, handlers: tuple[logging.Handler, ...], record: logging.LogRecord) -> None:
        for handler in handlers:
            if record.levelno >= handler.level:
                try:
                    handler.handle(record)
                except Exception as exc:
                    self._report_sink_failure("emit", handler, exc)

    def _report_sink_failure(self, operation: str, handler: logging.Handler, exc: Exception) -> None:
        count = self.sink_failures[operation] + 1
        self.sink_failures[operation] = count
        if count != 1 and count % 100:
            return
        # The logging queue may contain the failing handler. Use only safe
        # metadata on this background thread to avoid recursion or secret leaks.
        stream = sys.stderr
        if stream is None:
            self.diagnostic_failures += 1
            return
        try:
            stream.write(
                f"[WorkerLogging] sink failure operation={operation} "
                f"handler={type(handler).__name__} error={type(exc).__name__} count={count}\n"
            )
        except Exception:
            # Keep a visible counter even when the fallback stream is broken.
            self.diagnostic_failures += 1

    def _close_retired(self) -> None:
        with self._lock:
            retired = self._retired
            self._retired = set()
            self._handlers.difference_update(retired)
        for handler in retired:
            try:
                handler.close()
            except Exception as exc:
                self._report_sink_failure("close", handler, exc)


class _QueuedHandler(QueueHandler):
    def __init__(self, writer: BackgroundLogWriter, handlers: tuple[logging.Handler, ...]) -> None:
        super().__init__(writer.queue)
        self.writer = writer
        self.handlers = handlers
        self._retired = False

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        # QueueHandler normally formats tracebacks on the emitting thread.
        # This queue is process-local, so retain exception details for sinks.
        return copy.copy(record)

    def enqueue(self, record: logging.LogRecord) -> None:
        self.writer.submit(self.handlers, record)

    def close(self) -> None:
        if not self._retired:
            self._retired = True
            self.writer.retire(self.handlers)
        super().close()


_writer: BackgroundLogWriter | None = None


def enable_background_logging() -> None:
    """Enable only in the Runtime Worker entrypoint."""
    global _writer
    if _writer is None:
        _writer = BackgroundLogWriter()
    wrap_log_handlers()


def wrap_log_handlers(logger: logging.Logger | None = None) -> None:
    """Also wrap handlers installed by Runtime/core logging configuration."""
    if _writer is None:
        return
    if logger is not None:
        _writer.wrap(logger)
        return
    _writer.wrap(logging.getLogger())
    for candidate in list(logging.Logger.manager.loggerDict.values()):
        if isinstance(candidate, logging.Logger):
            _writer.wrap(candidate)


def stop_background_logging(timeout: float = 2.0) -> bool:
    """Return false if a sink is still blocked after the flush deadline."""
    if _writer is None:
        return True
    from jiuwenswarm.common.runtime_log_filter import flush_runtime_log_filter

    flush_runtime_log_filter()
    return _writer.stop(timeout)
