from types import SimpleNamespace
from unittest.mock import AsyncMock

from openjiuwen.core.foundation.llm import UserMessage

from jiuwenswarm.extensions.video_duplex.backend import conversation_context as sync
from jiuwenswarm.extensions.video_duplex.backend.tasks.service import TaskService


def test_saved_chat_tasks_share_session_but_have_distinct_execution_ids():
    # Verify the internal scheduler predicates without starting its worker loop.
    # pylint: disable=protected-access
    first = TaskService._new("alice", "task-duplex:web-chat", "one", {})
    second = TaskService._new("alice", "task-duplex:web-chat", "two", {"independent": True})
    assert first["core_session_id"] == second["core_session_id"] == "web-chat"
    assert first["request_id"] != second["request_id"]
    first["status"] = "running"
    assert not TaskService._ready(second, [first, second], [first])
    first["status"] = "completed"
    assert TaskService._ready(second, [first, second], [])
    # Separate saved conversations still permit concurrent execution.
    other = TaskService._new("alice", "task-duplex:other", "three", {"independent": True})
    first["status"] = "running"
    assert TaskService._ready(other, [first, other], [first])


async def test_live_context_imports_speech_once_and_skips_task_ui_duplicates(monkeypatch):
    messages = [UserMessage(content="old text")]
    context = SimpleNamespace(get_messages=lambda: messages)

    async def add(message):
        messages.append(message)

    context.add_messages = AsyncMock(side_effect=add)
    agent = SimpleNamespace(react_agent=SimpleNamespace(
        context_engine=SimpleNamespace(get_context=lambda **_: context)))
    records = [
        dict(id="voice-user", channel_id="video_duplex", role="user", content="我叫小明"),
        dict(id="voice-answer", channel_id="video_duplex", role="assistant", event_type="chat.final", content="你好，小明"),
        dict(
            id="task-result", channel_id="video_duplex", role="assistant",
            event_type="chat.final", presentation="tool_result", content="duplicate",
        ),
    ]
    monkeypatch.setattr(sync, "flush_pending_writes", lambda **_: True)
    monkeypatch.setattr(sync, "load_history_records", lambda _: records)
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
