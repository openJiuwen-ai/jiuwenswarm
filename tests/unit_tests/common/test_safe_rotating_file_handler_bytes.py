"""SafeRotatingFileHandler.shouldRollover measures bytes, not code points."""

import logging

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
