# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exception logging must redact secrets without reading source files."""

import gc
import io
import linecache
import logging
import sys
import weakref

import pytest

from jiuwenswarm.common import utils
from jiuwenswarm.common.background_logging import BackgroundLogWriter


@pytest.fixture
def source_masking(monkeypatch):
    previous = logging.getLogRecordFactory()
    monkeypatch.setattr(utils, "_source_record_masking_installed", False)
    monkeypatch.setattr(utils, "_source_masking_failures", 0)
    logging.setLogRecordFactory(logging.LogRecord)
    utils.install_source_record_masking()
    try:
        yield
    finally:
        logging.setLogRecordFactory(previous)


@pytest.mark.parametrize("at_source", [False, True])
def test_exception_masking_never_looks_up_source(at_source, source_masking, monkeypatch):
    lookups = []

    def unexpected_lookup(*args, **kwargs):
        lookups.append(args)
        raise AssertionError("exception logging must not access linecache")

    with monkeypatch.context() as patch:
        for name in ("checkcache", "getline", "getlines", "lazycache", "updatecache"):
            patch.setattr(linecache, name, unexpected_lookup)
        try:
            raise ValueError("api_key=exception-private-value")
        except ValueError:
            info = sys.exc_info()
            factory = logging.getLogRecordFactory() if at_source else logging.LogRecord
            record = factory("third-party", logging.ERROR, __file__, 1, "failed", (), info)
            if not at_source:
                utils.SensitiveDataFilter().filter(record)

    assert lookups == []
    assert record.exc_info is None
    assert record.jiuwen_sensitive_sanitized
    output = logging.Formatter().format(record)
    assert "Traceback (most recent call last)" in output
    assert 'in test_exception_masking_never_looks_up_source' in output
    assert __file__ in output
    assert "ValueError:" in output
    assert "exception-private-value" not in output
    assert utils._source_masking_failures == 0


@pytest.mark.parametrize("background", [False, True])
def test_third_party_sinks_receive_redacted_exception_text(background, source_masking):
    output = io.StringIO()
    target = logging.Logger("third-party-exception", logging.INFO)
    target.handlers = [logging.StreamHandler(output)]
    writer = BackgroundLogWriter() if background else None
    if writer:
        writer.wrap(target)
    try:
        try:
            raise ValueError("api_key=sink-private-value")
        except ValueError:
            target.exception("failure token=%s", "message-private-value")
    finally:
        if writer:
            assert writer.stop()
    text = output.getvalue()
    assert "ValueError:" in text
    assert "sink-private-value" not in text
    assert "message-private-value" not in text
    assert "******" in text


@pytest.mark.parametrize("relationship", ["cause", "context", "suppressed"])
def test_exception_chains_keep_order_and_context_suppression(relationship, source_masking):
    try:
        try:
            raise ValueError("api_key=inner-private-value")
        except ValueError as inner:
            if relationship == "cause":
                raise RuntimeError("token=outer-private-value") from inner
            if relationship == "suppressed":
                raise RuntimeError("token=outer-private-value") from None
            raise RuntimeError("token=outer-private-value")
    except RuntimeError:
        record = logging.getLogRecordFactory()(
            "third-party", logging.ERROR, __file__, 1, "failed", (), sys.exc_info(),
        )
    text = record.exc_text
    assert "RuntimeError:" in text
    assert "outer-private-value" not in text
    assert "inner-private-value" not in text
    if relationship == "suppressed":
        assert "ValueError:" not in text
    else:
        assert text.index("ValueError:") < text.index("RuntimeError:")
        separator = "direct cause" if relationship == "cause" else "During handling"
        assert separator in text


def test_exception_groups_and_notes_are_redacted_without_source(source_masking, monkeypatch):
    def unexpected_lookup(*args, **kwargs):
        raise AssertionError("exception group logging must not access linecache")

    try:
        try:
            raise ValueError("api_key=group-private-value")
        except ValueError as inner:
            inner.add_note("token=note-private-value")
            raise ExceptionGroup("group failed", [inner, TypeError("other child")]) from None
    except ExceptionGroup:
        with monkeypatch.context() as patch:
            patch.setattr(linecache, "checkcache", unexpected_lookup)
            patch.setattr(linecache, "getline", unexpected_lookup)
            record = logging.getLogRecordFactory()(
                "third-party", logging.ERROR, __file__, 1, "failed", (), sys.exc_info(),
            )
    assert record.exc_info is None
    assert "ExceptionGroup: group failed" in record.exc_text
    assert "ValueError:" in record.exc_text
    assert "TypeError: other child" in record.exc_text
    assert "group-private-value" not in record.exc_text
    assert "note-private-value" not in record.exc_text
    assert utils._source_masking_failures == 0


def test_record_uses_explicit_traceback_in_exception_info(source_masking):
    try:
        raise ValueError("original traceback")
    except ValueError:
        info = sys.exc_info()
    info[1].__traceback__ = None
    record = logging.getLogRecordFactory()(
        "third-party", logging.ERROR, __file__, 1, "failed", (), info,
    )
    assert "in test_record_uses_explicit_traceback_in_exception_info" in record.exc_text
    assert "ValueError: original traceback" in record.exc_text


def test_empty_exception_info_retains_none_type_text(source_masking):
    record = logging.getLogRecordFactory()(
        "third-party", logging.ERROR, __file__, 1, "failed", (), (None, None, None),
    )
    assert record.exc_info is None
    assert record.exc_text == "NoneType: None\n"


def test_preformatted_exception_text_is_still_redacted(source_masking):
    record = logging.LogRecord("third-party", logging.ERROR, __file__, 1, "failed", (), None)
    record.exc_text = "ValueError: api_key=preformatted-private-value"
    assert utils.SensitiveDataFilter().filter(record)
    assert "preformatted-private-value" not in record.exc_text
    assert "ValueError:" in record.exc_text


def test_masked_record_does_not_retain_exception_locals(source_masking):
    class Payload:
        pass

    def create_record():
        payload = Payload()
        reference = weakref.ref(payload)
        try:
            raise ValueError("test exception")
        except ValueError:
            record = logging.getLogRecordFactory()(
                "third-party", logging.ERROR, __file__, 1, "failed", (), sys.exc_info(),
            )
        return record, reference

    record, reference = create_record()
    gc.collect()
    assert reference() is None
    assert record.exc_info is None
    assert "ValueError: test exception" in record.exc_text


def test_preformatted_exception_releases_raw_frames():
    try:
        raise ValueError("raw exception")
    except ValueError:
        record = logging.LogRecord("third-party", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    record.exc_text = "ValueError: api_key=preformatted-private-value"
    assert utils.SensitiveDataFilter().filter(record)
    assert record.exc_info is None
    assert "preformatted-private-value" not in record.exc_text


def test_exception_rendering_bounds_deep_chains_groups_and_total_text():
    from jiuwenswarm.common.log_exception import format_log_exception

    root = ValueError("first")
    for index in range(100):
        outer = RuntimeError(f"chain {index}")
        outer.__cause__ = root
        root = outer
    text = format_log_exception((type(root), root, None))
    assert "additional exceptions omitted" in text
    assert "RuntimeError: chain 99" in text
    assert len(text) < 64 * 1024

    group = ExceptionGroup("wide group", [ValueError(f"member {index}") for index in range(100)])
    text = format_log_exception((type(group), group, None))
    assert "additional group exceptions omitted" in text
    assert "ValueError: member 0" in text
    assert "ValueError: member 99" not in text

    group = ExceptionGroup("large group", [ValueError("x" * 20_000) for _ in range(10)])
    text = format_log_exception((type(group), group, None))
    assert len(text) < 64 * 1024
    assert "exception diagnostics omitted" in text


def test_exception_rendering_handles_cycles_and_failed_str(source_masking):
    class BrokenError(Exception):
        def __str__(self):
            raise RuntimeError("api_key=must-not-leak")

    exc = BrokenError()
    exc.__cause__ = exc
    record = logging.getLogRecordFactory()(
        "third-party", logging.ERROR, __file__, 1, "failed", (), (type(exc), exc, None),
    )
    assert record.exc_info is None
    assert "BrokenError: <exception str() failed>" in record.exc_text
    assert "must-not-leak" not in record.exc_text
    assert utils._source_masking_failures == 0


def test_exception_rendering_bounds_frames_notes_and_large_messages():
    from jiuwenswarm.common.log_exception import format_log_exception

    def recurse(depth):
        if depth:
            recurse(depth - 1)
        else:
            raise ValueError("deep stack")

    try:
        recurse(100)
    except ValueError:
        text = format_log_exception(sys.exc_info())
    assert text.count('  File "') == 64
    assert "additional frames omitted" in text

    exc = ValueError("x" * 70_000)
    for index in range(20):
        exc.add_note(f"note {index}")
    text = format_log_exception((type(exc), exc, None))
    assert "exception text omitted" in text
    assert "additional exception notes omitted" in text
    assert "note 0\n" in text
    assert "note 19\n" not in text


def test_reused_group_members_are_rendered_and_counted():
    from jiuwenswarm.common.log_exception import format_log_exception

    child = ValueError("reused member")
    group = ExceptionGroup("shared children", [child, child])
    text = format_log_exception((type(group), group, None))
    assert text.count("ValueError: reused member") == 2

    for _ in range(5):
        group = ExceptionGroup("shared nested groups", [group] * 15)
    text = format_log_exception((type(group), group, None))
    assert "additional exceptions omitted" in text
    assert len(text) < 64 * 1024


@pytest.mark.parametrize("character", ["x", "汉", "😀"])
@pytest.mark.parametrize("extra_bytes", [-1, 0, 1])
def test_exception_utf8_budget_includes_type_and_newline(character, extra_bytes):
    from jiuwenswarm.common.log_exception import format_log_exception

    prefix = "ValueError: "
    message_bytes = 64 * 1024 - len((prefix + "\n").encode("utf-8")) + extra_bytes
    width = len(character.encode("utf-8"))
    message = character * (message_bytes // width) + "x" * (message_bytes % width)
    exc = ValueError(message)
    text = format_log_exception((type(exc), exc, None))
    assert len(text.encode("utf-8")) <= 64 * 1024
    assert utils._sanitize_record_text(text) == text
    if extra_bytes <= 0:
        assert text == prefix + message + "\n"
        assert len(text.encode("utf-8")) == 64 * 1024 + extra_bytes
    else:
        assert "exception diagnostics omitted" in text


@pytest.mark.parametrize("character,count", [("汉", 30_000), ("😀", 20_000)])
def test_oversized_utf8_exception_keeps_type_and_redaction(character, count, source_masking):
    exc = ValueError(character * count + " api_key=multibyte-private-value")
    record = logging.getLogRecordFactory()(
        "third-party", logging.ERROR, __file__, 1, "failed", (), (type(exc), exc, None),
    )
    assert record.exc_info is None
    assert "ValueError:" in record.exc_text
    assert "exception text omitted" in record.exc_text
    assert "multibyte-private-value" not in record.exc_text
    assert not record.exc_text.startswith("[log payload omitted:")
    assert len(record.exc_text.encode("utf-8")) <= 64 * 1024
    assert utils._source_masking_failures == 0


@pytest.mark.parametrize("character", ["汉", "😀"])
@pytest.mark.parametrize("structure", ["chain", "notes", "group"])
def test_exception_utf8_budget_counts_all_diagnostic_parts(character, structure):
    from jiuwenswarm.common.log_exception import format_log_exception

    # Each piece fits; the assembled diagnostic exceeds the UTF-8 byte budget.
    piece = character * 12_000
    assert len(piece.encode("utf-8")) < 64 * 1024
    if structure == "chain":
        exc = RuntimeError(piece)
        exc.__cause__ = ValueError(piece)
    elif structure == "notes":
        exc = ValueError("noted error")
        exc.add_note(piece)
        exc.add_note(piece)
    else:
        exc = ExceptionGroup("group", [ValueError(piece), RuntimeError(piece)])
    text = format_log_exception((type(exc), exc, None))
    assert "exception diagnostics omitted" in text
    assert len(text.encode("utf-8")) <= 64 * 1024
    assert utils._sanitize_record_text(text) == text
