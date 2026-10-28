"""#5545/#5486: a stopping task whose "ended" acknowledgement was lost is
settled by Core's confirmation instead of holding the session forever."""

import asyncio

import pytest

from jiuwenswarm.extensions.video_duplex.backend.task_adapter import AgentTaskExecutor
from jiuwenswarm.extensions.video_duplex.backend.tasks import TaskService, TaskStore
from jiuwenswarm.extensions.video_duplex.tests.backend.test_managed_tasks import Executor, until
from jiuwenswarm.extensions.video_duplex.tests.backend.test_unknown_task_settle import (
    SESSION,
    CoreSays,
    Recording,
)


TARGET = {"execution_target": {"mode": "agent", "work_mode": "work"}}


class Bound(Recording):
    """Binds its Core execution, then runs until released; never settles itself."""

    def __init__(self, client, store, result=None):
        super().__init__(client, store)
        self.release = asyncio.Event()
        self.result = result

    async def _run(self, task, progress, context):
        self.store.update(task["id"], lambda t: t.update(execution_bound=True))
        await self.release.wait()
        return self.result

    async def run(self, task, progress):
        self.calls.append(task["id"])
        if len(self.calls) > 1:
            await asyncio.Event().wait()
        return await AgentTaskExecutor.run(self, task, progress)


async def test_repeated_stop_reconciles_cancelling_task_and_dispatches_next(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite")
    # Core accepted the stop but its "ended" acknowledgement never arrives.
    core = CoreSays(probe_settled=True, cancel_settled=False)
    executor = Bound(core, store)
    service = TaskService(store, executor)
    try:
        active = service.submit("alice", SESSION, "a", "打开浏览器", TARGET)
        await until(lambda: store.read(active["id"]).get("execution_bound"))
        second = service.submit("alice", SESSION, "b", "继续")
        await service.cancel("alice", SESSION, active["id"], "stop-1")
        await until(lambda: ("cancel", store.read(active["id"])["request_id"]) in core.requests)
        await asyncio.sleep(0.1)
        assert service.get("alice", SESSION, active["id"])["status"] == "cancelling"
        await service.cancel("alice", SESSION, active["id"], "stop-2")
        await asyncio.wait_for(until(
            lambda: service.get("alice", SESSION, active["id"])["status"] == "cancelled"
        ), 2)
        assert ("probe", store.read(active["id"])["request_id"]) in core.requests
        await asyncio.wait_for(until(lambda: executor.calls == [active["id"], second["id"]]), 2)
    finally:
        await service.close()


async def test_restart_settles_cancelling_task_as_cancelled(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite")
    first = TaskService(store, Executor(store))
    active = first.submit("alice", SESSION, "a", "打开浏览器")
    await until(lambda: first.executor.calls)
    store.update(active["id"], lambda t: t.update(execution_bound=True, status="cancelling"))
    second = first.submit("alice", SESSION, "b", "继续")
    await first.close()

    store = TaskStore(store.path)
    executor = Recording(CoreSays(probe_settled=True), store)
    service = TaskService(store, executor)
    service.start()
    try:
        await asyncio.wait_for(until(
            lambda: service.get("alice", SESSION, active["id"])["status"] == "cancelled"
        ), 2)
        await asyncio.wait_for(until(lambda: executor.calls == [second["id"]]), 2)
    finally:
        await service.close()


@pytest.mark.parametrize("cancelled", [False, True])
async def test_output_end_waits_for_actual_settlement_without_duplex_deadline(tmp_path, monkeypatch, cancelled):
    real_timeout = asyncio.timeout
    # A reintroduced duplex deadline must fail before the assertions below.
    monkeypatch.setattr(asyncio, "timeout", lambda d: real_timeout(0.02 if d == 20 else d))
    store = TaskStore(tmp_path / "tasks.sqlite")
    core = CoreSays(probe_settled=True, cancel_settled=False)
    executor = Bound(core, store, result={"answer": "partial"})
    service = TaskService(store, executor)
    try:
        active = service.submit("alice", SESSION, "a", "打开浏览器", TARGET)
        await until(lambda: store.read(active["id"]).get("execution_bound"))
        second = service.submit("alice", SESSION, "b", "继续")
        if cancelled:
            await service.cancel("alice", SESSION, active["id"], "stop-1")
            await until(lambda: ("cancel", store.read(active["id"])["request_id"]) in core.requests)
        # The output ends before native execution cleanup finishes.
        executor.release.set()
        await asyncio.sleep(0.1)
        assert store.read(active["id"])["status"] == ("cancelling" if cancelled else "running")
        assert store.read(second["id"])["status"] == "queued"
        assert ("probe", store.read(active["id"])["request_id"]) not in core.requests
        store.update(active["id"], lambda t: t.update(
            execution_settled=True, execution_cancelled=cancelled,
        ))
        await until(lambda: store.read(active["id"])["status"] == ("cancelled" if cancelled else "completed"))
        await asyncio.wait_for(until(lambda: executor.calls == [active["id"], second["id"]]), 2)
    finally:
        await service.close()


def test_query_instructions_guide_stopping_unknown_task():
    from jiuwenswarm.extensions.video_duplex.backend.tasks.prompts import task_query_instructions

    text = task_query_instructions(2, {"unknown": 1, "queued": 1}, 2, 2, 2)
    assert "状态待核对" in text
    assert "jiuwen_task_cancel" in text
    assert "never ask the user to re-trigger or re-approve it" in text
