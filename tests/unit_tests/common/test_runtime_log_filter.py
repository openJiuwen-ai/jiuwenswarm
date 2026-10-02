# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression coverage for bounded runtime diagnostics."""

import io
import logging
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from jiuwenswarm.common import runtime_log_filter


def _record(message, *, name="team", level=logging.ERROR, line=10, exc_info=None):
    return logging.LogRecord(name, level, "engine.py", line, message, (), exc_info)


_POOL_ERROR = "QueuePool limit of size 8 overflow 0 reached, connection timed out, timeout 10.00"


def test_first_traceback_and_periodic_counts_are_preserved(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(runtime_log_filter.time, "monotonic", lambda: now[0])
    limiter = runtime_log_filter.RuntimeLogFilter()
    try:
        raise TimeoutError(_POOL_ERROR)
    except TimeoutError:
        first = _record(_POOL_ERROR, exc_info=sys.exc_info())
    assert limiter.filter(first)
    assert "Traceback" in logging.Formatter().format(first)
    for _ in range(100):
        assert not limiter.filter(_record(_POOL_ERROR))
    now[0] = 60.0
    summary = _record(_POOL_ERROR, exc_info=first.exc_info)
    assert limiter.filter(summary)
    assert _POOL_ERROR in summary.getMessage()
    assert "total=102 suppressed=100" in summary.getMessage()
    assert summary.exc_info is first.exc_info
    assert "Traceback" in logging.Formatter().format(summary)


def test_new_incident_after_quiet_period_keeps_current_context(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(runtime_log_filter.time, "monotonic", lambda: now[0])
    limiter = runtime_log_filter.RuntimeLogFilter()
    assert limiter.filter(_record("DbSessions.read() exceeded for session-a"))
    assert not limiter.filter(_record("DbSessions.read() exceeded for session-b"))

    now[0] = 3600.0
    current = _record("DbSessions.read() exceeded for session-c")
    assert limiter.filter(current)
    assert "session-c" in current.getMessage()
    assert "suppressed=1" in current.getMessage()


def test_short_burst_flushes_counts_once(caplog):
    limiter = runtime_log_filter.RuntimeLogFilter()
    with caplog.at_level(logging.WARNING, logger="team"):
        assert limiter.filter(_record(_POOL_ERROR))
        assert not limiter.filter(_record(_POOL_ERROR))
        limiter.flush()
        limiter.flush()
    assert len(caplog.records) == 1
    assert "total=2 suppressed=1" in caplog.text


def test_new_source_and_unrelated_errors_are_not_hidden():
    limiter = runtime_log_filter.RuntimeLogFilter()
    assert limiter.filter(_record(_POOL_ERROR))
    assert limiter.filter(_record(_POOL_ERROR, line=11))
    assert limiter.filter(_record(_POOL_ERROR, name="common"))
    for _ in range(3):
        assert limiter.filter(_record("unexpected programming error"))
        assert limiter.filter(_record("query returned no rows", level=logging.INFO))


@pytest.mark.parametrize("message", [
    "DbSessions.read() exceeded the 30s watchdog; the DB driver may be wedged",
    "DbSessions.write() exceeded the 30s watchdog; the DB driver may be wedged",
    "create_cur_session_tables hit a transient DB error (TimeoutError); retrying in 0.5s (attempt 1)",
    "BrowserService heartbeat: connection unhealthy — Playwright MCP subprocess not responding",
    "BrowserService heartbeat: restart deferred until the next browser task",
])
def test_known_repeating_diagnostics_are_limited(message):
    limiter = runtime_log_filter.RuntimeLogFilter()
    level = logging.INFO if "restart deferred" in message else logging.WARNING
    assert limiter.filter(_record(message, name="browser_agent", level=level))
    assert not limiter.filter(_record(message, name="browser_agent", level=level))


def test_normal_logs_do_not_format_or_read_clock(monkeypatch):
    def unexpected(*_args):
        raise AssertionError("normal logs must not enter the rate limiter")

    monkeypatch.setattr(runtime_log_filter.time, "monotonic", unexpected)
    for name in ("team", "browser_agent"):
        record = _record("ordinary request", name=name, level=logging.INFO)
        monkeypatch.setattr(record, "getMessage", unexpected)
        assert runtime_log_filter.RuntimeLogFilter().filter(record)


def test_installation_is_idempotent_and_filters_before_handler_fanout(monkeypatch):
    logger = logging.getLogger("team")
    monkeypatch.setattr(logger, "filters", [])
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(runtime_log_filter, "_FILTER", runtime_log_filter.RuntimeLogFilter())
    # Isolate all installation targets so this test leaves no filters behind.
    original_get_logger = logging.getLogger
    monkeypatch.setattr(runtime_log_filter.logging, "getLogger", lambda _name: logger)
    runtime_log_filter.install_runtime_log_filter()
    runtime_log_filter.install_runtime_log_filter()
    assert len(logger.filters) == 1
    seen = [[], []]
    for output in seen:
        handler = logging.Handler()
        handler.emit = output.append
        logger.addHandler(handler)
    logger.handle(_record(_POOL_ERROR))
    logger.handle(_record(_POOL_ERROR))
    assert [len(output) for output in seen] == [1, 1]
    monkeypatch.setattr(runtime_log_filter.logging, "getLogger", original_get_logger)


def test_concurrent_repeats_have_exact_counts(caplog):
    limiter = runtime_log_filter.RuntimeLogFilter()
    with ThreadPoolExecutor(max_workers=4) as executor:
        emitted = list(executor.map(lambda _: limiter.filter(_record(_POOL_ERROR)), range(100)))
    assert sum(emitted) == 1
    with caplog.at_level(logging.ERROR, logger="team"):
        limiter.flush()
    assert "total=100 suppressed=99" in caplog.text


def test_exit_flush_skips_closed_console_and_preserves_file_summary(monkeypatch):
    logger = logging.getLogger("team")
    closed = io.StringIO()
    closed.close()
    live = io.StringIO()
    monkeypatch.setattr(logger, "handlers", [logging.StreamHandler(closed), logging.StreamHandler(live)])
    monkeypatch.setattr(logger, "propagate", False)
    limiter = runtime_log_filter.RuntimeLogFilter()
    assert limiter.filter(_record(_POOL_ERROR))
    assert not limiter.filter(_record(_POOL_ERROR))
    limiter.flush()
    assert "total=2 suppressed=1" in live.getvalue()
