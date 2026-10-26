# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""多 handler 无重复标签测试。"""
import logging
from io import StringIO
from jiuwenswarm.common.utils import UserVisibleTagFilter, LoggingTagConfig


def test_multiple_handlers_one_tag_each():
    fmt = logging.Formatter("%(levelname)s %(user_tag)s%(name)s: %(message)s")
    streams = []
    logger = logging.getLogger("test_multi")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    for _ in range(3):
        s = StringIO()
        h = logging.StreamHandler(s)
        h.setFormatter(fmt)
        h.addFilter(UserVisibleTagFilter(LoggingTagConfig()))
        logger.addHandler(h)
        streams.append(s)
    logger.info("hi", extra={"user_visible": "critical"})
    for s in streams:
        out = s.getvalue()
        assert out.count("[USER]") == 1


def test_filter_sets_user_tag_once_per_record():
    f = UserVisibleTagFilter(LoggingTagConfig())
    r = logging.LogRecord("t", logging.INFO, "", 0, "m", (), None)
    r.user_visible = "critical"
    f.filter(r)
    first = r.user_tag
    f.filter(r)
    assert r.user_tag == first == "[USER] "
