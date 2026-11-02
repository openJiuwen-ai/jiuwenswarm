import time

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


def test_has_persistable_assistant_payload_keeps_usage_events():
    assert session_history._has_persistable_assistant_payload(
        content_text="",
        event_type="chat.usage_summary",
        extra={"usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}},
    ) is True
    assert session_history._has_persistable_assistant_payload(
        content_text="",
        event_type="chat.usage_metadata",
        extra={"metadata": {"usage_metadata": {"input_tokens": 10}}},
    ) is True
    assert session_history._has_persistable_assistant_payload(
        content_text="",
        event_type="chat.llm_usage",
        extra={"usage_metadata": {"input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050}},
    ) is True
    assert session_history._has_persistable_assistant_payload(
        content_text="",
        event_type="chat.usage_summary",
        extra={},
    ) is False


def test_append_history_persists_usage_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    session_history.append_history_record(
        session_id="s-usage",
        request_id="r-usage",
        channel_id="web",
        role="assistant",
        event_type="chat.usage_summary",
        content="",
        timestamp=1.0,
        extra={"usage": {"input_tokens": 33709, "output_tokens": 4, "total_tokens": 33713, "cache_tokens": 10880}},
    )
    data = _wait_history("s-usage", min_count=1)
    assert data[0]["event_type"] == "chat.usage_summary"
    assert data[0]["usage"]["input_tokens"] == 33709
    assert data[0]["content"] == ""


def test_context_usage_v1_with_structured_payload_is_persisted(tmp_path, monkeypatch):
    """context.usage is blank-content; v1 snapshot fields must survive history restore."""
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)

    session_history.append_history_record(
        session_id="s-ctx-usage",
        request_id="r1",
        channel_id="web",
        role="assistant",
        event_type="context.usage",
        content="",
        timestamp=1.0,
        extra={
            "schema_version": "context-usage.v1",
            "phase": "post_call",
            "context_window": {
                "limit_tokens": 128000,
                "input_tokens": 42000,
                "occupancy_rate": 0.328,
            },
            "parts": {
                "system_prompt": {"category": "system_prompt", "tokens": 8000},
                "tools": {"category": "tools", "tokens": 12000},
                "skills": {"category": "skills", "tokens": 2000},
                "messages": {"category": "messages", "tokens": 20000},
            },
            "session_kv_cache_hit_rate": 0.6,
            "rate": 32.8,
            "context_max": 128000,
            "tokens_used": 42000,
        },
    )

    data = _wait_history("s-ctx-usage", min_count=1)
    assert len(data) == 1
    record = data[0]
    assert record["event_type"] == "context.usage"
    assert record["schema_version"] == "context-usage.v1"
    assert record["context_window"]["input_tokens"] == 42000
    assert record["parts"]["messages"]["tokens"] == 20000
    assert record["session_kv_cache_hit_rate"] == 0.6
    assert record["rate"] == 32.8


def test_context_usage_without_v1_snapshot_is_rejected():
    assert session_history._has_persistable_assistant_payload(
        content_text="",
        event_type="context.usage",
        extra={},
    ) is False
    # Legacy three-field frame: useful for live alias only, not history restore.
    assert session_history._has_persistable_assistant_payload(
        content_text="",
        event_type="context.usage",
        extra={"rate": 42.5, "context_max": 200000, "tokens_used": 85000},
    ) is False


def test_context_usage_is_restorable_history_record():
    """history.get restore whitelist must allow context.usage."""
    from jiuwenswarm.server import wire_truncate

    assert "context.usage" in wire_truncate._HISTORY_RESTORABLE_ASSISTANT_EVENT_TYPES


def test_has_persistable_assistant_payload_tool_update_is_merged():
    assert session_history._has_persistable_assistant_payload(
        content_text="",
        event_type="chat.tool_update",
        extra={"tool_call_id": "call_abc", "beam_search": {}},
    ) is True


def test_has_persistable_assistant_payload_keeps_subtask_update():
    assert session_history._has_persistable_assistant_payload(
        content_text="",
        event_type="chat.subtask_update",
        extra={"task_id": "t1", "description": "wait"},
    ) is True
    assert session_history._has_persistable_assistant_payload(
        content_text="",
        event_type="chat.subtask_update",
        extra={},
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
