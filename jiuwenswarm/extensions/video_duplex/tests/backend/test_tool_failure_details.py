"""Failed tool receipts must retain actionable diagnostics in duplex progress."""

import pytest

from jiuwenswarm.extensions.video_duplex.backend.video_search import core_agent_progress


@pytest.mark.parametrize("field", ["result", "raw_output", "data"])
def test_nested_failure_exposes_details_without_losing_receipt(field):
    receipt = {"tool_name": "shell", "summary": "Execution failed",
               field: {"success": False, "error": "Permission denied",
                       "exit_code": 13, "stderr": "Cannot open target\n" + "details " * 200},
               "payload_refs": {"result": {"id": "full-result"}}}
    progress = core_agent_progress({"event_type": "chat.tool_result", "tool_result": receipt})
    assert progress["tool_success"] is False
    assert "Permission denied" in progress["detail"]
    forwarded = progress["tool_payload"]["tool_result"]
    assert "exit_code: 13" in forwarded["rendered_result"]
    assert "details " * 200 in forwarded["rendered_result"]
    assert forwarded["payload_refs"] == receipt["payload_refs"]
    assert forwarded[field] == receipt[field]
    assert "rendered_result" not in receipt


def test_json_compatibility_failure_is_detected():
    progress = core_agent_progress({"event_type": "chat.tool_result", "tool_name": "browser",
                                   "result": '{"isError":true,"error":"Window closed"}'})
    assert progress["status"] == "failed"
    assert "Window closed" in progress["detail"]


def test_pending_result_remains_pending_in_native_receipt():
    progress = core_agent_progress({"event_type": "chat.tool_result", "status": "pending",
                                   "error": "Awaiting approval"})
    assert progress["tool_success"] is True
    assert progress["tool_payload"]["status"] == "pending"
