"""Shared trace contracts are independent of the Environment HTTP transport."""
from jiuwenswarm.extensions.agentos.agentos_router.backend import (
    TRACE_ID_HEADER, normalize_trace_id, extract_trace_id, apply_trace_header,
    clear_southbound_trace_id, current_southbound_trace_id, bind_southbound_trace_id,
)

def test_normalize_and_extract_trace_id():
    assert normalize_trace_id("t-20260903-001") == "t-20260903-001"
    assert len(normalize_trace_id("")) == 36
    assert extract_trace_id({"X-Trace-Id": "abc"}) == "abc"
    assert extract_trace_id({"x-trace-id": "abc"}) == "abc"
    assert apply_trace_header({"Content-Type": "application/json"}, "req-1")[
        TRACE_ID_HEADER
    ] == "req-1"


def test_bind_southbound_trace_id_is_task_local():
    clear_southbound_trace_id()
    assert current_southbound_trace_id() == ""
    assert bind_southbound_trace_id("t-file-1") == "t-file-1"
    assert current_southbound_trace_id() == "t-file-1"
    apply_trace_header({}, "t-ws-2")
    assert current_southbound_trace_id() == "t-ws-2"
    clear_southbound_trace_id()
    assert current_southbound_trace_id() == ""
