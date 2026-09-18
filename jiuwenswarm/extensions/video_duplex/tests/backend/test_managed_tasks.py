"""Task business boundaries with real SQLite and controlled execution/stop receipts."""

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.runtime.tasks import TaskService, TaskStore
from jiuwenswarm.runtime.tasks.checkpoint import TaskCheckpoint, bind_task_execution


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


class Executor:
    def __init__(self, store):
        self.store = store
        self.calls, self.stops = [], []
        self.gates = {}
        self.stop_ack = asyncio.Event()
        self.stop_ack.set()
        self.reject_stop = False

    async def run(self, task, progress):
        self.calls.append(task)
        gate = self.gates.setdefault(task["id"], asyncio.Event())
        await gate.wait()
        self.store.update(task["id"], lambda t: t.update(execution_settled=True))
        if task["id"] in self.stops:
            raise RuntimeError("execution stopped")
        return {"answer": task["instruction"]}

    async def cancel(self, task):
        self.stops.append(task["id"])
        await self.stop_ack.wait()
        if self.reject_stop:
            raise RuntimeError("Stop acknowledgement unavailable")
        self.gates[task["id"]].set()
        await until(lambda: self.store.read(task["id"])["execution_settled"])


@pytest.fixture
async def tasks(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite")
    executor = Executor(store)
    service = TaskService(store, executor)
    yield service, executor
    await service.close()


def submit(service, key, text=None, owner="alice", session="conversation"):
    return service.submit(owner, session, key, text or key)


async def test_retry_conflict_scope_and_cancel_before_dispatch(tasks):
    service, executor = tasks
    task = submit(service, "create", "Paris")
    assert submit(service, "create", "Paris")["id"] == task["id"]
    with pytest.raises(ValueError):
        submit(service, "create", "London")
    for owner, session in [("mallory", "conversation"), ("alice", "other")]:
        with pytest.raises(ValueError):
            service.get(owner, session, task["id"])
        with pytest.raises(ValueError):
            await service.cancel(owner, session, task["id"], "stop")
    receipt = await service.cancel("alice", "conversation", task["id"], "stop")
    assert receipt["state"] == "cancelled"
    assert await service.cancel("alice", "conversation", task["id"], "stop") == receipt
    await asyncio.sleep(0.03)
    assert executor.calls == executor.stops == []


async def test_queued_change_and_order_are_authoritative(tasks):
    service, executor = tasks
    first = submit(service, "first")
    await until(lambda: len(executor.calls) == 1)
    second, third = submit(service, "second"), submit(service, "third")
    receipt = service.modify(
        "alice", "conversation", second["id"], "edit", 1, "use French"
    )
    assert receipt["state"] == "queued_input_updated"
    with pytest.raises(ValueError):
        service.reorder("alice", "conversation", third["id"], -1)
    _, version = service.snapshot("alice", "conversation")
    service.reorder("alice", "conversation", third["id"], version)
    executor.gates[first["id"]].set()
    await until(lambda: len(executor.calls) == 2)
    assert executor.calls[1]["id"] == third["id"]
    executor.gates[third["id"]].set()
    await until(lambda: len(executor.calls) == 3)
    assert "use French" in executor.calls[2]["instruction"]
    assert len({t["core_session_id"] for t in executor.calls}) == 3


async def test_preemption_waits_and_late_cancel_cannot_stop_successor(tasks):
    service, executor = tasks
    first = submit(service, "first")
    await until(lambda: len(executor.calls) == 1)
    second = submit(service, "second")
    executor.stop_ack.clear()
    _, version = service.snapshot("alice", "conversation")
    await service.preempt("alice", "conversation", second["id"], version, "preempt")
    await until(lambda: executor.stops)
    assert service.store.read(first["id"])["status"] == "cancelling"
    assert len(executor.calls) == 1
    executor.stop_ack.set()
    await until(lambda: len(executor.calls) == 2)
    assert service.store.read(first["id"])["status"] == "cancelled"
    await service.cancel("alice", "conversation", first["id"], "late-stop")
    assert executor.stops == [first["id"]]


async def test_unconfirmed_stop_preserves_uncertainty(tasks):
    service, executor = tasks
    task = submit(service, "first")
    await until(lambda: executor.calls)
    executor.reject_stop = True
    receipt = await service.cancel("alice", "conversation", task["id"], "stop")
    assert receipt["state"] == "accepted"
    await until(lambda: service.store.read(task["id"])["error"])
    assert service.store.read(task["id"])["status"] == "cancelling"
    submit(service, "waiting")
    await asyncio.sleep(0.03)
    assert len(executor.calls) == 1


async def test_finished_revision_preserves_result_and_replays_receipt(tasks):
    service, executor = tasks
    task = submit(service, "first")
    await until(lambda: executor.calls)
    executor.gates[task["id"]].set()
    await until(lambda: service.store.read(task["id"])["status"] == "completed")
    receipt = service.modify("alice", "conversation", task["id"], "edit", 1, "shorter")
    assert receipt["state"] == "followup"
    assert (
        service.modify("alice", "conversation", task["id"], "edit", 1, "shorter")
        == receipt
    )
    assert service.store.read(task["id"])["result"] == {"answer": "first"}
    child = service.store.read(receipt["successor_id"])
    assert child["parent_id"] == task["id"] and child["request"]["prior_result"]
    with pytest.raises(ValueError):
        service.modify("alice", "conversation", task["id"], "stale", 1, "other")


async def test_restart_does_not_replay_dispatched_effect(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite")
    service = TaskService(store, Executor(store))
    active = submit(service, "effect")
    await until(lambda: service.executor.calls)
    waiting = submit(service, "waiting")
    await service.close()
    replacement = TaskService(store, Executor(store))
    try:
        assert (
            replacement.get("alice", "conversation", active["id"])["status"]
            == "unknown"
        )
        assert (
            replacement.get("alice", "conversation", waiting["id"])["status"]
            == "queued"
        )
        assert submit(replacement, "effect")["id"] == active["id"]
        await asyncio.sleep(0.03)
        assert replacement.executor.calls == []
    finally:
        await replacement.close()


async def test_running_changes_exact_binding_and_model_observation(tasks, monkeypatch):
    service, executor = tasks
    task = submit(service, "first")
    await until(lambda: executor.calls)
    receipt = service.modify(
        "alice", "conversation", task["id"], "edit", 1, "use French"
    )
    assert receipt["state"] == "pending"
    messages = []

    async def add(message):
        messages.append(message)

    ctx = SimpleNamespace(
        extra={"run_context": {"extra": {"managed_task_request": task["request_id"]}}},
        context=SimpleNamespace(add_messages=add),
        inputs=SimpleNamespace(messages=messages),
    )
    checkpoint = TaskCheckpoint(service.store, task, object())
    ctx.extra["run_context"]["extra"]["managed_task_request"] = "stale"
    with pytest.raises(RuntimeError):
        await checkpoint.before_model(ctx)
    assert messages == []
    ctx.extra["run_context"]["extra"]["managed_task_request"] = task["request_id"]
    await checkpoint.before_model(ctx)
    await checkpoint.before_model(ctx)
    assert len(messages) == 1
    assert service.store.read(task["id"])["changes"][0]["state"] == "context_written"
    checkpoint.after_model(ctx)
    assert (
        service.store.read(task["id"])["changes"][0]["state"] == "model_input_observed"
    )
    executor.gates[task["id"]].set()
    await until(lambda: service.store.read(task["id"])["status"] == "completed")
    assert (
        service.store.read(task["id"])["changes"][0]["state"] == "model_input_observed"
    )


async def test_real_core_model_call_observes_change_through_host_binding(
    tasks, monkeypatch
):
    from unittest.mock import MagicMock, AsyncMock
    from openjiuwen.core.foundation.llm import AssistantMessage
    from openjiuwen.core.single_agent.agents.react_agent import (
        ReActAgent,
        ReActAgentConfig,
    )
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard
    from openjiuwen.core.single_agent.rail.base import AgentRail
    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
        JiuSwarmStreamEventRail,
    )
    from jiuwenswarm.runtime.tasks import checkpoint as module

    service, executor = tasks
    task = submit(service, "core-call")
    await until(lambda: executor.calls)
    service.modify(
        "alice", "conversation", task["id"], "edit-core", 1, "answer in French"
    )
    agent = ReActAgent(card=AgentCard(name="task-checkpoint-test"))
    agent.configure(ReActAgentConfig().configure_model("test-model"))
    rail = JiuSwarmStreamEventRail()

    # Exercise actual Core hook dispatch, context rebuild and Host hook entrypoints.
    class HostRail(AgentRail):
        async def before_model_call(self, ctx):
            ctx.extra[rail._SID_KEY] = task["core_session_id"]
            await rail.before_model_call(ctx)

        async def after_model_call(self, ctx):
            await rail.after_model_call(ctx)

    await agent.register_rail(HostRail())
    messages, sent = [], []

    async def add(value, **kwargs):
        messages.extend(value if isinstance(value, list) else [value])
        return messages

    context = MagicMock()
    context.add_messages = AsyncMock(side_effect=add)
    context.get_messages = MagicMock(return_value=messages)
    context.get_context_window = AsyncMock(
        side_effect=lambda **kwargs: SimpleNamespace(
            get_messages=lambda: list(messages), get_tools=lambda: []
        )
    )
    context.pop_messages = AsyncMock(return_value=[])
    engine = MagicMock()
    engine.create_context = AsyncMock(return_value=context)
    engine.save_contexts = AsyncMock()
    agent.context_engine = engine

    async def invoke(*args, **kwargs):
        sent.extend(kwargs.get("messages", args[0] if args else []))
        return AssistantMessage(content="Bonjour")

    model = MagicMock()
    model.invoke = AsyncMock(side_effect=invoke)
    monkeypatch.setattr(agent, "_get_llm", lambda: model)
    session = MagicMock()
    session.get_state.return_value = None
    session.get_session_id.return_value = task["core_session_id"]
    session.write_stream = AsyncMock()
    adapter = SimpleNamespace(
        _instance=SimpleNamespace(_react_agent=agent),
        _is_session_scoped_adapter=True,
        _stream_event_rail=rail,
    )
    request = SimpleNamespace(
        channel_id="video_tool",
        session_id=task["core_session_id"],
        request_id=task["request_id"],
    )
    monkeypatch.setattr(module, "task_database", lambda: service.store.path)
    inputs = {"conversation_id": task["core_session_id"], "query": "hello"}
    with bind_task_execution(request, adapter, inputs):
        from openjiuwen.harness.deep_agent import DeepAgent

        normalized = DeepAgent._normalize_inputs(None, inputs)
        result = await agent.invoke(
            {**inputs, "run_context": normalized.run_context}, session=session
        )
    assert result["output"] == "Bonjour"
    assert any("answer in French" in str(m.content) for m in sent)
    assert (
        service.store.read(task["id"])["changes"][0]["state"] == "model_input_observed"
    )
    assert inputs.get("run") is None
    assert not rail._managed_tasks


async def test_closed_output_waits_for_captured_native_execution(tasks, monkeypatch):
    from jiuwenswarm.runtime.tasks import checkpoint as module
    from openjiuwen.core.runner.callback.errors import AbortError

    service, executor = tasks
    task = submit(service, "native-stop")
    await until(lambda: executor.calls)
    gate = asyncio.Event()
    native = asyncio.create_task(gate.wait())
    root = object()
    rail = SimpleNamespace(_resolve_sid=lambda ctx, session: task["core_session_id"])
    harness = SimpleNamespace(
        _react_agent=root,
        active_round=SimpleNamespace(task_id="native-id"),
        loop_controller=SimpleNamespace(
            task_scheduler=SimpleNamespace(_running_tasks={"native-id": (None, native)})
        ),
    )
    adapter = SimpleNamespace(
        _instance=harness, _stream_event_rail=rail, _is_session_scoped_adapter=True
    )
    request = SimpleNamespace(
        channel_id="video_tool",
        request_id=task["request_id"],
        session_id=task["core_session_id"],
    )
    monkeypatch.setattr(module, "task_database", lambda: service.store.path)
    ctx = SimpleNamespace(
        agent=root,
        session=None,
        extra={"run_context": {"extra": {"managed_task_request": task["request_id"]}}},
    )
    try:
        with module.bind_task_execution(request, adapter, {}):
            await module.task_checkpoint(rail, ctx, "before_model")
        assert not service.store.read(task["id"])["execution_settled"]
        with pytest.raises(AbortError):
            await module.task_checkpoint(rail, ctx, "before_tool")
        gate.set()
        await until(lambda: service.store.read(task["id"])["execution_settled"])
    finally:
        gate.set()
        await native


async def test_same_words_are_distinct_requests_and_late_progress_cannot_cross_tasks(
    tasks,
):
    service, executor = tasks
    callbacks = []
    original = executor.run

    async def run(task, progress):
        callbacks.append(progress)
        return await original(task, progress)

    executor.run = run
    first = submit(service, "one", "same words")
    second = submit(service, "two", "same words")
    assert first["id"] != second["id"]
    await until(lambda: len(executor.calls) == 1)
    executor.gates[first["id"]].set()
    await until(lambda: len(executor.calls) == 2)
    before = service.store.read(second["id"])
    await callbacks[0]({"stage": "tool_result", "content": "late old result"})
    assert service.store.read(second["id"]) == before


async def test_change_after_last_checkpoint_becomes_linked_revision(tasks):
    service, executor = tasks
    task = submit(service, "first")
    await until(lambda: executor.calls)
    service.modify("alice", "conversation", task["id"], "last-edit", 1, "French")
    executor.gates[task["id"]].set()
    await until(lambda: len(executor.calls) == 2)
    saved = service.store.read(task["id"])
    assert saved["result"] == {"answer": "first"}
    assert saved["changes"][0]["state"] == "followup"
    assert executor.calls[1]["parent_id"] == task["id"]
    assert "French" in executor.calls[1]["instruction"]


async def test_second_dispatch_owner_cannot_reset_running_state(tasks):
    import portalocker

    service, executor = tasks
    task = submit(service, "first")
    await until(lambda: executor.calls)
    other = TaskService(service.store, Executor(service.store))
    try:
        with pytest.raises(portalocker.exceptions.LockException):
            other.start()
        assert service.store.read(task["id"])["status"] == "running"
    finally:
        await other.close()


async def test_truncated_agent_stream_is_unknown_not_completed(tasks):
    from jiuwenswarm.extensions.video_duplex.backend.video_search import (
        execute_core_agent,
    )

    service, executor = tasks

    async def stream(request):
        yield SimpleNamespace(
            payload={"event_type": "chat.delta", "content": "partial answer"}
        )

    async def run(task, progress):
        return await execute_core_agent(
            SimpleNamespace(send_request_stream=stream),
            question=task["instruction"],
            query=task["instruction"],
            visual_context="",
            search_session_id=task["session"],
            on_progress=progress,
        )

    executor.run = run
    task = submit(service, "lost-stream")
    await until(lambda: service.store.read(task["id"])["status"] == "unknown")
    assert service.store.read(task["id"])["result"] is None


async def test_completion_receipt_wins_before_stop_finalization(tasks):
    service, executor = tasks
    task = submit(service, "finish-race")
    await until(lambda: executor.calls)

    async def stop(current):
        # Native execution has ended, but its successful output is still in transit.
        service.store.update(current["id"], lambda t: t.update(execution_settled=True))

    executor.cancel = stop
    await service.cancel("alice", "conversation", task["id"], "stop")
    await asyncio.sleep(0.02)
    assert service.store.read(task["id"])["status"] == "cancelling"
    executor.gates[task["id"]].set()
    await until(lambda: service.store.read(task["id"])["status"] == "completed")
    assert service.store.read(task["id"])["result"] == {"answer": "finish-race"}
