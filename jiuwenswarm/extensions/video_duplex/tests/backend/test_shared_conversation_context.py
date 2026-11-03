from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.foundation.llm import UserMessage

from jiuwenswarm.extensions.video_duplex.backend import conversation_context as sync
from jiuwenswarm.extensions.video_duplex.backend.tasks.service import TaskService


async def test_voice_history_filters_result_echoes_and_blank_or_non_text_records(monkeypatch):
    records = [
        dict(id="user", channel_id="video_duplex", role="user", content="Keep this"),
        dict(id="echo", channel_id="video_duplex", role="assistant", event_type="chat.final",
             presentation="tool_result", content="Already in native history"),
        dict(id="blank", channel_id="video_duplex", role="user", content="  "),
        dict(id="object", channel_id="video_duplex", role="user", content={"text": "not speech"}),
        dict(id="typed", channel_id="web", role="user", content="Native history"),
    ]
    monkeypatch.setattr(sync.session_history, "flush_pending_writes", lambda **_: True)
    monkeypatch.setattr(sync.session_history, "load_history_records", lambda _: records)
    assert await sync.load_voice_history("chat") == [records[0]]


async def test_voice_history_never_reads_unflushed_records(monkeypatch):
    monkeypatch.setattr(sync.session_history, "flush_pending_writes", lambda **_: False)
    read = AsyncMock()
    monkeypatch.setattr(sync.session_history, "load_history_records", read)
    with pytest.raises(RuntimeError, match="still saving"):
        await sync.load_voice_history("chat")
    read.assert_not_called()


def test_saved_chat_tasks_share_session_but_have_distinct_execution_ids():
    # Verify the internal scheduler predicates without starting its worker loop.
    # pylint: disable=protected-access
    first = TaskService._new("alice", "task-duplex:web-chat", "one", {})
    second = TaskService._new("alice", "task-duplex:web-chat", "two", {"independent": True})
    assert first["core_session_id"] == second["core_session_id"] == "web-chat"
    assert first["request_id"] != second["request_id"]
    scheduler = TaskService.__new__(TaskService)
    scheduler.pending_interrupts = set()
    first["status"] = "running"
    assert not scheduler._ready(second, [first, second], [first])
    first["status"] = "completed"
    assert scheduler._ready(second, [first, second], [])
    # Separate saved conversations still permit concurrent execution.
    other = TaskService._new("alice", "task-duplex:other", "three", {"independent": True})
    first["status"] = "running"
    assert scheduler._ready(other, [first, other], [first])


async def test_live_context_imports_speech_once_and_skips_task_ui_duplicates(monkeypatch):
    messages = [UserMessage(content="old text")]
    context = SimpleNamespace(get_messages=lambda: messages)

    async def add(message):
        messages.append(message)

    context.add_messages = AsyncMock(side_effect=add)
    agent = SimpleNamespace(react_agent=SimpleNamespace(
        context_engine=SimpleNamespace(get_context=lambda **_: context, save_contexts=AsyncMock())),
        _interaction_session=SimpleNamespace(get_session_id=lambda: "web-chat"))
    records = [
        dict(id="voice-user", channel_id="video_duplex", role="user", content="我叫小明"),
        dict(id="voice-answer", channel_id="video_duplex", role="assistant", event_type="chat.final", content="你好，小明"),
        dict(
            id="task-result", channel_id="video_duplex", role="assistant",
            event_type="chat.final", presentation="tool_result", content="duplicate",
        ),
    ]
    monkeypatch.setattr(sync.session_history, "flush_pending_writes", lambda **_: True)
    monkeypatch.setattr(sync.session_history, "load_history_records", lambda _: records)
    await sync.sync_conversation_context(agent, "web-chat")
    await sync.sync_conversation_context(agent, "web-chat")
    assert [m.content for m in messages] == ["old text", "我叫小明", "你好，小明"]
    # A warm-restored message is already consumed and must not be imported twice.
    restored = SimpleNamespace(get_messages=lambda: [UserMessage(
        content="我叫小明", metadata={sync.HISTORY_ID: "voice-user"})], add_messages=AsyncMock())

    def get_restored_context(**_):
        return restored

    agent.react_agent.context_engine.get_context = get_restored_context
    await sync.sync_conversation_context(agent, "web-chat")
    assert restored.add_messages.await_count == 1


async def test_imported_speech_survives_next_react_context_initialization(monkeypatch):
    from copy import deepcopy
    from openjiuwen.core.context_engine import ContextEngine, ContextEngineConfig
    from openjiuwen.core.context_engine.token.string_length_counter import StringLengthCounter

    state = {}
    session = SimpleNamespace(
        get_session_id=lambda: "web-chat",
        get_state=lambda key: deepcopy(state.get(key)),
        update_state=lambda values: state.update(deepcopy(values)),
    )
    engine = ContextEngine(ContextEngineConfig(enable_openrouter_model_context_window_tokens=False))
    context = await engine.create_context(session=session, token_counter=StringLengthCounter())
    await context.add_messages(UserMessage(content="Earlier typed question"))
    await engine.save_contexts(session)
    agent = SimpleNamespace(react_agent=SimpleNamespace(context_engine=engine), _interaction_session=session)
    monkeypatch.setattr(sync.session_history, "flush_pending_writes", lambda **_: True)
    monkeypatch.setattr(sync.session_history, "load_history_records", lambda _: [
        dict(id="voice-name", channel_id="video_duplex", role="user", content="My project is Cedar"),
        dict(id="voice-reply", channel_id="video_duplex", role="assistant", event_type="chat.final", content="Cedar noted"),
    ])
    await sync.sync_conversation_context(agent, "web-chat")
    # ReAct calls create_context again before it reads the next typed query.
    restored = await engine.create_context(session=session)
    assert [m.content for m in restored.get_messages()] == [
        "Earlier typed question", "My project is Cedar", "Cedar noted",
    ]
    await sync.sync_conversation_context(agent, "web-chat")
    assert len(restored.get_messages()) == 3
