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
@pytest.mark.parametrize("refresh_existing", [False, True])
async def test_cron_followup_refreshes_cached_context_and_keeps_completed_tail(monkeypatch, refresh_existing):
    from jiuwenswarm.agents.harness.common import session_ops_service

    deep_agent, context_engine = _deep_agent_with_empty_context()
    existing_context = SimpleNamespace(
        context_id=lambda: "default_context_id", save_state=lambda: {}, load_state=MagicMock(),
    )
    context_engine.get_context = lambda **_kwargs: existing_context
    context_engine.clear_context = AsyncMock()
    load_history = MagicMock(return_value=[
        {"role": "user", "request_id": "cron-1", "content": "Research task"},
        {"role": "user", "request_id": "web-1", "content": "Hello"},
        {"role": "assistant", "request_id": "cron-1", "event_type": "chat.final", "content": "Completed report"},
        {"role": "user", "request_id": "web-2", "content": "Queued input"},
    ])
    monkeypatch.setattr(session_ops_service, "history_exists", lambda _sid: True)
    monkeypatch.setattr(session_ops_service, "load_history_records", load_history)
    monkeypatch.setattr(session_ops_service, "resolve_live_agent_session", lambda *_args: object())

    assert await session_ops_service.warmup_session_context(
        deep_agent=deep_agent,
        session_id="__cron__session",
        history_before_request_id="web-1",
        refresh_existing=refresh_existing,
    )

    if refresh_existing:
        restored = existing_context.load_state.call_args.args[0]["default_context_id"]["messages"]
        assert [message.content for message in restored] == ["Research task", "Completed report"]
        context_engine.create_context.assert_awaited_once()
    else:
        load_history.assert_not_called()
        existing_context.load_state.assert_not_called()
        context_engine.create_context.assert_not_awaited()
    context_engine.clear_context.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_history", [False, True])
async def test_cron_refresh_keeps_existing_context_and_session_state(monkeypatch, invalid_history):
    from openjiuwen.core.context_engine import ContextEngine
    from jiuwenswarm.agents.harness.common import session_ops_service

    session_id = "__cron__session"
    state = {}
    session = SimpleNamespace(get_session_id=lambda: session_id, get_state=state.get)
    context_engine = ContextEngine()
    existing_context = await context_engine.create_context(
        session=session, history_messages=[UserMessage(content="Cached task")],
    )
    state["context"] = {existing_context.context_id(): existing_context.save_state()}
    deep_agent = SimpleNamespace(react_agent=SimpleNamespace(context_engine=context_engine))
    monkeypatch.setattr(session_ops_service, "history_exists", lambda _sid: True)
    monkeypatch.setattr(session_ops_service, "load_history_records", lambda _sid: [
        {"role": "user", "request_id": "cron-1", "content": "Research task"},
        {"role": "assistant", "request_id": "cron-1", "event_type": "chat.final", "content": "Completed report"},
    ])
    monkeypatch.setattr(session_ops_service, "resolve_live_agent_session", lambda *_args: session)
    if invalid_history:
        monkeypatch.setattr(session_ops_service, "_build_context_messages_from_history", lambda _records: ([object()], 0))

    restored = await session_ops_service.warmup_session_context(
        deep_agent=deep_agent, session_id=session_id, refresh_existing=True,
    )
    assert restored is not invalid_history
    assert context_engine.get_context(session_id=session_id) is existing_context
    await context_engine.create_context(session=session)
    expected = ["Cached task"] if invalid_history else ["Research task", "Completed report"]
    assert [message.content for message in existing_context.get_messages()] == expected
    assert not existing_context.get_messages(with_history=False)


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
