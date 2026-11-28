"""SafeRotatingFileHandler.shouldRollover measures bytes, not code points."""

import logging
from pathlib import Path

from jiuwenswarm.common.utils import SafeRotatingFileHandler


def _make_handler(path: str, max_bytes: int) -> SafeRotatingFileHandler:
    handler = SafeRotatingFileHandler(path, maxBytes=max_bytes, backupCount=1)
    handler.setLevel(logging.INFO)
    return handler


def test_multibyte_message_triggers_rollover_by_byte_size(tmp_path):
    # 60 CJK chars = 60 code points but 180 UTF-8 bytes
    handler = _make_handler(str(tmp_path / "app.log"), max_bytes=100)
    try:
        handler.stream.write("")
        record = logging.LogRecord(
            "t", logging.INFO, __file__, 1, "汉" * 60, None, None
        )
        # code-point counting (old behavior) sees 60 < 100 and misses rollover
        assert handler.shouldRollover(record) is True
    finally:
        handler.close()


def test_ascii_message_keeps_code_point_behavior(tmp_path):
    handler = _make_handler(str(tmp_path / "app.log"), max_bytes=100)
    try:
        handler.stream.write("")
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "a" * 10, None, None)
        assert handler.shouldRollover(record) is False
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "a" * 200, None, None)
        assert handler.shouldRollover(record) is True
    finally:
        handler.close()


def test_zero_max_bytes_disables_rollover(tmp_path):
    handler = _make_handler(str(tmp_path / "app.log"), max_bytes=0)
    try:
        handler.stream.write("")
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "a" * 10_000, None, None)
        assert handler.shouldRollover(record) is False
    finally:
        handler.close()


def test_delayed_stream_does_not_roll_and_first_write_lands(tmp_path):
    # delay=True keeps self.stream None until the first emit, so
    # shouldRollover must return False without touching any stream.
    handler = SafeRotatingFileHandler(
        str(tmp_path / "app.log"), maxBytes=100, backupCount=1, delay=True
    )
    handler.setLevel(logging.INFO)
    try:
        assert handler.stream is None
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "a" * 200, None, None)
        assert handler.shouldRollover(record) is False
        handler.emit(record)
        assert Path(handler.baseFilename).read_text(encoding="utf-8") == "a" * 200 + "\n"
    finally:
        handler.close()


def test_code_point_bound_short_circuits_before_encoding(tmp_path):
    # The code-point bound alone exceeds the budget: no encode needed.
    handler = _make_handler(str(tmp_path / "app.log"), max_bytes=10)
    try:
        handler.stream.write("")
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "汉" * 20, None, None)
        # 20 code points already >= 10, so it rolls even though the byte
        # measurement (which the short circuit skips) would also roll.
        assert handler.shouldRollover(record) is True
    finally:
        handler.close()
