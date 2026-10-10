# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Restored tool messages carry the text the model read, not the structured repr."""

from openjiuwen.core.foundation.llm import ToolMessage

from jiuwenswarm.agents.harness.common.session_ops_service import _build_context_messages_from_history


def _records(tool_result: dict[str, str]) -> list[dict[str, object]]:
    return [
        {
            "role": "assistant",
            "event_type": "chat.tool_call",
            "tool_call": {"name": "glob", "tool_call_id": "tc-1", "arguments": {"pattern": "*.py"}},
        },
        {"role": "assistant", "event_type": "chat.tool_result", "tool_call_id": "tc-1", **tool_result},
    ]


def _tool_messages(messages: list[object]) -> list[ToolMessage]:
    return [message for message in messages if isinstance(message, ToolMessage)]


def test_restore_prefers_rendered_result() -> None:
    messages, _ = _build_context_messages_from_history(
        _records({"result": "success=True data={'matching_files': ['/a.py']} error=None", "rendered_result": "/a.py"})
    )

    assert [message.content for message in _tool_messages(messages)] == ["/a.py"]


def test_restore_falls_back_to_legacy_result() -> None:
    messages, _ = _build_context_messages_from_history(_records({"result": "legacy text"}))

    assert [message.content for message in _tool_messages(messages)] == ["legacy text"]
