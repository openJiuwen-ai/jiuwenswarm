# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Slow sinks must not block log producers, exhaust memory, or prevent exit."""

import io
import logging
import threading
import time

import pytest

from jiuwenswarm.common.background_logging import BackgroundLogWriter


def test_blocked_sink_has_bounded_queue_and_shutdown():
    started = threading.Event()
    release = threading.Event()
    messages = []

    class SlowHandler(logging.Handler):
        def emit(self, record):
            started.set()
            release.wait(5)
            messages.append(record.getMessage())

    writer = BackgroundLogWriter(capacity=2)
    target = logging.Logger("slow-test", logging.INFO)
    target.addHandler(SlowHandler())
    writer.wrap(target)
    try:
        target.info("first")
        assert started.wait(1)
        began = time.monotonic()
        for index in range(1000):
            target.info("queued %s", index)
        assert time.monotonic() - began < 1
        assert writer.queue.qsize() == 2
        assert writer.dropped == 998
        assert not writer.stop(timeout=0.02)
    finally:
        release.set()
        assert writer.stop(timeout=2)
    assert messages == ["first", "Worker log queue full: dropped=998", "queued 0", "queued 1"]


def test_background_fanout_preserves_levels_filters_and_tracebacks():
    output = io.StringIO()
    info_output = io.StringIO()
    warning_handler = logging.StreamHandler(output)
    warning_handler.setLevel(logging.WARNING)
    info_handler = logging.StreamHandler(info_output)
    info_handler.addFilter(lambda record: "excluded" not in record.getMessage())
    target = logging.Logger("fanout-test", logging.INFO)
    target.handlers = [warning_handler, info_handler]
    writer = BackgroundLogWriter()
    writer.wrap(target)
    try:
        target.info("info")
        target.info("excluded")
        try:
            raise ValueError("test traceback")
        except ValueError:
            target.exception("failed")
    finally:
        assert writer.stop(timeout=2)
    assert "info\n" not in output.getvalue()
    assert "ValueError: test traceback" in output.getvalue()
    assert "info\n" in info_output.getvalue()
    assert "excluded" not in info_output.getvalue()


def test_reconfigured_logger_drains_before_closing_old_sink():
    class Sink(logging.Handler):
        def __init__(self):
            super().__init__()
            self.messages = []
            self.was_closed = False

        def emit(self, record):
            assert not self.was_closed
            self.messages.append(record.getMessage())

        def close(self):
            self.was_closed = True
            super().close()

    sink = Sink()
    target = logging.Logger("reload-test", logging.INFO)
    target.handlers = [sink]
    writer = BackgroundLogWriter()
    writer.wrap(target)
    target.info("before reload")
    target.handlers[0].close()
    target.handlers = []
    assert writer.stop(timeout=2)
    assert sink.messages == ["before reload"]
    assert sink.was_closed


def test_shared_sink_is_kept_until_all_loggers_retire():
    output = io.StringIO()
    sink = logging.StreamHandler(output)
    first = logging.Logger("first", logging.INFO)
    second = logging.Logger("second", logging.INFO)
    first.handlers = [sink]
    second.handlers = [sink]
    writer = BackgroundLogWriter()
    writer.wrap(first)
    writer.wrap(second)
    first.handlers[0].close()
    first.handlers[0].close()
    second.info("still owned")
    assert writer.stop(timeout=2)
    assert "still owned" in output.getvalue()


def test_worker_flushes_repeat_counts_before_stopping_queue(monkeypatch):
    from jiuwenswarm.common import background_logging, runtime_log_filter

    output = io.StringIO()
    logger = logging.getLogger("team")
    limiter = runtime_log_filter.RuntimeLogFilter()
    monkeypatch.setattr(logger, "handlers", [logging.StreamHandler(output)])
    monkeypatch.setattr(logger, "filters", [limiter])
    monkeypatch.setattr(logger, "level", logging.INFO)
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(runtime_log_filter, "_FILTER", limiter)
    writer = BackgroundLogWriter()
    monkeypatch.setattr(background_logging, "_writer", writer)
    writer.wrap(logger)
    message = "DbSessions.read() exceeded the 30s watchdog"
    for _ in range(2):
        logger.error(message)
    assert background_logging.stop_background_logging()
    assert "total=2 suppressed=1" in output.getvalue()


def test_failed_emit_reports_counts_without_stopping_healthy_sinks(capsys):
    class BrokenHandler(logging.Handler):
        def emit(self, record):
            raise OSError("api_key=secret-in-exception")

    output = io.StringIO()
    target = logging.Logger("broken-sink", logging.INFO)
    target.handlers = [BrokenHandler(), logging.StreamHandler(output)]
    writer = BackgroundLogWriter()
    writer.wrap(target)
    for index in range(101):
        target.info("record %s", index)
    assert writer.stop(timeout=2)
    diagnostic = capsys.readouterr().err
    assert writer.sink_failures == {"emit": 101, "close": 0}
    assert len(output.getvalue().splitlines()) == 101
    assert "operation=emit handler=BrokenHandler error=OSError count=1\n" in diagnostic
    assert "count=100\n" in diagnostic
    assert len(diagnostic.splitlines()) == 2
    assert "secret-in-exception" not in diagnostic


def test_failed_overflow_summary_and_close_are_reported(capsys):
    started = threading.Event()
    release = threading.Event()
    output = io.StringIO()

    class BrokenSummaryHandler(logging.Handler):
        def emit(self, record):
            if "Worker log queue full" in record.getMessage():
                raise OSError("summary failed")
            started.set()
            release.wait(5)

        def close(self):
            super().close()
            raise OSError("close failed")

    target = logging.Logger("broken-summary", logging.INFO)
    target.handlers = [BrokenSummaryHandler(), logging.StreamHandler(output)]
    writer = BackgroundLogWriter(capacity=1)
    writer.wrap(target)
    try:
        target.info("first")
        assert started.wait(1)
        target.info("second")
        target.info("dropped")
    finally:
        release.set()
        assert writer.stop(timeout=2)
    diagnostic = capsys.readouterr().err
    assert writer.dropped == 1
    assert writer.sink_failures == {"emit": 1, "close": 1}
    assert "operation=emit" in diagnostic
    assert "operation=close" in diagnostic
    assert output.getvalue().splitlines() == ["first", "Worker log queue full: dropped=1", "second"]


@pytest.mark.parametrize("missing_stream", [False, True])
def test_unavailable_diagnostic_stream_does_not_stop_fanout(monkeypatch, missing_stream):
    from jiuwenswarm.common import background_logging

    class BrokenHandler(logging.Handler):
        def emit(self, record):
            raise OSError("emit failed")

    class BrokenStderr:
        def write(self, text):
            raise OSError("stderr failed")

    output = io.StringIO()
    target = logging.Logger("broken-stderr", logging.INFO)
    target.handlers = [BrokenHandler(), logging.StreamHandler(output)]
    writer = BackgroundLogWriter()
    writer.wrap(target)
    with monkeypatch.context() as context:
        context.setattr(background_logging.sys, "stderr", None if missing_stream else BrokenStderr())
        target.info("survives")
        assert writer.stop(timeout=2)
    assert output.getvalue() == "survives\n"
    assert writer.sink_failures["emit"] == writer.diagnostic_failures == 1
