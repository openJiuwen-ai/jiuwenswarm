# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""UserVisibleTagFilter / LoggingTagConfig 测试。"""
import logging
from jiuwenswarm.common.utils import UserVisibleTagFilter, LoggingTagConfig


def _rec(uv=None):
    r = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
    if uv is not None:
        r.user_visible = uv
    return r


def test_critical_sets_user_tag():
    f = UserVisibleTagFilter(LoggingTagConfig())
    r = _rec("critical")
    assert f.filter(r) is True and r.user_tag == "[USER] "


def test_progress_sets_user_progress_tag():
    f = UserVisibleTagFilter(LoggingTagConfig())
    r = _rec("progress")
    assert f.filter(r) is True
    assert r.user_tag == "[USER_PROGRESS] "


def test_no_user_visible_sets_empty():
    f = UserVisibleTagFilter(LoggingTagConfig())
    r = _rec()
    assert f.filter(r) is True
    assert r.user_tag == ""


def test_unknown_value_sets_empty():
    f = UserVisibleTagFilter(LoggingTagConfig())
    r = _rec("unknown")
    assert f.filter(r) is True
    assert r.user_tag == ""


def test_config_disabled_user_visible():
    c = LoggingTagConfig()
    c.user_visible = False
    f = UserVisibleTagFilter(c)
    r = _rec("critical")
    assert f.filter(r) is True
    assert r.user_tag == ""  # 配置禁用 -> 空


def test_config_disabled_user_progress_visible():
    c = LoggingTagConfig()
    c.user_progress_visible = False
    f = UserVisibleTagFilter(c)
    r = _rec("progress")
    assert f.filter(r) is True
    assert r.user_tag == ""


def test_filter_does_not_modify_msg():
    f = UserVisibleTagFilter(LoggingTagConfig())
    r = _rec("critical")
    assert f.filter(r) is True
    assert r.msg == "msg"
