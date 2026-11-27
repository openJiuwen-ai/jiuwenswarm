from types import SimpleNamespace

from jiuwenclaw.e2a.acp.session_updates import (
    build_acp_final_text_update,
    build_acp_session_update,
)
from jiuwenclaw.schema.message import EventType, Message


def _build_message(event_type: EventType) -> Message:
    return Message(
        id="req-1",
        type="event",
        channel_id="acp",
        session_id="sess-1",
        params={},
        timestamp=0.0,
        ok=True,
        payload={},
        event_type=event_type,
    )


def _build_state() -> SimpleNamespace:
    return SimpleNamespace(
        assistant_message_id=None,
        thought_message_id=None,
        user_message_id=None,
        tool_call_cache={},
    )


def test_build_acp_session_update_maps_todo_updated_to_todo_update():
    update = build_acp_session_update(
        _build_message(EventType.TODO_UPDATED),
        {
            "todos": [
                {
                    "id": "todo-1",
                    "content": "Implement ACP todo update",
                    "activeForm": "Implementing ACP todo update",
                    "status": "in_progress",
                    "createdAt": "2026-04-16T00:00:00Z",
                    "updatedAt": "2026-04-16T00:05:00Z",
                }
            ]
        },
        _build_state(),
    )

    assert update == {
        "sessionUpdate": "todo_update",
        "todos": [
            {
                "id": "todo-1",
                "content": "Implement ACP todo update",
                "activeForm": "Implementing ACP todo update",
                "status": "in_progress",
                "createdAt": "2026-04-16T00:00:00Z",
                "updatedAt": "2026-04-16T00:05:00Z",
            }
        ],
    }


def test_build_acp_session_update_maps_chat_reasoning_to_agent_thought_chunk():
    state = _build_state()
    update = build_acp_session_update(
        _build_message(EventType.CHAT_REASONING),
        {"content": "Reasoning chunk", "event_type": "chat.reasoning"},
        state,
    )

    assert update == {
        "sessionUpdate": "agent_thought_chunk",
        "messageId": state.thought_message_id,
        "content": {"type": "text", "text": "Reasoning chunk"},
    }
    assert isinstance(state.thought_message_id, str)


def test_build_acp_session_update_keeps_reasoning_consistent_between_delta_and_reasoning():
    delta_state = _build_state()
    reasoning_state = _build_state()

    delta_update = build_acp_session_update(
        _build_message(EventType.CHAT_DELTA),
        {"content": "Shared reasoning", "source_chunk_type": "llm_reasoning"},
        delta_state,
    )
    reasoning_update = build_acp_session_update(
        _build_message(EventType.CHAT_REASONING),
        {"content": "Shared reasoning", "event_type": "chat.reasoning"},
        reasoning_state,
    )

    assert delta_update is not None
    assert reasoning_update is not None
    assert delta_update["sessionUpdate"] == "agent_thought_chunk"
    assert reasoning_update["sessionUpdate"] == "agent_thought_chunk"
    assert delta_update["content"] == reasoning_update["content"] == {
        "type": "text",
        "text": "Shared reasoning",
    }


def test_build_acp_session_update_caches_tool_metadata_for_tool_result():
    state = _build_state()

    tool_call_update = build_acp_session_update(
        _build_message(EventType.CHAT_TOOL_CALL),
        {
            "tool_call": {
                "tool_call_id": "call-1",
                "name": "read_file",
                "arguments": {"path": "src/main.py"},
            }
        },
        state,
    )
    tool_result_update = build_acp_session_update(
        _build_message(EventType.CHAT_TOOL_RESULT),
        {
            "tool_call_id": "call-1",
            "result": "file contents",
        },
        state,
    )

    assert tool_call_update is not None
    assert tool_result_update == {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "call-1",
        "toolName": "read_file",
        "title": "Reading src/main.py",
        "kind": "read",
        "status": "completed",
        "rawInput": {"path": "src/main.py"},
        "locations": [{"path": "src/main.py"}],
        "result": "file contents",
        "content": [{"type": "content", "content": {"type": "text", "text": "file contents"}}],
    }


def test_build_acp_session_update_maps_tool_update_to_in_progress_status():
    state = _build_state()

    tool_call_update = build_acp_session_update(
        _build_message(EventType.CHAT_TOOL_CALL),
        {
            "tool_call": {
                "tool_call_id": "call-2",
                "name": "read_file",
                "arguments": {"path": "src/main.py"},
            }
        },
        state,
    )
    tool_progress_update = build_acp_session_update(
        _build_message(EventType.CHAT_TOOL_UPDATE),
        {
            "tool_call_id": "call-2",
            "status": "in_progress",
        },
        state,
    )

    assert tool_call_update is not None
    assert tool_progress_update == {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "call-2",
        "toolName": "read_file",
        "title": "Reading src/main.py",
        "kind": "read",
        "status": "in_progress",
        "rawInput": {"path": "src/main.py"},
        "locations": [{"path": "src/main.py"}],
    }


def test_build_acp_session_update_embeds_terminal_for_create_terminal_result():
    state = _build_state()

    tool_call_update = build_acp_session_update(
        _build_message(EventType.CHAT_TOOL_CALL),
        {
            "tool_call": {
                "tool_call_id": "call-term-1",
                "name": "create_terminal",
                "arguments": {"command": "uv run python batch_convert.py"},
            }
        },
        state,
    )
    tool_result_update = build_acp_session_update(
        _build_message(EventType.CHAT_TOOL_RESULT),
        {
            "tool_call_id": "call-term-1",
            "tool_name": "create_terminal",
            "result": "{'terminalId': 'term_1'}",
            "raw_output": {"terminalId": "term_1"},
        },
        state,
    )

    assert tool_call_update is not None
    assert tool_result_update == {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "call-term-1",
        "toolName": "create_terminal",
        "title": "Running uv run python batch_convert.py",
        "kind": "execute",
        "status": "in_progress",
        "rawInput": {"command": "uv run python batch_convert.py"},
        "rawOutput": {"terminalId": "term_1"},
        "content": [{"type": "terminal", "terminalId": "term_1"}],
    }


def test_build_acp_final_text_update_maps_reasoning_final_to_agent_thought_chunk():
    state = _build_state()

    update = build_acp_final_text_update(
        {
            "content": "Final reasoning",
            "event_type": "chat.reasoning",
        },
        state,
    )

    assert update == {
        "sessionUpdate": "agent_thought_chunk",
        "messageId": state.thought_message_id,
        "content": {"type": "text", "text": "Final reasoning"},
    }
