from jiuwenclaw.e2a.acp_tool_updates import (
    build_acp_todo_update,
    build_acp_tool_call_update,
    build_acp_tool_result_update,
)


def test_build_acp_tool_call_update_enriches_display_fields_and_preserves_legacy_shape():
    cache = {}
    update = build_acp_tool_call_update(
        {
            "tool_call": {
                "id": "call-1",
                "name": "read_file",
                "arguments": {"path": "src/main.py"},
            }
        },
        cache=cache,
    )

    assert update == {
        "sessionUpdate": "tool_call",
        "toolCall": {
            "id": "call-1",
            "name": "read_file",
            "arguments": {"path": "src/main.py"},
        },
        "toolCallId": "call-1",
        "title": "Reading src/main.py",
        "kind": "read",
        "status": "pending",
        "rawInput": {"path": "src/main.py"},
        "locations": [{"path": "src/main.py"}],
    }
    assert cache == {
        "call-1": {
            "toolCallId": "call-1",
            "title": "Reading src/main.py",
            "kind": "read",
            "status": "pending",
            "rawInput": {"path": "src/main.py"},
            "locations": [{"path": "src/main.py"}],
            "toolName": "read_file",
        }
    }


def test_build_acp_tool_result_update_reuses_cached_metadata_and_adds_content():
    cache = {
        "call-1": {
            "toolCallId": "call-1",
            "title": "Reading src/main.py",
            "kind": "read",
            "rawInput": {"path": "src/main.py"},
            "locations": [{"path": "src/main.py"}],
            "toolName": "read_file",
        }
    }

    update = build_acp_tool_result_update(
        {
            "tool_call_id": "call-1",
            "result": "file contents",
        },
        cache=cache,
    )

    assert update == {
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


def test_build_acp_tool_result_update_supports_in_progress_without_result():
    cache = {
        "call-1": {
            "toolCallId": "call-1",
            "title": "Reading src/main.py",
            "kind": "read",
            "rawInput": {"path": "src/main.py"},
            "locations": [{"path": "src/main.py"}],
            "toolName": "read_file",
        }
    }

    update = build_acp_tool_result_update(
        {
            "tool_call_id": "call-1",
            "status": "in_progress",
        },
        cache=cache,
    )

    assert update == {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "call-1",
        "toolName": "read_file",
        "title": "Reading src/main.py",
        "kind": "read",
        "status": "in_progress",
        "rawInput": {"path": "src/main.py"},
        "locations": [{"path": "src/main.py"}],
    }


def test_build_acp_tool_result_update_embeds_terminal_for_create_terminal():
    cache = {
        "call-term-1": {
            "toolCallId": "call-term-1",
            "title": "Running uv run python batch_convert.py",
            "kind": "execute",
            "rawInput": {"command": "uv run python batch_convert.py"},
            "toolName": "create_terminal",
        }
    }

    update = build_acp_tool_result_update(
        {
            "tool_call_id": "call-term-1",
            "tool_name": "create_terminal",
            "result": "{'terminalId': 'term_1'}",
            "raw_output": {"terminalId": "term_1"},
        },
        cache=cache,
    )

    assert update == {
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


def test_build_acp_tool_result_update_keeps_wait_for_terminal_exit_timeout_in_progress():
    cache = {
        "call-wait-1": {
            "toolCallId": "call-wait-1",
            "title": "Waiting for terminal exit term_1",
            "kind": "execute",
            "rawInput": {"terminalId": "term_1"},
            "toolName": "wait_for_terminal_exit",
        }
    }

    update = build_acp_tool_result_update(
        {
            "tool_call_id": "call-wait-1",
            "tool_name": "wait_for_terminal_exit",
            "raw_output": {"timedOut": True, "running": True},
        },
        cache=cache,
    )

    assert update == {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "call-wait-1",
        "toolName": "wait_for_terminal_exit",
        "title": "Waiting for terminal exit term_1",
        "kind": "execute",
        "status": "in_progress",
        "rawInput": {"terminalId": "term_1"},
        "rawOutput": {"timedOut": True, "running": True},
    }


def test_build_acp_tool_result_update_supports_failed_without_result():
    cache = {
        "call-1": {
            "toolCallId": "call-1",
            "title": "Running pwd",
            "kind": "execute",
            "rawInput": {"command": "pwd"},
            "toolName": "terminal_create",
        }
    }

    update = build_acp_tool_result_update(
        {
            "tool_call_id": "call-1",
            "status": "failed",
            "content": "Permission denied",
        },
        cache=cache,
    )

    assert update == {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "call-1",
        "toolName": "terminal_create",
        "title": "Running pwd",
        "kind": "execute",
        "status": "failed",
        "rawInput": {"command": "pwd"},
        "result": "Permission denied",
        "content": [{"type": "content", "content": {"type": "text", "text": "Permission denied"}}],
    }


def test_build_acp_tool_result_update_omits_raw_output_for_list_directory_alias():
    update = build_acp_tool_result_update(
        {
            "tool_name": "list_directories",
            "tool_call_id": "call-list-1",
            "status": "completed",
            "rawOutput": {"list_items": [{"name": "src", "is_directory": True}]},
            "result": "[dir] src",
        }
    )

    assert update == {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "call-list-1",
        "toolName": "list_directories",
        "title": "Listing files",
        "kind": "read",
        "status": "completed",
        "result": "[dir] src",
        "content": [{"type": "content", "content": {"type": "text", "text": "[dir] src"}}],
    }


def test_build_acp_todo_update_keeps_full_snapshot_shape():
    update = build_acp_todo_update(
        {
            "todos": [
                {
                    "id": "todo-1",
                    "content": "Add ACP todo session update",
                    "activeForm": "Adding ACP todo session update",
                    "status": "in_progress",
                    "createdAt": "2026-04-16T00:00:00Z",
                    "updatedAt": "2026-04-16T00:05:00Z",
                }
            ]
        }
    )

    assert update == {
        "sessionUpdate": "todo_update",
        "todos": [
            {
                "id": "todo-1",
                "content": "Add ACP todo session update",
                "activeForm": "Adding ACP todo session update",
                "status": "in_progress",
                "createdAt": "2026-04-16T00:00:00Z",
                "updatedAt": "2026-04-16T00:05:00Z",
            }
        ],
    }
