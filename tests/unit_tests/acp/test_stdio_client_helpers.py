# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from jiuwenswarm.acp.stdio_client import _consume_one_json, _extract_session_update_text


def test_consume_one_json_multiline_whitespace_between():
    buf = '{"a": 1}\n\n{"jsonrpc":"2.0","id":1}\n'
    first, buf2 = _consume_one_json(buf)
    assert first == {"a": 1}
    second, buf3 = _consume_one_json(buf2)
    assert second == {"jsonrpc": "2.0", "id": 1}
    assert buf3 == ""


def test_consume_one_json_incomplete_then_complete():
    part1 = '{\n  "method":'
    obj, tail = _consume_one_json(part1)
    assert obj is None and tail == part1
    buf = part1 + ' "session/update"\n}\n{"x":true}'
    obj, tail = _consume_one_json(buf)
    assert obj == {"method": "session/update"}
    obj2, tail2 = _consume_one_json(tail)
    assert obj2 == {"x": True}
    assert tail2 == ""


def test_extract_session_update_text_accepts_nested_update():
    msg = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "messageId": "m1",
                "content": {"type": "text", "text": "hello"},
            },
        },
    }
    assert _extract_session_update_text(msg) == "hello"
