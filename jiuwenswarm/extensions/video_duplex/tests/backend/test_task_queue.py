"""Exercise actual execution locks rather than fabricated progress snapshots."""
# pylint: disable=protected-access

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


from jiuwenswarm.extensions.video_duplex.tests.backend.task_bridge_support import (
    empty_task_file_query,  # noqa: F401 -- pytest fixture
)

from jiuwenswarm.extensions.video_duplex.backend import video_search


@pytest.mark.asyncio
@pytest.mark.parametrize("same_session,fail_first", [(True, False), (False, False), (True, True)])
async def test_jobs_wait_for_execution_capacity(monkeypatch, same_session, fail_first, tmp_path):
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
        channel,
        None,
        log_event=lambda _: None,
        qwen_active=lambda: True,
        max_concurrency=2 if same_session else 1,
        path=tmp_path / "tasks.sqlite",
        authorize=lambda ws, scope: ("test", scope),
    )
    first = await manager.start(
        None, question="0", query="first", search_session_id="a", command_id="first"
    )
    assert first["status"] == "queued"
    await asyncio.wait_for(entered[0].wait(), 2)
    second = await manager.start(
        None,
        question="1",
        query="second",
        search_session_id="a" if same_session else "b",
        command_id="second",
    )
    try:
        await asyncio.sleep(0.05)
        assert manager.service.store.read(first["id"])["status"] == "running"
        assert manager.service.store.read(second["id"])["status"] == "queued"
        assert not entered[1].is_set()
        assert manager.service.store.read(first["id"])["id"] != second["id"]  # Both persist.
    finally:
        release.set()
        async with asyncio.timeout(3):
            while manager.service.store.read(second["id"])["status"] not in {"completed", "failed"}:
                await asyncio.sleep(0.01)
    assert manager.service.store.read(first["id"])["status"] == ("failed" if fail_first else "completed")
    assert manager.service.store.read(second["id"])["status"] == "completed"
    stages = [step["stage"] for step in manager.service.store.read(second["id"])["progress"]]
    assert stages == ["started"]
    await manager.close()


def test_plan_forwards_steps_instead_of_only_count():
    progress = video_search.core_agent_progress({"event_type": "todo.updated", "todos": [
        {"id": "one", "content": "Read sources", "status": "completed"},
        {"id": "two", "content": "Compare findings", "status": "in_progress"},
    ]})
    assert progress["detail"] == "1/2 项已完成"
    assert progress["todos"][1] == {
        "id": "two", "content": "Compare findings", "status": "in_progress",
    }
