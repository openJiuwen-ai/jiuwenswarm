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
async def test_side_context_places_boundary_after_inherited_history(monkeypatch):
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
        "side-target",
        side_conversation=True,
    )

    assert copied is True
    messages = create_new_context_engine.await_args.kwargs["messages"]
    assert [message.content for message in messages[:2]] == ["源问题", "源回答"]
    assert isinstance(messages[-1], SystemMessage)
    assert "side conversation" in messages[-1].content.lower()
    assert "reference context" in messages[-1].content.lower()


@pytest.mark.asyncio
async def test_side_warmup_restores_boundary_between_parent_and_side_turns(monkeypatch):
    from jiuwenswarm.agents.harness.common import session_ops_service

    deep_agent, context_engine = _deep_agent_with_empty_context()
    monkeypatch.setattr(session_ops_service, "history_exists", lambda _session_id: True)
    monkeypatch.setattr(
        session_ops_service,
        "load_history_records",
        lambda _session_id: [
            {
                "role": "user",
                "request_id": "request-parent",
                "content": "源问题",
                "forked_from": {"session_id": "fork-source"},
            },
            {
                "role": "assistant",
                "request_id": "request-parent",
                "event_type": "chat.final",
                "content": "源回答",
                "forked_from": {"session_id": "fork-source"},
            },
            {
                "role": "user",
                "request_id": "request-side",
                "content": "侧会话问题",
            },
            {
                "role": "assistant",
                "request_id": "request-side",
                "event_type": "chat.final",
                "content": "侧会话回答",
            },
        ],
    )
    monkeypatch.setattr(
        session_ops_service,
        "resolve_live_agent_session",
        lambda _deep_agent, _session_id: object(),
    )
    monkeypatch.setattr(
        session_ops_service,
        "_side_parent_for_session",
        lambda _session_id: "fork-source",
    )

    restored = await session_ops_service.warmup_session_context(
        deep_agent=deep_agent,
        session_id="side-target",
    )

    assert restored is True
    messages = context_engine.create_context.await_args.kwargs["history_messages"]
    assert [message.content for message in messages[:2]] == ["源问题", "源回答"]
    assert isinstance(messages[2], SystemMessage)
    assert [message.content for message in messages[3:]] == ["侧会话问题", "侧会话回答"]


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
