import json
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.extensions.video_duplex.backend import realtime_context as context


def history(count):
    return [dict(id=f"turn-{i}", role="user" if i % 2 == 0 else "assistant",
                 event_type="chat.final", content=f"Exact conversation statement {i}", timestamp=i)
            for i in range(count)]


@pytest.fixture
def saved(monkeypatch, tmp_path):
    records = history(12)
    monkeypatch.setattr(context, "flush_pending_writes", lambda **_: True)
    monkeypatch.setattr(context, "load_history_records", lambda _: records)
    monkeypatch.setattr(context, "resolve_session_dir", lambda _: (tmp_path, None))
    return records


def test_transcript_preserves_roles_and_does_not_promote_assistant_claims():
    records = history(2) + [
        dict(id="reason", role="assistant", event_type="chat.reasoning", content="private reasoning"),
        dict(id="reported", role="assistant", event_type="chat.final", presentation="tool_result", content="Core reports a file"),
    ]
    turns = context.transcript(records)
    assert len(turns) == 3
    assert turns[1]["origin"] == "conversation"
    assert turns[2]["origin"] == "core_report"
    assert context.validated_memory([{"source_id": turns[0]["source_id"], "text": "Task completed"}], turns) == []


async def test_short_history_needs_no_compressor_and_has_current_tasks(saved, monkeypatch):
    del saved[2:]
    compressor = AsyncMock()
    monkeypatch.setattr(context, "select_memory", compressor)
    result = await context.prepare_context("chat-a", "alice", lambda: {"jobs": [], "queue_version": 2})
    assert len(result["recent_turns"]) == 2
    assert result["memory"] == []
    assert result["tasks"]["queue_version"] == 2
    compressor.assert_not_called()


async def test_memory_is_cached_but_task_status_is_fresh(saved, monkeypatch):
    async def select(older):
        return [{"source_id": older[0]["source_id"], "text": older[0]["text"]}]
    compressor = AsyncMock(side_effect=select)
    monkeypatch.setattr(context, "select_memory", compressor)
    status = "running"
    tasks = lambda: {"jobs": [{"job_id": "job-a", "status": status, "revision": 1}]}
    first = await context.prepare_context("chat-a", "alice", tasks)
    assert first["memory_method"] == "selected_excerpts"
    status = "completed"
    second = await context.prepare_context("chat-a", "alice", tasks)
    assert second["memory_method"] == "cached_excerpts"
    assert second["tasks"]["jobs"][0]["status"] == "completed"
    assert compressor.await_count == 1
    saved[0]["content"] = "Corrected historical statement"
    await context.prepare_context("chat-a", "alice", tasks)
    assert compressor.await_count == 2
    await context.prepare_context("chat-a", "bob", tasks)
    assert compressor.await_count == 3


async def test_hallucinated_memory_falls_back_to_actual_excerpts(saved, monkeypatch):
    monkeypatch.setattr(context, "select_memory", AsyncMock(return_value=[{
        "source_id": "turn-0:0", "text": "I deleted all files successfully",
    }]))
    result = await context.prepare_context("chat-a", "alice", lambda: {"jobs": []})
    assert result["memory_method"] == "fallback_excerpts"
    assert "deleted all" not in json.dumps(result)
    assert result["memory"][0]["text"] == saved[0]["content"]


async def test_compressor_failure_uses_bounded_fallback(saved, monkeypatch):
    monkeypatch.setattr(context, "select_memory", AsyncMock(side_effect=TimeoutError))
    saved.extend(history(40))
    for record in saved:
        record["content"] += '"\\' * 2000
    tasks = lambda: {"jobs": [dict(job_id=str(i), status="running", question="x" * 8000) for i in range(30)]}
    result = await context.prepare_context("chat-a", "alice", tasks)
    assert len(json.dumps(result, ensure_ascii=False)) <= context.MAX_CONTEXT_CHARS
    assert result["context_incomplete"]
    assert result["tasks"]["omitted_jobs"] == 30


async def test_task_snapshot_is_read_after_compression(saved, monkeypatch):
    ready = False
    async def select(older):
        nonlocal ready
        ready = True
        return [{"source_id": older[0]["source_id"], "text": older[0]["text"]}]
    def tasks():
        assert ready
        return {"jobs": []}
    monkeypatch.setattr(context, "select_memory", select)
    await context.prepare_context("chat-a", "alice", tasks)


async def test_failed_history_flush_prevents_restore(saved, monkeypatch):
    monkeypatch.setattr(context, "flush_pending_writes", lambda **_: False)
    with pytest.raises(RuntimeError, match="finished saving"):
        await context.prepare_context("chat-a", "alice", lambda: {"jobs": []})


async def test_empty_history_and_no_tasks_returns_no_background(saved):
    saved.clear()
    assert await context.prepare_context("chat-a", "alice", lambda: {"jobs": []}) is None


async def test_shared_restorer_preserves_history_and_refreshes_only_task_facts(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    package = {"session_id": "chat-a", "memory": [{"role": "user", "text": "Remember Cedar"}],
               "recent_turns": [], "tasks": {"jobs": []}, "context_incomplete": False}
    prepare = AsyncMock(return_value=package)
    monkeypatch.setattr(context, "prepare_context", prepare)
    manager = SimpleNamespace(
        authorized_scope=AsyncMock(return_value=("alice", "task-duplex:chat-a")),
        snapshot=Mock(return_value={"jobs": [{"job_id": "job-a", "status": "running"}]}),
    )
    restorer = context.RealtimeContextRestorer(manager)
    assert await restorer.prepare(None, "standalone") is None
    manager.authorized_scope.assert_not_called()
    envelope = await restorer.prepare(None, "task-duplex:chat-a")
    assert context.RESTORE_INSTRUCTIONS in envelope["instructions"]
    assert json.loads(envelope["instructions"].rsplit("\n", 1)[1]) == package
    refreshed = await restorer.instructions(None, "task-duplex:chat-a", envelope["token"])
    current = json.loads(refreshed.rsplit("\n", 1)[1])
    assert current["memory"] == package["memory"]
    assert current["tasks"]["jobs"][0]["status"] == "running"
    assert package["tasks"]["jobs"] == []
    assert prepare.await_count == 1
    manager.authorized_scope.return_value = ("bob", "task-duplex:chat-a")
    with pytest.raises(ValueError, match="unavailable"):
        await restorer.instructions(None, "task-duplex:chat-a", envelope["token"])
