"""Recovery must stay inside the conversation that opened the task service."""

import asyncio
import time

import pytest

from jiuwenswarm.extensions.video_duplex.backend.tasks.service import TaskService
from jiuwenswarm.extensions.video_duplex.backend.tasks.store import TaskStore


class Executor:
    """Stands in for the Core bridge: records dispatches and reconciliation probes."""

    def __init__(self):
        self.calls = []
        self.cancels = []
        self.gates = {}

    async def run(self, task, progress):
        self.calls.append(
            {"id": task["id"], "owner": task["owner"], "session": task["session"]}
        )
        gate = asyncio.Event()
        self.gates[task["id"]] = gate
        await gate.wait()
        return "done"

    async def cancel(self, task, *, probe=False):
        self.cancels.append({"id": task["id"], "probe": probe})
        return False

    async def close(self):
        pass


async def until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_submit_dispatches_only_its_own_conversation(tmp_path):
    store = TaskStore(tmp_path / "queued.sqlite")
    executor = Executor()
    service = TaskService(store, executor)
    bob = service.new_task("bob", "task-duplex:bob", "bob pending work", {})
    with store.transaction() as db:
        store.put(db, bob)
    try:
        service.submit("alice", "task-duplex:alice", "cmd-1", "alice work")
        await until(lambda: executor.calls)
        await asyncio.sleep(0.05)
        assert [(c["owner"], c["session"]) for c in executor.calls] == [
            ("alice", "task-duplex:alice")
        ]
        assert store.read(bob["id"])["status"] == "queued"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_submit_leaves_other_conversations_untouched(tmp_path):
    store = TaskStore(tmp_path / "running.sqlite")
    executor = Executor()
    service = TaskService(store, executor)
    bob = service.new_task("bob", "task-duplex:bob", "bob running work", {})
    bob.update(status="running", interaction={"state": "accepted"})
    with store.transaction() as db:
        store.put(db, bob)
    try:
        service.submit("alice", "task-duplex:alice", "cmd-1", "alice work")
        await asyncio.sleep(0.1)
        after = store.read(bob["id"])
        assert after["status"] == "running"
        assert after["interaction"]["state"] == "accepted"
        assert executor.cancels == []

        # Opening bob's conversation is what recovers bob's tasks.
        service.snapshot("bob", "task-duplex:bob")
        await until(lambda: executor.cancels)
        assert executor.cancels[0]["id"] == bob["id"]
        assert store.read(bob["id"])["status"] in {"unknown", "running"}
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_repeated_snapshot_does_not_recover_a_conversation_twice(tmp_path):
    store = TaskStore(tmp_path / "repeat.sqlite")
    executor = Executor()
    service = TaskService(store, executor)
    bob = service.new_task("bob", "task-duplex:bob", "bob running work", {})
    bob.update(status="running")
    with store.transaction() as db:
        store.put(db, bob)
    try:
        service.snapshot("bob", "task-duplex:bob")
        await until(lambda: executor.cancels)
        service.snapshot("bob", "task-duplex:bob")
        await asyncio.sleep(0.05)
        assert len(executor.cancels) == 1
    finally:
        await service.close()
