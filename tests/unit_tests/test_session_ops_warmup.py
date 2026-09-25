from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from openjiuwen.core.foundation.llm.schema.message import (
    AssistantMessage,
    SystemMessage,
    UserMessage,
)


def _deep_agent_with_empty_context():
    context_engine = SimpleNamespace(
        get_context=lambda *, session_id: None,
        create_context=AsyncMock(),
    )
    react_agent = SimpleNamespace(
        context_engine=context_engine,
        _config=SimpleNamespace(context_processors=[]),
    )
    return SimpleNamespace(react_agent=react_agent), context_engine


@pytest.mark.asyncio
async def test_warmup_excludes_current_request_from_restored_history(monkeypatch):
    from jiuwenswarm.agents.harness.common import session_ops_service

    deep_agent, context_engine = _deep_agent_with_empty_context()
    monkeypatch.setattr(session_ops_service, "history_exists", lambda _session_id: True)
    monkeypatch.setattr(
        session_ops_service,
        "load_history_records",
        lambda _session_id: [
            {"role": "user", "request_id": "request-old", "content": "旧问题"},
            {
                "role": "assistant",
                "request_id": "request-old",
                "event_type": "chat.final",
                "content": "旧回答",
            },
            {"role": "user", "request_id": "request-current", "content": "你好"},
            {"role": "user", "request_id": "request-later", "content": "后续消息"},
        ],
    )
    monkeypatch.setattr(
        session_ops_service,
        "resolve_live_agent_session",
        lambda _deep_agent, _session_id: object(),
    )

    restored = await session_ops_service.warmup_session_context(
        deep_agent=deep_agent,
        session_id="session-1",
        history_before_request_id="request-current",
    )

    assert restored is True
    history_messages = context_engine.create_context.await_args.kwargs[
        "history_messages"
    ]
    assert [message.content for message in history_messages] == ["旧问题", "旧回答"]


@pytest.mark.asyncio
async def test_warmup_does_not_restore_first_current_user_message(monkeypatch):
    from jiuwenswarm.agents.harness.common import session_ops_service

    deep_agent, context_engine = _deep_agent_with_empty_context()
    monkeypatch.setattr(session_ops_service, "history_exists", lambda _session_id: True)
    monkeypatch.setattr(
        session_ops_service,
        "load_history_records",
        lambda _session_id: [
            {"role": "user", "request_id": "request-current", "content": "你好"},
        ],
    )

    restored = await session_ops_service.warmup_session_context(
        deep_agent=deep_agent,
        session_id="session-1",
        history_before_request_id="request-current",
    )

    assert restored is False
    context_engine.create_context.assert_not_awaited()


@pytest.mark.asyncio
async def test_warmup_keeps_old_history_when_current_write_is_not_visible(monkeypatch):
    from jiuwenswarm.agents.harness.common import session_ops_service

    deep_agent, context_engine = _deep_agent_with_empty_context()
    monkeypatch.setattr(session_ops_service, "history_exists", lambda _session_id: True)
    monkeypatch.setattr(
        session_ops_service,
        "load_history_records",
        lambda _session_id: [
            {"role": "user", "request_id": "request-old", "content": "旧问题"},
            {
                "role": "assistant",
                "request_id": "request-old",
                "event_type": "chat.final",
                "content": "旧回答",
            },
        ],
    )
    monkeypatch.setattr(
        session_ops_service,
        "resolve_live_agent_session",
        lambda _deep_agent, _session_id: object(),
    )

    restored = await session_ops_service.warmup_session_context(
        deep_agent=deep_agent,
        session_id="session-1",
        history_before_request_id="request-current",
    )

    assert restored is True
    history_messages = context_engine.create_context.await_args.kwargs[
        "history_messages"
    ]
    assert [message.content for message in history_messages] == ["旧问题", "旧回答"]


@pytest.mark.asyncio
async def test_fork_context_falls_back_to_copied_disk_history(monkeypatch):
    from jiuwenswarm.agents.harness.common import session_ops_service

    create_new_context_engine = AsyncMock()
    deep_agent = SimpleNamespace(
        get_current_context=MagicMock(side_effect=RuntimeError("source context missing")),
        create_new_context_engine=create_new_context_engine,
    )
    monkeypatch.setattr(
        session_ops_service,
        "load_history_records",
        lambda session_id: [
            {"role": "user", "content": "源问题"},
            {
                "role": "assistant",
                "event_type": "chat.final",
                "content": "源回答",
            },
        ] if session_id == "fork-target" else [],
    )

    copied = await session_ops_service.copy_session_context(
        deep_agent,
        "fork-source",
        "fork-target",
    )

    assert copied is True
    messages = create_new_context_engine.await_args.kwargs["messages"]
    assert [message.role for message in messages] == ["system", "user", "assistant"]
    assert "fork-source" in messages[0].content
    assert [message.content for message in messages[1:]] == ["源问题", "源回答"]


@pytest.mark.asyncio
async def test_fork_context_marks_live_source_history(monkeypatch):
    from jiuwenswarm.agents.harness.common import session_ops_service

    create_new_context_engine = AsyncMock()
    deep_agent = SimpleNamespace(
        get_current_context=MagicMock(return_value=[
            UserMessage(content="源问题"),
            AssistantMessage(content="源回答"),
        ]),
        create_new_context_engine=create_new_context_engine,
    )

    copied = await session_ops_service.copy_session_context(
        deep_agent,
        "fork-source",
        "fork-target",
    )

    assert copied is True
    messages = create_new_context_engine.await_args.kwargs["messages"]
    assert isinstance(messages[0], SystemMessage)
    assert "fork-source" in messages[0].content
    assert [message.content for message in messages[1:]] == ["源问题", "源回答"]


@pytest.mark.asyncio
async def test_warmup_restores_fork_origin_semantics_from_copied_history(monkeypatch):
    from jiuwenswarm.agents.harness.common import session_ops_service

    deep_agent, context_engine = _deep_agent_with_empty_context()
    monkeypatch.setattr(session_ops_service, "history_exists", lambda _session_id: True)
    monkeypatch.setattr(
        session_ops_service,
        "load_history_records",
        lambda _session_id: [
            {
                "role": "user",
                "request_id": "request-old",
                "content": "源问题",
                "forked_from": {"session_id": "fork-source"},
            },
            {
                "role": "assistant",
                "request_id": "request-old",
                "event_type": "chat.final",
                "content": "源回答",
                "forked_from": {"session_id": "fork-source"},
            },
        ],
    )
    monkeypatch.setattr(
        session_ops_service,
        "resolve_live_agent_session",
        lambda _deep_agent, _session_id: object(),
    )

    restored = await session_ops_service.warmup_session_context(
        deep_agent=deep_agent,
        session_id="fork-target",
    )

    assert restored is True
    history_messages = context_engine.create_context.await_args.kwargs[
        "history_messages"
    ]
    assert [message.role for message in history_messages] == [
        "system",
        "user",
        "assistant",
    ]
    assert "fork-source" in history_messages[0].content
    assert [message.content for message in history_messages[1:]] == ["源问题", "源回答"]


@pytest.mark.asyncio
async def test_warmup_restores_fork_origin_from_session_metadata(monkeypatch):
    from jiuwenswarm.agents.harness.common import session_ops_service
    from jiuwenswarm.server.runtime.session import session_metadata

    deep_agent, context_engine = _deep_agent_with_empty_context()
    monkeypatch.setattr(session_ops_service, "history_exists", lambda _session_id: True)
    monkeypatch.setattr(
        session_ops_service,
        "load_history_records",
        lambda _session_id: [
            {
                "role": "user",
                "request_id": "request-old",
                "content": "压缩后仍保留的问题",
            },
            {
                "role": "assistant",
                "request_id": "request-old",
                "event_type": "chat.final",
                "content": "压缩后仍保留的回答",
            },
        ],
    )
    monkeypatch.setattr(
        session_metadata,
        "get_session_metadata",
        lambda _session_id, **_kwargs: {"forked_from": "fork-source"},
    )
    monkeypatch.setattr(
        session_ops_service,
        "resolve_live_agent_session",
        lambda _deep_agent, _session_id: object(),
    )

    restored = await session_ops_service.warmup_session_context(
        deep_agent=deep_agent,
        session_id="fork-target",
    )

    assert restored is True
    history_messages = context_engine.create_context.await_args.kwargs[
        "history_messages"
    ]
    assert [message.role for message in history_messages] == [
        "system",
        "user",
        "assistant",
    ]
    assert "fork-source" in history_messages[0].content


# ---------------------------------------------------------------------------
# Replay start: history before a compact boundary was replaced by its summary
# ---------------------------------------------------------------------------

def _boundary(trigger: str | None = None) -> dict:
    record = {
        "role": "assistant",
        "event_type": "context.compact_boundary",
        "content": "Conversation compacted",
    }
    if trigger is not None:
        record["compact_metadata"] = {"trigger": trigger}
    return record


def _compact_summary(text: str) -> dict:
    return {
        "role": "assistant",
        "event_type": "context.compact_summary",
        "content": text,
        "is_compact_summary": True,
    }


def _rewind_summary(text: str) -> dict:
    return {
        "role": "assistant",
        "event_type": "context.rewind_summary",
        "content": text,
        "is_compact_summary": True,
    }


def _tool_call_name(call) -> str:
    data = call.model_dump() if hasattr(call, "model_dump") else call
    if isinstance(data, dict):
        function = data.get("function")
        if isinstance(function, dict) and function.get("name"):
            return str(function["name"])
        if data.get("name"):
            return str(data["name"])
    return str(data)


def _describe(messages) -> list[tuple[str, str, str, str]]:
    """Flatten messages to (role, content, reasoning-or-tool_call_id, tool names)."""
    from openjiuwen.core.foundation.llm.schema.message import (
        AssistantMessage,
        ToolMessage,
        UserMessage,
    )

    described: list[tuple[str, str, str, str]] = []
    for message in messages:
        if isinstance(message, UserMessage):
            described.append(("user", message.content, "", ""))
        elif isinstance(message, ToolMessage):
            described.append(("tool", message.content, message.tool_call_id, ""))
        elif isinstance(message, AssistantMessage):
            described.append((
                "assistant",
                message.content,
                message.reasoning_content or "",
                ",".join(_tool_call_name(call) for call in message.tool_calls or []),
            ))
        else:  # pragma: no cover - defensive
            described.append((type(message).__name__, str(message), "", ""))
    return described


_FULL_TURN_HISTORY = [
    {"role": "user", "content": "q1"},
    {"role": "assistant", "event_type": "chat.reasoning", "content": "think"},
    {
        "role": "assistant",
        "event_type": "chat.tool_call",
        "tool_call": {"name": "read", "tool_call_id": "tc1", "arguments": {"path": "a"}},
    },
    {
        "role": "assistant",
        "event_type": "chat.tool_result",
        "tool_call_id": "tc1",
        "result": "file body",
    },
    {"role": "assistant", "event_type": "chat.final", "content": "a1"},
    {"role": "assistant", "event_type": "chat.delta", "content": "ignored"},
    {"role": "user", "content": "q2"},
    {"role": "assistant", "event_type": "chat.final", "content": "a2"},
]


def test_history_without_boundary_replays_everything():
    """The common case must not regress: no boundary, no slicing."""
    from jiuwenswarm.agents.harness.common import session_ops_service

    assert session_ops_service._find_replay_start_index(_FULL_TURN_HISTORY) == 0

    messages, skipped = session_ops_service._build_context_messages_from_history(
        _FULL_TURN_HISTORY
    )
    assert _describe(messages) == [
        ("user", "q1", "", ""),
        ("assistant", "", "think", "read"),
        ("tool", "file body", "tc1", ""),
        ("assistant", "a1", "", ""),
        ("user", "q2", "", ""),
        ("assistant", "a2", "", ""),
    ]
    assert skipped == 1


def test_replay_starts_at_the_last_compact_boundary():
    from jiuwenswarm.agents.harness.common import session_ops_service

    history = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "event_type": "chat.final", "content": "a1"},
        _boundary(),
        _compact_summary("summary one"),
        {"role": "user", "content": "q2"},
        {"role": "assistant", "event_type": "chat.final", "content": "a2"},
        _boundary(trigger="auto"),
        _compact_summary("summary two"),
        {"role": "user", "content": "q3"},
        {"role": "assistant", "event_type": "chat.final", "content": "a3"},
    ]

    assert session_ops_service._find_replay_start_index(history) == 6

    messages, _skipped = session_ops_service._build_context_messages_from_history(history)
    assert _describe(messages) == [
        ("user", "summary two", "", ""),
        ("user", "q3", "", ""),
        ("assistant", "a3", "", ""),
    ]


def test_boundary_as_final_record_replays_everything():
    """A boundary with no summary after it describes nothing; replay it all."""
    from jiuwenswarm.agents.harness.common import session_ops_service

    history = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "event_type": "chat.final", "content": "a1"},
        _boundary(trigger="auto"),
    ]

    assert session_ops_service._find_replay_start_index(history) == 0

    messages, _skipped = session_ops_service._build_context_messages_from_history(history)
    assert _describe(messages) == [("user", "q1", "", ""), ("assistant", "a1", "", "")]


def test_summaryless_boundary_falls_back_to_the_previous_boundary():
    """A /rewind can cut a boundary's summary away; the earlier one still holds."""
    from jiuwenswarm.agents.harness.common import session_ops_service

    history = [
        {"role": "user", "content": "q1"},
        _boundary(trigger="auto"),
        _compact_summary("summary one"),
        {"role": "user", "content": "q2"},
        _boundary(trigger="auto"),
    ]

    assert session_ops_service._find_replay_start_index(history) == 1

    messages, _skipped = session_ops_service._build_context_messages_from_history(history)
    assert _describe(messages) == [("user", "summary one", "", ""), ("user", "q2", "", "")]


def test_rewind_boundary_is_not_a_replay_start():
    """compact_partial_session truncates first, so records before its boundary
    are the ones deliberately kept — slicing there would delete them."""
    from jiuwenswarm.agents.harness.common import session_ops_service

    history = [
        {"role": "user", "content": "kept question"},
        {"role": "assistant", "event_type": "chat.final", "content": "kept answer"},
        _boundary(trigger="manual_rewind"),
        _rewind_summary("Summarized 4 messages from this point."),
        _compact_summary("what the removed tail was about"),
    ]

    assert session_ops_service._find_replay_start_index(history) == 0

    messages, _skipped = session_ops_service._build_context_messages_from_history(history)
    assert _describe(messages) == [
        ("user", "kept question", "", ""),
        ("assistant", "kept answer", "", ""),
        ("user", "Summarized 4 messages from this point.", "", ""),
        ("user", "what the removed tail was about", "", ""),
    ]


def test_rewind_summary_cancels_a_boundary_without_trigger_metadata():
    """Second, independent signal: a rewind boundary is always followed by a
    context.rewind_summary, even when compact_metadata is absent."""
    from jiuwenswarm.agents.harness.common import session_ops_service

    history = [
        {"role": "user", "content": "kept question"},
        _boundary(),
        _rewind_summary("Summarized 2 messages up to this point."),
        _compact_summary("removed prefix recap"),
    ]

    assert session_ops_service._find_replay_start_index(history) == 0


def test_replay_start_scan_tolerates_malformed_records():
    from jiuwenswarm.agents.harness.common import session_ops_service

    history = [
        "not a dict",
        None,
        {"role": "user", "content": "q1"},
        {"role": "assistant", "event_type": 7, "content": "non-string event_type"},
        _boundary(trigger="auto"),
        ["also not a dict"],
        _compact_summary("summary"),
    ]

    assert session_ops_service._find_replay_start_index(history) == 4


@pytest.mark.asyncio
async def test_warmup_scans_for_the_boundary_inside_the_request_id_bound(monkeypatch):
    """history_before_request_id is the stronger claim: it decides which records
    exist for this warmup, and the boundary scan runs within that range."""
    from jiuwenswarm.agents.harness.common import session_ops_service

    deep_agent, context_engine = _deep_agent_with_empty_context()
    monkeypatch.setattr(session_ops_service, "history_exists", lambda _session_id: True)
    monkeypatch.setattr(
        session_ops_service,
        "load_history_records",
        lambda _session_id: [
            {"role": "user", "request_id": "request-old", "content": "旧问题"},
            {"role": "user", "request_id": "request-current", "content": "你好"},
            {**_boundary(trigger="auto"), "request_id": "request-current"},
            {**_compact_summary("summary the warmup must not see"), "request_id": "request-current"},
        ],
    )
    monkeypatch.setattr(
        session_ops_service,
        "resolve_live_agent_session",
        lambda _deep_agent, _session_id: object(),
    )

    restored = await session_ops_service.warmup_session_context(
        deep_agent=deep_agent,
        session_id="session-1",
        history_before_request_id="request-current",
    )

    assert restored is True
    history_messages = context_engine.create_context.await_args.kwargs["history_messages"]
    assert [message.content for message in history_messages] == ["旧问题"]


@pytest.mark.asyncio
async def test_warmup_replays_from_a_boundary_before_the_request_id_bound(monkeypatch):
    from jiuwenswarm.agents.harness.common import session_ops_service

    deep_agent, context_engine = _deep_agent_with_empty_context()
    monkeypatch.setattr(session_ops_service, "history_exists", lambda _session_id: True)
    monkeypatch.setattr(
        session_ops_service,
        "load_history_records",
        lambda _session_id: [
            {"role": "user", "request_id": "request-old", "content": "旧问题"},
            {**_boundary(trigger="auto"), "request_id": "request-old"},
            {**_compact_summary("摘要"), "request_id": "request-old"},
            {"role": "user", "request_id": "request-mid", "content": "中间问题"},
            {"role": "user", "request_id": "request-current", "content": "你好"},
        ],
    )
    monkeypatch.setattr(
        session_ops_service,
        "resolve_live_agent_session",
        lambda _deep_agent, _session_id: object(),
    )

    restored = await session_ops_service.warmup_session_context(
        deep_agent=deep_agent,
        session_id="session-1",
        history_before_request_id="request-current",
    )

    assert restored is True
    history_messages = context_engine.create_context.await_args.kwargs["history_messages"]
    assert [message.content for message in history_messages] == ["摘要", "中间问题"]
