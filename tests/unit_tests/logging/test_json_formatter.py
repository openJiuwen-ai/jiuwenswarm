# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""JsonUserVisibleFormatter 测试。"""
import json
import logging
from unittest.mock import patch

from jiuwenswarm.common.utils import JsonUserVisibleFormatter


def _make_record(name="test.logger", lineno=88, msg="测试消息", user_visible=None):
    rec = logging.LogRecord(name, logging.INFO, "", lineno, msg, (), None)
    if user_visible is not None:
        rec.user_visible = user_visible
    return rec


def test_json_includes_process_and_lineno():
    with patch("os.getpid", return_value=12345):
        out = JsonUserVisibleFormatter().format(_make_record(lineno=88))
        obj = json.loads(out)
        assert obj["process"] == 12345
        assert obj["lineno"] == 88


def test_json_field_order():
    with patch("os.getpid", return_value=12345):
        out = JsonUserVisibleFormatter().format(_make_record(lineno=125))
        positions = [out.find(k) for k in ('"timestamp"', '"process"', '"session_id"', '"level"', '"logger"', '"lineno"', '"message"')]
        assert positions == sorted(positions)


def test_json_identity_fields_present_even_when_none():
    rec = _make_record()
    obj = json.loads(JsonUserVisibleFormatter().format(rec))
    assert "user_id" in obj and obj["user_id"] is None
    assert "domain_id" in obj and obj["domain_id"] is None
    assert "app_id" in obj and obj["app_id"] is None
    assert obj["session_id"] == "<nosid>"


def test_json_component_derived():
    rec = logging.LogRecord("jiuwenswarm.gateway.routing.agent_client", logging.INFO, "", 1, "x", (), None)
    obj = json.loads(JsonUserVisibleFormatter().format(rec))
    assert obj["component"] == "gateway"


def test_json_timestamp_has_milliseconds():
    import re
    out = JsonUserVisibleFormatter().format(_make_record())
    obj = json.loads(out)
    # text 格式时间戳应含毫秒: YYYY-MM-DD HH:MM:SS.mmm
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}$", obj["timestamp"]), obj["timestamp"]
