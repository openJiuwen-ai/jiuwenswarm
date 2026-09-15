"""Exercise actual execution locks rather than fabricated progress snapshots."""
# pylint: disable=protected-access

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.extensions.video_duplex.backend import video_search


@pytest.mark.asyncio
@pytest.mark.parametrize("same_session,fail_first", [(True, False), (False, False), (True, True)])
async def test_jobs_wait_for_execution_capacity(monkeypatch, same_session, fail_first):
    entered = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()

    async def execute(_client, *, question, **_kwargs):
        index = int(question)
        entered[index].set()
        if index == 0:
            await release.wait()
            if fail_first:
                raise RuntimeError("test failure")
        return {"answer": "done", "realtime_brief": "done"}

    monkeypatch.setattr(video_search, "execute_core_agent", execute)
    channel = SimpleNamespace(send_event=AsyncMock())
    manager = video_search.VideoSearchManager(
        channel, None, log_event=lambda _: None, qwen_active=lambda: True,
        max_concurrency=2 if same_session else 1, max_cached_jobs=1,
    )
    first = manager.start(None, question="0", query="first", search_session_id="a")
    assert first["status"] == "queued"
    await asyncio.wait_for(entered[0].wait(), 2)
    second = manager.start(None, question="1", query="second", search_session_id="a" if same_session else "b")
    try:
        await asyncio.sleep(0.05)
        assert manager._jobs[first["id"]]["status"] == "running"
        assert manager._jobs[second["id"]]["status"] == "queued"
        assert not entered[1].is_set()
        assert len(manager._jobs) == 2  # Active jobs survive cache pressure.
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*list(manager._tasks)), 3)
    assert manager._jobs[first["id"]]["status"] == ("failed" if fail_first else "completed")
    assert manager._jobs[second["id"]]["status"] == "completed"
    stages = [step["stage"] for step in manager._jobs[second["id"]]["progress_history"]]
    assert stages == ["queued", "started", "completed"]


def test_plan_forwards_steps_instead_of_only_count():
    progress = video_search.core_agent_progress({"event_type": "todo.updated", "todos": [
        {"id": "one", "content": "Read sources", "status": "completed"},
        {"id": "two", "content": "Compare findings", "status": "in_progress"},
    ]})
    assert progress["detail"] == "1/2 项已完成"
    assert progress["todos"][1] == {
        "id": "two", "content": "Compare findings", "status": "in_progress",
    }
