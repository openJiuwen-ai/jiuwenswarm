# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Logging failures must not expose raw exceptions or retain their frames."""

import gc
import linecache
import logging
import sys
import threading
import types
import weakref

import pytest

from jiuwenswarm.common import utils
from jiuwenswarm.common.background_logging import BackgroundLogWriter
from jiuwenswarm.common.log_exception import format_log_exception


@pytest.fixture
def record_maker(monkeypatch):
    previous = logging.getLogRecordFactory()
    monkeypatch.setattr(utils, "_source_record_masking_installed", False)
    monkeypatch.setattr(utils, "_source_masking_failures", 0)
    logging.setLogRecordFactory(logging.LogRecord)
    utils.install_source_record_masking()

    def make(at_source, info, message="failure", arguments=(), preformatted=None):
        if at_source and preformatted is not None:
            def preset_factory(*args, **kwargs):
                record = logging.LogRecord(*args, **kwargs)
                record.exc_text = preformatted
                return record

            logging.setLogRecordFactory(preset_factory)
            utils._source_record_masking_installed = False
            utils.install_source_record_masking()
        factory = logging.getLogRecordFactory() if at_source else logging.LogRecord
        record = factory("third-party", logging.ERROR, __file__, 1, message, arguments, info)
        if not at_source:
            if preformatted is not None:
                record.exc_text = preformatted
            utils.SensitiveDataFilter().filter(record)
        return record

    try:
        yield make
    finally:
        logging.setLogRecordFactory(previous)


@pytest.fixture
def forbid_source(monkeypatch):
    calls = []

    def unexpected(*args, **kwargs):
        calls.append(True)
        raise AssertionError("exception logging accessed source")

    with monkeypatch.context() as patch:
        for name in ("checkcache", "getlines", "getline", "lazycache", "updatecache"):
            patch.setattr(linecache, name, unexpected)
        yield calls


class BadNotesError(ValueError):
    @property
    def __notes__(self):
        raise RuntimeError("token=notes-private-value")


@pytest.mark.parametrize("at_source", [False, True])
def test_bad_notes_are_omitted_without_raw_fallback(at_source, record_maker, forbid_source):
    exc = BadNotesError("api_key=exception-private-value")
    record = record_maker(at_source, (type(exc), exc, None))
    text = logging.Formatter().format(record)
    assert record.exc_info is None
    assert record.jiuwen_sensitive_sanitized
    assert "BadNotesError:" in text and "notes omitted" in text
    assert "exception-private-value" not in text and "notes-private-value" not in text
    assert not forbid_source


@pytest.mark.parametrize("at_source", [False, True])
@pytest.mark.parametrize("failure", ["format", "redact", "preformatted"])
def test_exception_failures_emit_safe_summary(at_source, failure, record_maker, forbid_source, monkeypatch):
    def failed(*args, **kwargs):
        raise RuntimeError("token=failure-private-value")

    if failure == "format":
        monkeypatch.setattr(utils, "format_log_exception", failed)
    else:
        original = utils._sanitize_record_text

        def redact(text):
            if "ValueError:" in text:
                failed()
            return original(text)

        monkeypatch.setattr(utils, "_sanitize_record_text", redact)
    exc = ValueError("api_key=exception-private-value")
    preformatted = "ValueError: api_key=preformatted-private-value" if failure == "preformatted" else None
    record = record_maker(at_source, (type(exc), exc, None), preformatted=preformatted)
    text = logging.Formatter().format(record)
    assert record.exc_info is None
    assert record.jiuwen_sensitive_sanitized
    assert "exception diagnostics omitted" in text
    assert all(secret not in text for secret in (
        "exception-private-value", "failure-private-value", "preformatted-private-value",
    ))
    assert not forbid_source
    assert utils._source_masking_failures == int(at_source)


@pytest.mark.parametrize("at_source", [False, True])
@pytest.mark.parametrize("failure", ["format", "redact"])
def test_message_failure_still_sanitizes_exception(at_source, failure, record_maker, monkeypatch):
    message, arguments = "%s %s", ("message-private-value",)
    if failure == "redact":
        message, arguments = "token=message-private-value", ()
        original = utils._sanitize_record_text

        def redact(text):
            if text == message:
                raise ValueError("token=redactor-private-value")
            return original(text)

        monkeypatch.setattr(utils, "_sanitize_record_text", redact)
    exc = ValueError("api_key=exception-private-value")
    record = record_maker(at_source, (type(exc), exc, None), message, arguments)
    text = logging.Formatter().format(record)
    assert record.args == () and record.exc_info is None
    assert "log message omitted" in text and "ValueError:" in text
    assert "message-private-value" not in text and "exception-private-value" not in text
    assert utils._source_masking_failures == int(at_source)


@pytest.mark.parametrize("failure", ["notes", "format", "message"])
def test_failed_records_release_locals_before_background_sink(failure, record_maker, monkeypatch):
    if failure == "format":
        def failed(*args, **kwargs):
            raise RuntimeError("token=formatter-private-value")

        monkeypatch.setattr(utils, "format_log_exception", failed)
    started, release = threading.Event(), threading.Event()
    output = []
    raw_frames = []

    class Sink(logging.Handler):
        def emit(self, record):
            raw_frames.append(record.exc_info is not None)
            started.set()
            assert release.wait(5)
            output.append(logging.Formatter().format(record))

    class Payload:
        pass

    target = logging.Logger("background-failure", logging.INFO)
    target.handlers = [Sink()]
    writer = BackgroundLogWriter(capacity=2)
    writer.wrap(target)

    def produce():
        payload = Payload()
        payload.data = bytearray(1024 * 1024)
        reference = weakref.ref(payload)
        try:
            raise BadNotesError("api_key=exception-private-value")
        except BadNotesError:
            if failure == "message":
                target.error("%s %s", "message-private-value", exc_info=True)
            else:
                target.exception("failure")
        return reference

    try:
        reference = produce()
        assert started.wait(1)
        gc.collect()
        assert reference() is None
    finally:
        release.set()
        assert writer.stop(timeout=3)
    assert raw_frames == [False]
    assert len(output) == 1
    assert "private-value" not in output[0]


def test_deep_traceback_keeps_innermost_failure_location():
    def crash_site():
        raise ValueError("origin")

    def recurse(depth):
        if depth:
            recurse(depth - 1)
        else:
            crash_site()

    try:
        recurse(100)
    except ValueError:
        text = format_log_exception(sys.exc_info())
    assert text.count('  File "') == 64
    assert "additional frames omitted" in text
    assert "in crash_site\n" in text and "ValueError: origin" in text


def test_extreme_traceback_has_a_scan_limit():
    frame = sys._getframe()
    tb = None
    try:
        for _ in range(5000):
            tb = types.TracebackType(tb, frame, frame.f_lasti, frame.f_lineno)
        exc = ValueError("bounded scan")
        text = format_log_exception((type(exc), exc, tb))
        assert "traceback scan limit" in text
        assert text.count('  File "') == 64
        assert len(text.encode("utf-8")) <= 64 * 1024
    finally:
        del frame, tb


@pytest.mark.parametrize("at_source", [False, True])
def test_redaction_cannot_expand_exception_past_byte_budget(at_source, record_maker):
    exc = ValueError("token=x " * 7000)
    record = record_maker(at_source, (type(exc), exc, None))
    assert record.exc_info is None
    assert len(record.exc_text.encode("utf-8")) <= 64 * 1024
    assert "exception diagnostics omitted" in record.exc_text


@pytest.mark.parametrize("at_source", [False, True])
@pytest.mark.parametrize("control", [KeyboardInterrupt, SystemExit])
def test_exception_logging_preserves_process_control(at_source, control, record_maker):
    class ControlledError(ValueError):
        @property
        def __notes__(self):
            raise control()

    exc = ControlledError("control flow")
    with pytest.raises(control):
        record_maker(at_source, (type(exc), exc, None))
