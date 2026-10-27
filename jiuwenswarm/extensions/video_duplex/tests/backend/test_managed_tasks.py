"""Task business boundaries with real SQLite and controlled execution/stop receipts."""

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.extensions.video_duplex.tests.backend.task_bridge_support import LocalCheckpoint, install_bridge

from jiuwenswarm.extensions.video_duplex.backend.tasks import TaskService, TaskStore
from jiuwenswarm.extensions.video_duplex.backend.tasks.service import TaskModification
from jiuwenswarm.extensions.video_duplex.backend.tasks.checkpoint import TaskCheckpoint
from jiuwenswarm.extensions.video_duplex.backend.tasks.execution import bind_task_execution
from jiuwenswarm.extensions.video_duplex.backend.tasks.rail import task_checkpoint


class TaskAdapter(SimpleNamespace):
    @property
    def task_execution_binding(self):
        return self._instance, self._voice_agent_task_rail


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
        "alice", "conversation", second["id"], "edit", TaskModification(1, "use French")
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
    service.concurrency = 1
    task = independent(service, "first")
    await until(lambda: executor.calls)
    executor.reject_stop = True
    receipt = await service.cancel("alice", "conversation", task["id"], "stop")
    assert receipt["state"] == "accepted"
    await until(lambda: service.store.read(task["id"])["error"])
    assert service.store.read(task["id"])["status"] == "cancelling"
    waiting = independent(service, "waiting")
    await asyncio.sleep(0.03)
    assert len(executor.calls) == 1
    assert not service.store.read(task["id"])["execution_settled"]
    service.store.update(task["id"], lambda t: t.update(execution_cancelled=True))
    executor.gates[task["id"]].set()
    await until(lambda: len(executor.calls) == 2)
    assert executor.calls[1]["id"] == waiting["id"]


async def test_finished_revision_preserves_result_and_replays_receipt(tasks):
    service, executor = tasks
    task = submit(service, "first")
    await until(lambda: executor.calls)
    executor.gates[task["id"]].set()
    await until(lambda: service.store.read(task["id"])["status"] == "completed")
    receipt = service.modify(
        "alice", "conversation", task["id"], "edit", TaskModification(1, "shorter")
    )
    assert receipt["state"] == "followup"
    assert (
        service.modify(
            "alice", "conversation", task["id"], "edit", TaskModification(1, "shorter")
        )
        == receipt
    )
    assert service.store.read(task["id"])["result"] == {"answer": "first"}
    child = service.store.read(receipt["successor_id"])
    assert child["parent_id"] == task["id"] and child["request"]["prior_result"]
    with pytest.raises(ValueError):
        service.modify(
            "alice", "conversation", task["id"], "stale", TaskModification(1, "other")
        )


async def test_restart_does_not_replay_dispatched_effect(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite")
    service = TaskService(store, Executor(store))
    active = submit(service, "effect")
    await until(lambda: service.executor.calls)
    waiting = submit(service, "waiting")
    await service.close()
    reopened = TaskStore(store.path)
    replacement = TaskService(reopened, Executor(reopened))
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
        "alice", "conversation", task["id"], "edit", TaskModification(1, "use French")
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
    checkpoint = TaskCheckpoint(LocalCheckpoint(service.store, task), task, object())
    ctx.extra["run_context"]["extra"]["managed_task_request"] = "stale"
    with pytest.raises(RuntimeError):
        await checkpoint.before_model(ctx)
    assert messages == []
    ctx.extra["run_context"]["extra"]["managed_task_request"] = task["request_id"]
    await checkpoint.before_model(ctx)
    await checkpoint.before_model(ctx)
    assert len(messages) == 1
    assert service.store.read(task["id"])["changes"][0]["state"] == "context_written"
    ctx.inputs.messages = []
    await checkpoint.after_model(ctx)
    assert service.store.read(task["id"])["changes"][0]["state"] == "context_written"
    ctx.inputs.messages = messages
    await checkpoint.after_model(ctx)
    assert (
        service.store.read(task["id"])["changes"][0]["state"] == "model_input_observed"
    )
    executor.gates[task["id"]].set()
    await until(lambda: service.store.read(task["id"])["status"] == "completed")
    assert (
        service.store.read(task["id"])["changes"][0]["state"] == "model_input_observed"
    )


@pytest.mark.parametrize("write_before_error", [False, True])
async def test_context_write_failure_never_claims_success_or_replays(tasks, write_before_error):
    service, executor = tasks
    task = submit(service, "write-failure")
    await until(lambda: executor.calls)
    service.modify("alice", "conversation", task["id"], "edit", TaskModification(1, "French"))
    writes = []

    async def fail_write(message):
        if write_before_error:
            writes.append(message)
        raise RuntimeError("context write acknowledgement lost")

    ctx = SimpleNamespace(
        extra={"run_context": {"extra": {"managed_task_request": task["request_id"]}}},
        context=SimpleNamespace(add_messages=fail_write), inputs=SimpleNamespace(messages=writes),
    )
    checkpoint = TaskCheckpoint(LocalCheckpoint(service.store, task), task, object())
    with pytest.raises(RuntimeError, match="acknowledgement lost"):
        await checkpoint.before_model(ctx)
    assert service.store.read(task["id"])["changes"][0]["state"] == "claimed"
    await checkpoint.before_model(ctx)
    await checkpoint.after_model(ctx)
    assert len(writes) == int(write_before_error)
    assert service.store.read(task["id"])["changes"][0]["state"] == "claimed"
    executor.gates[task["id"]].set()
    await until(lambda: service.store.read(task["id"])["status"] == "completed")
    assert service.store.read(task["id"])["changes"][0]["state"] == "unknown"


@pytest.mark.parametrize("stage", ["before_model", "after_model", "before_tool"])
async def test_old_model_callbacks_cannot_modify_a_new_execution_of_same_task(tasks, stage):
    from openjiuwen.core.runner.callback.errors import AbortError

    service, executor = tasks
    task = submit(service, "old-execution")
    await until(lambda: executor.calls)
    root = object()
    old = TaskCheckpoint(LocalCheckpoint(service.store, task), task, root)
    service.store.update(task["id"], lambda t: t.update(
        request_id="new-execution",
        changes=[{"id": "edit", "instruction": "French", "state": "context_written"}],
    ))
    current = service.store.read(task["id"])
    rail = SimpleNamespace(
        managed_tasks={task["core_session_id"]: TaskCheckpoint(LocalCheckpoint(service.store, current), current, root)},
    )
    ctx = SimpleNamespace(
        agent=root, session=None,
        extra={"run_context": {"extra": {"managed_task_request": old.request_id}}},
        inputs=SimpleNamespace(messages=[{"content": '{"managed_task_change":"edit"}'}]),
    )
    with pytest.raises(AbortError):
        await task_checkpoint(rail, ctx, stage)
    assert service.store.read(task["id"]) == current
    assert len(executor.calls) == 1


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
    from jiuwenswarm.extensions.video_duplex.backend.tasks.rail import VoiceAgentTaskRail

    service, executor = tasks
    task = submit(service, "core-call")
    await until(lambda: executor.calls)
    service.modify(
        "alice",
        "conversation",
        task["id"],
        "edit-core",
        TaskModification(1, "answer in French"),
    )
    agent = ReActAgent(card=AgentCard(name="task-checkpoint-test"))
    agent.configure(ReActAgentConfig().configure_model("test-model"))
    rail = VoiceAgentTaskRail()

    # Exercise the independently registered rail through actual Core hook dispatch.
    await agent.register_rail(rail)
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
    adapter = TaskAdapter(
        _instance=SimpleNamespace(_react_agent=agent),
        _is_session_scoped_adapter=True,
        _voice_agent_task_rail=rail,
    )
    request = SimpleNamespace(
        channel_id="video_tool",
        session_id=task["core_session_id"],
        request_id=task["request_id"],
    )
    install_bridge(monkeypatch, service.store, request)
    inputs = {"conversation_id": task["core_session_id"], "query": "hello"}
    async with bind_task_execution(request, adapter, inputs):
        from openjiuwen.harness.deep_agent import DeepAgent

        # Deliberately exercise Core input normalization for this integration contract.
        normalized = DeepAgent._normalize_inputs(None, inputs)  # pylint: disable=protected-access
        result = await agent.invoke(
            {**inputs, "run_context": normalized.run_context}, session=session
        )
    assert result["output"] == "Bonjour"
    assert any("answer in French" in str(m.content) for m in sent)
    assert (
        service.store.read(task["id"])["changes"][0]["state"] == "model_input_observed"
    )
    assert inputs.get("run") is None
    assert not rail.managed_tasks


async def test_cancelled_invocation_before_first_model_is_observed(tasks, monkeypatch):
    from jiuwenswarm.extensions.video_duplex.backend.tasks import execution as module

    service, executor = tasks
    task = submit(service, "cancel-during-setup")
    await until(lambda: executor.calls)
    adapter = TaskAdapter(
        _instance=SimpleNamespace(_react_agent=object()),
        _is_session_scoped_adapter=True,
        _voice_agent_task_rail=SimpleNamespace(managed_tasks={}),
    )
    request = SimpleNamespace(
        channel_id="video_tool",
        session_id=task["core_session_id"],
        request_id=task["request_id"],
    )
    install_bridge(monkeypatch, service.store, request)
    with pytest.raises(asyncio.CancelledError):
        async with module.bind_task_execution(request, adapter, {}):
            raise asyncio.CancelledError()
    stopped = service.store.read(task["id"])
    assert stopped["execution_settled"] and stopped["execution_cancelled"]


async def test_cancelled_invocation_captured_as_current_task_settles_inline(
    tasks, monkeypatch
):
    from jiuwenswarm.extensions.video_duplex.backend.tasks import execution as module

    service, executor = tasks
    task = submit(service, "cancel-captured-request")
    await until(lambda: executor.calls)
    root = object()
    rail = SimpleNamespace(managed_tasks={})
    harness = SimpleNamespace(
        _react_agent=root,
        active_round=SimpleNamespace(task_id="direct-resume"),
        loop_controller=SimpleNamespace(
            task_scheduler=SimpleNamespace(_running_tasks={})
        ),
    )
    adapter = TaskAdapter(
        _instance=harness,
        _is_session_scoped_adapter=True,
        _voice_agent_task_rail=rail,
    )
    request = SimpleNamespace(
        channel_id="video_tool",
        session_id=task["core_session_id"],
        request_id=task["request_id"],
    )
    install_bridge(monkeypatch, service.store, request)
    ctx = SimpleNamespace(
        agent=root,
        session=None,
        extra={
            "run_context": {
                "extra": {"managed_task_request": task["request_id"]}
            }
        },
    )

    with pytest.raises(asyncio.CancelledError):
        async with module.bind_task_execution(request, adapter, {}):
            await task_checkpoint(rail, ctx, "before_model")
            raise asyncio.CancelledError()

    stopped = service.store.read(task["id"])
    assert stopped["execution_settled"] and stopped["execution_cancelled"]


async def test_cancelled_invocation_ignores_stale_scheduler_handle(tasks, monkeypatch):
    from jiuwenswarm.extensions.video_duplex.backend.tasks import execution as module

    service, executor = tasks
    task = submit(service, "cancel-stale-scheduler")
    await until(lambda: executor.calls)
    release = asyncio.Event()
    stale = asyncio.create_task(release.wait())
    root = object()
    rail = SimpleNamespace(managed_tasks={})
    harness = SimpleNamespace(
        _react_agent=root,
        active_round=SimpleNamespace(task_id="stale-native"),
        loop_controller=SimpleNamespace(
            task_scheduler=SimpleNamespace(
                _running_tasks={"stale-native": (None, stale)}
            )
        ),
    )
    adapter = TaskAdapter(
        _instance=harness,
        _is_session_scoped_adapter=True,
        _voice_agent_task_rail=rail,
    )
    request = SimpleNamespace(
        channel_id="video_tool",
        session_id=task["core_session_id"],
        request_id=task["request_id"],
    )
    install_bridge(monkeypatch, service.store, request)
    ctx = SimpleNamespace(
        agent=root,
        session=None,
        extra={
            "run_context": {
                "extra": {"managed_task_request": task["request_id"]}
            }
        },
    )

    try:
        with pytest.raises(asyncio.CancelledError):
            async with module.bind_task_execution(request, adapter, {}):
                await task_checkpoint(rail, ctx, "before_model")
                raise asyncio.CancelledError()

        stopped = service.store.read(task["id"])
        assert stopped["execution_settled"] and stopped["execution_cancelled"]
        assert not stale.done()
    finally:
        release.set()
        await stale


@pytest.mark.parametrize("swallowed_cancel", [False, True])
async def test_closed_output_waits_for_captured_native_execution(
    tasks, monkeypatch, swallowed_cancel
):
    from jiuwenswarm.extensions.video_duplex.backend.tasks import execution as module
    from openjiuwen.core.runner.callback.errors import AbortError

    service, executor = tasks
    task = submit(service, "native-stop")
    await until(lambda: executor.calls)
    gate = asyncio.Event()

    async def native_execution():
        try:
            await gate.wait()
        except asyncio.CancelledError:
            # Core's scheduler drains cancellation without re-raising it.
            pass

    native = asyncio.create_task(native_execution())
    await asyncio.sleep(0)
    root = object()
    rail = SimpleNamespace(managed_tasks={})
    harness = SimpleNamespace(
        _react_agent=root,
        active_round=SimpleNamespace(task_id="native-id"),
        loop_controller=SimpleNamespace(
            task_scheduler=SimpleNamespace(_running_tasks={"native-id": (None, native)})
        ),
    )
    adapter = TaskAdapter(
        _instance=harness, _voice_agent_task_rail=rail, _is_session_scoped_adapter=True
    )
    request = SimpleNamespace(
        channel_id="video_tool",
        request_id=task["request_id"],
        session_id=task["core_session_id"],
    )
    install_bridge(monkeypatch, service.store, request)
    ctx = SimpleNamespace(
        agent=root,
        session=None,
        extra={"run_context": {"extra": {"managed_task_request": task["request_id"]}}},
    )
    try:
        async with module.bind_task_execution(request, adapter, {}):
            await task_checkpoint(rail, ctx, "before_model")
        assert not service.store.read(task["id"])["execution_settled"]
        with pytest.raises(AbortError):
            await task_checkpoint(rail, ctx, "before_tool")
        if swallowed_cancel:
            native.cancel()
        else:
            gate.set()
        await until(lambda: service.store.read(task["id"])["execution_settled"])
        assert service.store.read(task["id"])["execution_cancelled"] is swallowed_cancel
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
    before = service.snapshot("alice", "conversation")
    await callbacks[0]({"stage": "tool_result", "content": "late old result"})
    assert service.snapshot("alice", "conversation") == before


async def test_change_after_last_checkpoint_becomes_linked_revision(tasks):
    service, executor = tasks
    task = submit(service, "first")
    await until(lambda: executor.calls)
    service.modify(
        "alice", "conversation", task["id"], "last-edit", TaskModification(1, "French")
    )
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


def independent(service, key, *, resources=None, depends_on=None):
    return service.submit(
        "alice",
        "conversation",
        key,
        key,
        {
            "independent": True,
            "resources": resources or [],
            "depends_on": depends_on or [],
        },
    )


async def test_independent_weather_finishes_while_paris_runs_and_capacity_queues(tasks):
    service, executor = tasks
    paris = submit(service, "Paris itinerary")
    await until(lambda: len(executor.calls) == 1)
    weather = independent(service, "Hangzhou weather")
    third = independent(service, "Read separate file")
    await until(lambda: len(executor.calls) == 2)
    assert service.store.read(third["id"])["status"] == "queued"
    assert executor.calls[0]["core_session_id"] != executor.calls[1]["core_session_id"]
    executor.gates[weather["id"]].set()
    await until(lambda: len(executor.calls) == 3)
    assert service.store.read(paris["id"])["status"] == "running"
    assert service.store.read(weather["id"])["result"]["answer"] == "Hangzhou weather"
    await service.cancel("alice", "conversation", paris["id"], "cancel-paris")
    await until(lambda: service.store.read(paris["id"])["status"] == "cancelled")
    assert executor.stops == [paris["id"]]
    assert service.store.read(third["id"])["status"] == "running"
    executor.gates[third["id"]].set()
    await until(lambda: service.store.read(third["id"])["status"] == "completed")
    assert service.store.read(third["id"])["result"]["answer"] == "Read separate file"


async def test_unspecified_resources_allow_two_independent_tasks_and_cancel_isolated(
    tasks,
):
    service, executor = tasks
    items = [
        service.submit("alice", "conversation", city, city, {"independent": True})
        for city in ("Hangzhou", "Suzhou", "Beijing")
    ]
    await until(lambda: len(executor.calls) == 2)
    assert {t["id"] for t in executor.calls} == {t["id"] for t in items[:2]}
    assert service.store.read(items[2]["id"])["status"] == "queued"
    await service.cancel("alice", "conversation", items[0]["id"], "stop-hangzhou")
    await until(lambda: len(executor.calls) == 3)
    assert service.store.read(items[0]["id"])["status"] == "cancelled"
    assert service.store.read(items[1]["id"])["status"] == "running"


async def test_reasoning_burst_and_slow_subscriber_do_not_hold_result_or_capacity(
    tmp_path, monkeypatch,
):
    from jiuwenswarm.extensions.video_duplex.backend.tasks import service as module

    monkeypatch.setattr(
        module, "time",
        SimpleNamespace(time=module.time.time, time_ns=module.time.time_ns, monotonic=lambda: 10.0),
    )
    store = TaskStore(tmp_path / "tasks.sqlite")
    subscriber_started, release_subscriber = asyncio.Event(), asyncio.Event()
    delivered = []

    async def notify(task):
        subscriber_started.set()
        await release_subscriber.wait()
        delivered.append((task["id"], task["status"]))

    class StreamingExecutor:
        async def run(self, task, progress):
            await subscriber_started.wait()
            for _ in range(2000):
                await progress({"stage": "reasoning", "content": "杭"})
            await progress({"stage": "file", "path": "outputs/Hangzhou.md"})
            store.update(task["id"], lambda t: t.update(execution_settled=True))
            return {"answer": "Saved outputs/Hangzhou.md"}

    service = TaskService(store, StreamingExecutor(), on_change=notify, concurrency=1)
    try:
        first = submit(service, "first")
        second = submit(service, "second")
        await until(lambda: store.read(second["id"])["status"] == "completed")
        assert store.read(first["id"])["result"]["answer"].startswith("Saved")
        history = store.read(first["id"])["progress"]
        reasoning = [p for p in history if p["stage"] == "reasoning"]
        assert "".join(p["content"] for p in reasoning) == "杭" * 2000
        assert len(reasoning) < 20
        assert not delivered  # Still blocked, yet both results are persisted.
        release_subscriber.set()
        await until(lambda: (second["id"], "completed") in delivered)
        assert delivered[-1][1] == "completed"
    finally:
        release_subscriber.set()
        await service.close()


async def test_dependencies_and_conflicting_resources_wait_but_unrelated_work_runs(
    tasks,
):
    service, executor = tasks
    first = independent(service, "write report", resources=["C:/Work/report.txt"])
    conflict = independent(service, "read report", resources=["c:\\work\\report.txt"])
    dependent = independent(service, "summarize report", depends_on=[first["id"]])
    weather = independent(service, "weather")
    await until(lambda: len(executor.calls) == 2)
    assert {t["id"] for t in executor.calls} == {first["id"], weather["id"]}
    # Reordering cannot override dependency or resource eligibility.
    for target in (conflict, dependent):
        version = service.snapshot("alice", "conversation")[1]
        service.reorder("alice", "conversation", target["id"], version)
    executor.gates[weather["id"]].set()
    await until(lambda: service.store.read(weather["id"])["status"] == "completed")
    assert service.store.read(conflict["id"])["status"] == "queued"
    assert service.store.read(dependent["id"])["status"] == "queued"
    assert len(executor.calls) == 2
    executor.gates[first["id"]].set()
    await until(lambda: len(executor.calls) == 4)
    assert {t["id"] for t in executor.calls[2:]} == {conflict["id"], dependent["id"]}


async def test_dependency_scope_and_cancelled_dependency_fail_closed(tasks):
    service, executor = tasks
    foreign = submit(service, "foreign", session="other")
    with pytest.raises(ValueError):
        independent(service, "wrong scope", depends_on=[foreign["id"]])
    parent = submit(service, "parent")
    child = independent(service, "child", depends_on=[parent["id"]])
    await service.cancel("alice", "conversation", parent["id"], "stop-parent")
    await asyncio.sleep(0.03)
    assert service.store.read(child["id"])["status"] == "queued"
    assert child["id"] not in [t["id"] for t in executor.calls]


async def test_parallel_preempt_rejects_ambiguous_cancel_target(tasks):
    service, executor = tasks
    independent(service, "one")
    independent(service, "two")
    pending = independent(service, "three")
    await until(lambda: len(executor.calls) == 2)
    _, version = service.snapshot("alice", "conversation")
    with pytest.raises(ValueError, match="Multiple tasks"):
        await service.preempt(
            "alice", "conversation", pending["id"], version, "preempt"
        )
    assert executor.stops == []


@pytest.mark.parametrize("is_independent, count", [(True, 2), (False, 1)])
async def test_unknown_history_does_not_block_other_conversation(tasks, is_independent, count):
    service, executor = tasks
    old = submit(service, "old", session="old-conversation")
    service.store.update(old["id"], lambda t: t.update(status="unknown"))
    create = independent if is_independent else submit
    new = [create(service, f"new-{i}") for i in range(count)]
    await until(lambda: len(executor.calls) == count)
    assert {t["id"] for t in executor.calls} == {t["id"] for t in new}


async def test_reorder_version_ignores_progress_but_detects_dispatch(tasks):
    service, executor = tasks
    first = submit(service, "active")
    await until(lambda: len(executor.calls) == 1)
    second, third = submit(service, "waiting-a"), submit(service, "waiting-b")
    _, version = service.snapshot("alice", "conversation")
    service.store.update(first["id"], lambda t: t["progress"].append({"stage": "reasoning", "content": "progress"}))
    assert service.snapshot("alice", "conversation")[1] == version
    service.reorder("alice", "conversation", third["id"], version)
    changed = service.snapshot("alice", "conversation")[1]
    assert changed != version
    with pytest.raises(ValueError, match="Queue changed"):
        service.reorder("alice", "conversation", second["id"], version)
    with pytest.raises(ValueError, match="no longer waiting"):
        service.reorder("alice", "conversation", first["id"], changed)
    executor.gates[first["id"]].set()
    await until(lambda: len(executor.calls) == 2)
    assert executor.calls[1]["id"] == third["id"]
    assert service.snapshot("alice", "conversation")[1] != changed
    with pytest.raises(ValueError, match="Queue changed"):
        service.reorder("alice", "conversation", second["id"], changed)


async def test_cancel_receipt_does_not_claim_stop_before_ack(tasks):
    service, executor = tasks
    task = submit(service, "cancel-with-ack")
    await until(lambda: len(executor.calls) == 1)
    executor.stop_ack.clear()
    receipt = await service.cancel("alice", "conversation", task["id"], "cancel-op")
    assert receipt["state"] == "accepted"
    assert receipt["task_status"] == "cancelling" and receipt["stopped"] is False
    assert service.get("alice", "conversation", task["id"])["status"] == "cancelling"
    executor.stop_ack.set()


async def test_cancelled_execution_final_text_is_not_success(tasks):
    service, executor = tasks

    async def partial(task, progress):
        service.store.update(task["id"], lambda t: t.update(
            execution_cancelled=True, execution_settled=True, status="cancelling"))
        return {"answer": "I will start researching"}
    executor.run = partial
    task = submit(service, "cancelled-partial")
    await until(lambda: service.get("alice", "conversation", task["id"])["status"] == "cancelled")
    record = service.get("alice", "conversation", task["id"])
    assert record["result"] is None
    assert record["partial_result"]["answer"] == "I will start researching"


async def test_completed_cancel_preserves_resource_context_for_core_revision(tasks):
    service, executor = tasks
    task = submit(service, "create daily reminder")
    await until(lambda: executor.calls)
    executor.gates[task["id"]].set()
    await until(lambda: service.store.read(task["id"])["status"] == "completed")
    original = {"answer": "Created cron-reminder-1", "files": []}
    service.store.update(task["id"], lambda current: current.update(result=original))
    receipt = await service.cancel("alice", "conversation", task["id"], "stop-finished")
    assert receipt["task_status"] == "completed"
    assert receipt["stopped"] is False
    assert "仍可另行委托 Core 修改或继续处理" in receipt["message"]
    current = service.store.read(task["id"])
    revision = service.modify(
        "alice", "conversation", task["id"], "manage-reminder",
        TaskModification(current["revision"], "Query and delete cron-reminder-1; verify deletion"),
    )
    assert revision["state"] == "followup"
    child = service.store.read(revision["successor_id"])
    assert child["request"]["prior_result"] == original
    assert "cron-reminder-1" in child["instruction"]
    assert service.store.read(task["id"])["result"] == original


async def test_titles_persist_search_and_revision_without_replacing_instructions(tasks):
    service, executor = tasks
    task = service.submit("alice", "conversation", "titled", "请查询香港今天的天气并说明是否带伞", {"title": "查询香港天气"})
    await until(lambda: executor.calls)
    restored = TaskStore(service.store.path).read(task["id"])
    assert restored["title"] == "查询香港天气"
    assert restored["instruction"] == task["instruction"]
    assert service.list("alice", "conversation", query="查询香港天气")[0][0]["id"] == task["id"]
    assert service.list("alice", "conversation", query="是否带伞")[0][0]["id"] == task["id"]
    executor.gates[task["id"]].set()
    await until(lambda: service.store.read(task["id"])["status"] == "completed")
    receipt = service.modify("alice", "conversation", task["id"], "english", TaskModification(1, "改成英文", "用英文介绍香港天气"))
    child = service.store.read(receipt["successor_id"])
    assert child["title"] == "用英文介绍香港天气"
    assert child["request"]["prior_result"]["answer"] == task["instruction"]
    assert service.store.read(task["id"])["title"] == "查询香港天气"
    assert service.modify("alice", "conversation", task["id"], "english", TaskModification(1, "改成英文", "用英文介绍香港天气")) == receipt


async def test_queued_title_change_preserves_full_requirement_and_legacy_replay(tasks):
    service, executor = tasks
    first = submit(service, "first")
    await until(lambda: executor.calls)
    queued = service.submit("alice", "conversation", "second", "修改刚才的打卡提醒", {"title": "调整打卡提醒时间"})
    service.modify("alice", "conversation", queued["id"], "time", TaskModification(1, "改到九点", "修改打卡提醒为九点"))
    updated = service.store.read(queued["id"])
    assert updated["title"] == "修改打卡提醒为九点"
    assert "修改刚才的打卡提醒" in updated["instruction"] and "改到九点" in updated["instruction"]
    assert submit(service, "first")["id"] == first["id"]
    service.store.update(first["id"], lambda task: task.pop("title", None))
    assert service.list("alice", "conversation", query="first")[0][0]["id"] == first["id"]


def _round_harness(round_task=None):
    harness = SimpleNamespace(
        _react_agent=object(),
        active_round=SimpleNamespace(task_id="round-id"),
        loop_controller=SimpleNamespace(task_scheduler=SimpleNamespace(_running_tasks={})),
    )
    if round_task is not None:
        harness._interaction_round_task = round_task
    return harness


@pytest.mark.parametrize("has_round_task", [True, False])
async def test_5545_stopped_round_without_scheduler_handle_still_settles(
    tasks, monkeypatch, has_round_task
):
    """#5545: the output closes while the stopped round (e.g. CUA cleanup) still
    unwinds outside the scheduler map; its end must still be acknowledged."""
    from jiuwenswarm.extensions.video_duplex.backend.tasks import execution as module

    service, executor = tasks
    task = submit(service, "computer-use-stop")
    await until(lambda: executor.calls)
    gate = asyncio.Event()
    round_task = asyncio.create_task(gate.wait()) if has_round_task else None
    harness = _round_harness(round_task)
    rail = SimpleNamespace(managed_tasks={})
    adapter = TaskAdapter(
        _instance=harness, _voice_agent_task_rail=rail, _is_session_scoped_adapter=True
    )
    request = SimpleNamespace(
        channel_id="video_tool", request_id=task["request_id"], session_id=task["core_session_id"]
    )
    install_bridge(monkeypatch, service.store, request)
    try:
        async with module.bind_task_execution(request, adapter, {}):
            pass
        await asyncio.sleep(0.1)
        assert not service.store.read(task["id"])["execution_settled"]
        if has_round_task:
            gate.set()
        else:
            harness.active_round = None
        await until(lambda: service.store.read(task["id"])["execution_settled"])
    finally:
        gate.set()
        if round_task is not None:
            await round_task


async def test_5545_failed_stop_request_still_ends_cancelled_when_execution_ends(tasks):
    """#5545: a stop request that errors must not leave the task 'stopping' forever
    once the execution has actually ended."""
    service, executor = tasks
    service.concurrency = 1
    task = independent(service, "first")
    await until(lambda: executor.calls)
    executor.reject_stop = True
    await service.cancel("alice", "conversation", task["id"], "stop")
    await until(lambda: service.store.read(task["id"])["error"])
    executor.gates[task["id"]].set()
    await until(lambda: service.store.read(task["id"])["status"] == "cancelled")


async def test_5544_capacity_wait_names_the_other_conversations_task(tasks):
    """#5544: with one desktop slot, a queued task names exactly what it waits on."""
    service, executor = tasks
    service.concurrency = 1
    running = service.submit("alice", "task-duplex:a", "a", "open the browser")
    await until(lambda: executor.calls)
    waiting = service.submit("alice", "task-duplex:b", "b", "fill the form")
    service.submit("bob", "task-duplex:c", "c", "bob private")
    snapshot, _ = service.snapshot("alice", "task-duplex:b")
    queued = next(t for t in snapshot if t["id"] == waiting["id"])
    assert queued["wait_reason"] == "capacity_order_or_resource"
    assert queued["waiting_on"] == [dict(
        id=running["id"], title=running.get("title") or running["instruction"],
        status="running", search_session_id="task-duplex:a",
    )]


async def test_restored_open_card_refuses_a_new_task_before_binding(tasks, monkeypatch):
    # #5487: a card restored from disk would take the new voice task as its answer.
    # Refusing before bind keeps the task failed rather than unknown.
    service, executor = tasks
    task = submit(service, "card-open", session="task-duplex:chat")
    await until(lambda: executor.calls)
    card = SimpleNamespace(interrupted_tools={"call_1": object()})
    adapter = TaskAdapter(
        _instance=SimpleNamespace(
            _react_agent=object(), loop_session=SimpleNamespace(get_state=lambda _key: card),
        ),
        _is_session_scoped_adapter=True,
        _voice_agent_task_rail=SimpleNamespace(managed_tasks={}),
    )
    request = SimpleNamespace(
        channel_id="video_tool", session_id=task["core_session_id"], request_id=task["request_id"],
    )
    install_bridge(monkeypatch, service.store, request)
    with pytest.raises(RuntimeError, match="确认卡片"):
        async with bind_task_execution(request, adapter, {"query": "追加一行"}):
            pytest.fail("the task must not run while a card is open")
    assert not service.store.read(task["id"]).get("execution_bound")
