import time

import pytest

from jiuwenswarm.server.runtime.session import session_history


def _wait_history(session_id: str, *, min_count: int = 1):
    deadline = time.time() + 5
    while time.time() < deadline:
        data = session_history.load_history_records(session_id)
        if len(data) >= min_count:
            return data
        time.sleep(0.05)
    return session_history.load_history_records(session_id)


def test_append_history_skips_empty_chat_final_and_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)

    session_history.append_history_record(
        session_id="s-empty",
        request_id="r1",
        channel_id="web",
        role="assistant",
        event_type="chat.final",
        content="   ",
        timestamp=1.0,
    )
    session_history.append_history_record(
        session_id="s-empty",
        request_id="r2",
        channel_id="web",
        role="assistant",
        event_type="chat.final",
        content="HEARTBEAT_OK",
        timestamp=2.0,
    )
    session_history.append_history_record(
        session_id="heartbeat_abc",
        request_id="r3",
        channel_id="heartbeat",
        role="user",
        content="heartbeat prompt",
        timestamp=3.0,
    )
    session_history.append_history_record(
        session_id="s-empty",
        request_id="r4",
        channel_id="web",
        role="assistant",
        event_type="chat.file",
        content="",
        timestamp=4.0,
        extra={"files": [{"path": "/tmp/a.md", "name": "a.md"}]},
    )
    session_history.append_history_record(
        session_id="s-empty",
        request_id="r5",
        channel_id="web",
        role="assistant",
        event_type="chat.final",
        content="hello",
        timestamp=5.0,
    )

    data = _wait_history("s-empty", min_count=2)
    assert [item.get("event_type") for item in data] == ["chat.file", "chat.final"]
    assert data[1]["content"] == "hello"
    assert session_history.load_history_records("heartbeat_abc") == []


def test_assistant_file_event_is_restored_for_team_history() -> None:
    assert session_history._is_team_relevant(
        {
            "event_type": "chat.file",
            "role": "assistant",
            "files": [
                {
                    "name": "report.xlsx",
                    "download_url": "/file-api/download?token=signed",
                }
            ],
        }
    )


def test_has_persistable_assistant_payload_tool_result_with_tool_call_id():
    assert session_history._has_persistable_assistant_payload(
        content_text="",
        event_type="chat.tool_result",
        extra={"tool_call_id": "call_abc", "tool_name": "list_files", "result": "ok"},
    ) is True


def test_has_persistable_assistant_payload_tool_result_with_nested_tool_result():
    assert session_history._has_persistable_assistant_payload(
        content_text="",
        event_type="chat.tool_result",
        extra={"tool_result": {"tool_name": "test", "tool_call_id": "call_abc", "result": "ok"}},
    ) is True


def test_has_persistable_assistant_payload_tool_result_empty_rejected():
    assert session_history._has_persistable_assistant_payload(
        content_text="",
        event_type="chat.tool_result",
        extra={},
    ) is False


def test_context_usage_with_structured_payload_is_persisted(tmp_path, monkeypatch):
    """context.usage 是空 content 的结构化监控事件，payload 非空时必须落盘，
    且 rate/context_max/tokens_used 合并到记录顶层供 history.get 恢复。"""
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)

    session_history.append_history_record(
        session_id="s-ctx-usage",
        request_id="r1",
        channel_id="web",
        role="assistant",
        event_type="context.usage",
        content="",
        timestamp=1.0,
        extra={"rate": 42.5, "context_max": 200000, "tokens_used": 85000},
    )

    data = _wait_history("s-ctx-usage", min_count=1)
    assert len(data) == 1
    record = data[0]
    assert record["event_type"] == "context.usage"
    assert record["rate"] == 42.5
    assert record["context_max"] == 200000
    assert record["tokens_used"] == 85000


def test_context_usage_without_payload_is_rejected():
    assert session_history._has_persistable_assistant_payload(
        content_text="",
        event_type="context.usage",
        extra={},
    ) is False


def test_context_usage_is_restorable_history_record():
    """history.get 恢复白名单必须放行 context.usage（AgentServer 侧按此集合过滤）。"""
    from jiuwenswarm.server import wire_truncate

    assert "context.usage" in wire_truncate._HISTORY_RESTORABLE_ASSISTANT_EVENT_TYPES


def test_chat_usage_summary_with_usage_is_persisted(tmp_path, monkeypatch):
    """chat.usage_summary 空 content 但 usage 非空时必须落盘，
    供历史恢复重建每轮 token 统计（对齐 develop）。"""
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)

    session_history.append_history_record(
        session_id="s-usage-summary",
        request_id="r1",
        channel_id="web",
        role="assistant",
        event_type="chat.usage_summary",
        content="",
        timestamp=1.0,
        extra={
            "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            "model": "test-model",
        },
    )

    data = _wait_history("s-usage-summary", min_count=1)
    assert len(data) == 1
    record = data[0]
    assert record["event_type"] == "chat.usage_summary"
    assert record["usage"]["total_tokens"] == 150
    assert record["model"] == "test-model"


def test_chat_usage_summary_with_empty_usage_is_rejected():
    assert session_history._has_persistable_assistant_payload(
        content_text="",
        event_type="chat.usage_summary",
        extra={"usage": {}},
    ) is False


def test_has_persistable_assistant_payload_tool_result_falsy_values_rejected():
    assert session_history._has_persistable_assistant_payload(
        content_text="",
        event_type="chat.tool_result",
        extra={"tool_call_id": "", "tool_result": None},
    ) is False


def test_has_persistable_assistant_payload_processing_status_still_rejected():
    assert session_history._has_persistable_assistant_payload(
        content_text="",
        event_type="chat.processing_status",
        extra={"is_processing": True},
    ) is False


def test_has_persistable_assistant_payload_tool_update_still_rejected():
    assert session_history._has_persistable_assistant_payload(
        content_text="",
        event_type="chat.tool_update",
        extra={"tool_call_id": "call_abc", "beam_search": {}},
    ) is False


def test_append_history_persists_tool_result(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)

    session_history.append_history_record(
        session_id="s-tool-result",
        request_id="r1",
        channel_id="web",
        role="assistant",
        event_type="chat.tool_call",
        content="",
        timestamp=1.0,
        extra={"tool_call": {"name": "list_files", "id": "call_abc", "arguments": "{}"}},
    )
    session_history.append_history_record(
        session_id="s-tool-result",
        request_id="r1",
        channel_id="web",
        role="assistant",
        event_type="chat.tool_result",
        content="",
        timestamp=2.0,
        extra={"tool_call_id": "call_abc", "tool_name": "list_files", "result": "file1.txt", "success": True},
    )
    session_history.append_history_record(
        session_id="s-tool-result",
        request_id="r1",
        channel_id="web",
        role="assistant",
        event_type="chat.final",
        content="Done",
        timestamp=3.0,
    )

    data = _wait_history("s-tool-result", min_count=3)
    event_types = [item.get("event_type") for item in data]
    assert event_types == ["chat.tool_call", "chat.tool_result", "chat.final"]

    tool_result_record = data[1]
    assert tool_result_record["event_type"] == "chat.tool_result"
    assert tool_result_record["tool_call_id"] == "call_abc"
    assert tool_result_record["tool_name"] == "list_files"


def test_request_completion_is_persisted_after_prior_history(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)

    session_history.append_history_record(
        session_id="s-complete",
        request_id="r1",
        channel_id="web",
        role="assistant",
        event_type="chat.final",
        content="Done",
        timestamp=1.0,
    )
    receipt = session_history.enqueue_history_request_completion(
        "s-complete",
        "r1",
        terminal_status="success",
    )
    assert receipt is not None
    receipt.result(timeout=2)

    records = session_history.load_history_records("s-complete")
    assert [record.get("event_type") for record in records] == [
        "chat.final",
        session_history.SESSION_REQUEST_COMPLETED_EVENT,
    ]
    assert records[-1]["status"] == "success"


def test_append_history_persists_tool_result_with_nested_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)

    session_history.append_history_record(
        session_id="s-nested",
        request_id="r1",
        channel_id="web",
        role="assistant",
        event_type="chat.tool_result",
        content="cancelled",
        timestamp=1.0,
        extra={
            "tool_result": {
                "tool_name": "code",
                "tool_call_id": "call_xyz",
                "result": "cancelled",
                "status": "error",
            },
        },
    )

    data = _wait_history("s-nested", min_count=1)
    assert len(data) == 1
    assert data[0]["event_type"] == "chat.tool_result"


def test_append_history_tool_result_empty_payload_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)

    session_history.append_history_record(
        session_id="s-empty-tr",
        request_id="r1",
        channel_id="web",
        role="assistant",
        event_type="chat.tool_result",
        content="",
        timestamp=1.0,
        extra={},
    )

    import time as _t
    _t.sleep(0.3)
    data = session_history.load_history_records("s-empty-tr")
    assert data == []


def test_append_history_keeps_only_structurally_valid_tool_results(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)

    records = (
        ("valid-flat", {"tool_call_id": "call-flat", "success": False}),
        (
            "valid-nested",
            {"tool_result": {"tool_call_id": "call-nested", "status": "denied"}},
        ),
        ("missing-id", {"result": "orphan"}),
        ("empty-shell", {"tool_call_id": "call-empty"}),
        ("heuristic-only", {"tool_name": "bash", "status": "denied"}),
    )
    for request_id, extra in records:
        session_history.append_history_record(
            session_id="s-tool-results",
            request_id=request_id,
            channel_id="web",
            role="assistant",
            event_type="chat.tool_result",
            content="",
            timestamp=1.0,
            extra=extra,
        )

    data = _wait_history("s-tool-results", min_count=2)
    assert [item["request_id"] for item in data] == ["valid-flat", "valid-nested"]
    assert data[0]["tool_call_id"] == "call-flat"
    assert data[0]["success"] is False
    assert data[1]["tool_result"]["tool_call_id"] == "call-nested"
    assert data[1]["tool_result"]["status"] == "denied"


def test_tool_result_content_does_not_bypass_structural_validation() -> None:
    assert session_history._has_persistable_assistant_payload(
        content_text="orphan result",
        event_type="chat.tool_result",
        extra={},
    ) is False


@pytest.mark.parametrize(
    "extra",
    (
        {"tool_call_id": "call-flat", "success": False},
        {"tool_call_id": "call-flat", "result": ""},
        {"tool_result": {"tool_call_id": "call-nested", "error": None}},
        {
            "tool_call_id": "call-shared",
            "tool_result": {
                "tool_call_id": "call-shared",
                "status": "denied",
            },
        },
    ),
)
def test_structurally_valid_tool_result_keeps_falsy_terminal_values(
    extra: dict,
) -> None:
    assert session_history._is_structurally_valid_tool_result(extra) is True


@pytest.mark.parametrize(
    "extra",
    (
        {"tool_call_id": 123, "result": "done"},
        {"tool_call_id": " ", "result": "done"},
        {"tool_result": {"tool_call_id": None, "result": "done"}},
        {
            "tool_call_id": "call-a",
            "tool_result": {"tool_call_id": "call-b", "result": "done"},
        },
        {"tool_call_id": "call-a", "tool_result": {"result": "done"}},
        {"result": "done", "tool_result": {"tool_call_id": "call-b"}},
        {"tool_call_id": "call-a", "tool_result": "done"},
        {"tool_call_id": "call-a", "result": "done", "tool_result": {}},
        {
            "tool_call_id": "call-a",
            "result": "done",
            "tool_result": {"tool_call_id": "call-a"},
        },
    ),
)
def test_structurally_invalid_tool_result_is_rejected(extra: dict) -> None:
    assert session_history._is_structurally_valid_tool_result(extra) is False
