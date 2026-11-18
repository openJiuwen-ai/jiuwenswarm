"""History selection at the task-to-Agent request boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.extensions.video_duplex.backend.task_adapter import AgentTaskExecutor
from jiuwenswarm.extensions.video_duplex.backend.tasks import TaskService, TaskStore
from jiuwenswarm.extensions.video_duplex.backend import video_search

# Seed records without executing prior tasks; assertions inspect the outgoing request.
# pylint: disable=protected-access


def seed(store, label, *, owner="alice", session="voice", status="completed", answer=None):
    task = TaskService._new(owner, session, label, {})
    task.update(status=status, result={"answer": answer or "Answer " + label})
    with store.transaction() as db:
        store.put(db, task)
    return task


async def outgoing_request(store, request):
    calls = []

    async def send(envelope):
        if envelope.method == "voice.task.files":
            return SimpleNamespace(ok=True, payload={"files": []})
        calls.append(envelope)
        return SimpleNamespace(ok=True, payload={"content": "Done"})

    task = seed(store, "Current request", status="running")
    task = store.update(task["id"], lambda t: t.update(request=request))
    executor = AgentTaskExecutor(SimpleNamespace(send_request=send), store, None)
    try:
        await executor.run(task, AsyncMock())
        assert len(calls) == 1
        return calls[0].params
    finally:
        await executor.close()


@pytest.mark.parametrize("mode", ["ordinary", "independent", "dependent"])
async def test_history_selection_excludes_unrelated_and_unfinished_results(tmp_path, mode):
    store = TaskStore(tmp_path / "tasks.sqlite")
    prior = [seed(store, f"History-{index}") for index in range(8)]
    for owner, session, status in [
        ("bob", "voice", "completed"), ("alice", "other", "completed"),
        ("alice", "voice", "running"), ("alice", "voice", "failed"),
    ]:
        seed(store, "Must not be shared", owner=owner, session=session, status=status)
    request = {"independent": mode != "ordinary"}
    if mode == "dependent":
        request["depends_on"] = [prior[0]["id"], prior[7]["id"]]
    params = await outgoing_request(store, request)
    expected = list(range(2, 8)) if mode == "ordinary" else [0, 7] if mode == "dependent" else []
    assert [item["question"] for item in params["video_delegation_context"]] == [
        f"History-{index}" for index in expected
    ]
    assert "Must not be shared" not in params["query"]
    for index in expected:
        assert f"Answer History-{index}" in params["query"]


async def test_revision_keeps_prior_answer_and_reports_files_without_reading_them(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite")
    path = tmp_path / "plan.md"
    path.write_text("FILE_BODY_MUST_NOT_BE_INJECTED", encoding="utf-8")
    previous = seed(store, "Plan", answer="Original plan")
    store.update(previous["id"], lambda t: t["result"].update(files=[{"name": "plan.md", "path": str(path)}]))
    params = await outgoing_request(store, {
        "independent": True, "depends_on": [previous["id"]],
        "prior_result": {"answer": "Revision basis"},
    })
    context = params["video_delegation_context"]
    assert context[0]["files"] == [{"name": "plan.md", "path": str(path)}]
    assert context[-1]["result"] == "Revision basis"
    assert "Original plan" in params["query"] and "Revision basis" in params["query"]
    assert "FILE_BODY_MUST_NOT_BE_INJECTED" not in params["query"]


def test_rendered_history_keeps_recent_six_and_bounds_long_context():
    # The bound applies to rendered model input, not the stored result or RPC metadata.
    items = [{"question": f"History-{index}", "result": f"Answer-{index}"} for index in range(8)]
    rendered = video_search._delegation_context_text(items)
    assert "History-0" not in rendered and "History-1" not in rendered
    assert all(f"Answer-{index}" in rendered for index in range(2, 8))
    for item in items:
        item["result"] = "x" * 2000
    items[-1]["result"] += "MUST_BE_TRUNCATED"
    bounded = video_search._delegation_context_text(items)
    assert 0 < len(bounded) <= 8000
    assert "MUST_BE_TRUNCATED" not in bounded
    assert items[-1]["result"].endswith("MUST_BE_TRUNCATED")
