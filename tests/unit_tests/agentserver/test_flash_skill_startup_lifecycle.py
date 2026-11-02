"""Background build scheduling must not depend on diagnostic instrumentation."""

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.flash.skill_selection import execution
from jiuwenswarm.agents.harness.flash.skill_selection import startup_lifecycle as lifecycle


def test_preparation_does_not_create_runtime_when_selection_disabled(monkeypatch):
    gate = execution.StartupGate()
    monkeypatch.setattr(execution, "_runtime", None)
    monkeypatch.setattr(execution, "_startup_gate", gate)
    with lifecycle.preparing("disabled"):
        assert gate.stats()["startups"] == 1
        assert execution._runtime is None
    assert gate.stats()["startups"] == 0


def test_gate_yields_until_lease_closed_and_close_is_idempotent():
    gate = execution.StartupGate(max_pause_s=5)
    lease = gate.startup()
    entered, finished = threading.Event(), threading.Event()

    def run():
        entered.set()
        gate.checkpoint()
        finished.set()

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(1)
        assert not finished.wait(0.03)
        lease.close()
        lease.close()
        assert finished.wait(1)
        assert gate.stats()["startups"] == 0
    finally:
        gate.close()
        worker.join(1)


def test_gate_has_bounded_wait_even_under_continuous_startup():
    gate = execution.StartupGate(max_pause_s=0.025)
    lease = gate.startup()
    started = time.monotonic()
    gate.checkpoint()
    assert time.monotonic() - started >= 0.02
    assert gate.stats()["startups"] == 1
    lease.close()


def test_concurrent_lease_close_preserves_other_active_preparation():
    gate = execution.StartupGate()
    lease = gate.startup()
    other = gate.startup()
    barrier = threading.Barrier(8)

    def close_lease():
        barrier.wait(timeout=2)
        lease.close()

    workers = [threading.Thread(target=close_lease) for _ in range(8)]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(2)
        assert not any(worker.is_alive() for worker in workers)
        assert gate.stats()["startups"] == 1
        other.close()
        assert gate.stats()["startups"] == 0
    finally:
        barrier.abort()
        for worker in workers:
            if worker.ident is not None:
                worker.join(2)
        lease.close()
        other.close()
        gate.close()


def test_demand_wakes_a_build_and_releases_on_exception():
    gate = execution.StartupGate(max_pause_s=5)
    lease = gate.startup()
    finished = threading.Event()
    worker = threading.Thread(target=lambda: (gate.checkpoint(), finished.set()))
    worker.start()
    try:
        assert not finished.wait(0.03)
        with pytest.raises(RuntimeError):
            with gate.demand():
                assert finished.wait(1)
                raise RuntimeError("cancelled search")
        assert gate.stats()["demands"] == 0
    finally:
        lease.close()
        gate.close()
        worker.join(1)


def test_running_build_is_cooperative_and_runtime_shutdown_wakes_it():
    runtime = execution.SelectionRuntime(build_workers=1, build_queue=0)
    runtime.gate.max_pause_s = 60
    lease = runtime.gate.startup()
    entered = threading.Event()

    def work():
        entered.set()
        execution.checkpoint()
        return "complete"

    token = execution.build_gate.set(runtime.gate)
    try:
        future = runtime.builds.submit(work)
    finally:
        execution.build_gate.reset(token)
    stopped = threading.Event()
    shutdown = threading.Thread(target=lambda: (runtime.shutdown(), stopped.set()))
    try:
        assert entered.wait(1)
        # Running Python code cannot be paused or stopped by Future.cancel().
        assert not future.cancel()
        assert not future.done()
        shutdown.start()
        assert stopped.wait(2)
        assert future.result() == "complete"
    finally:
        runtime.gate.close()
        if shutdown.ident is not None:
            shutdown.join(2)
        else:
            runtime.shutdown()
        lease.close()


class _React:
    def _apply_llm_call_kwargs(self, ctx, kwargs):
        return kwargs


class _Instance:
    """Reuse a worker task whose ContextVars were copied during the first round."""

    def __init__(self, gate):
        self.gate = gate
        self.react_agent = _React()
        self.controller = SimpleNamespace(task_scheduler=SimpleNamespace(execute_task=self.execute))
        self.queue = asyncio.Queue()
        self.worker = None
        self.observed = []
        self.fail = False

    async def _setup_task_loop(self):
        if self.worker is None:
            self.worker = asyncio.create_task(self.consume())
        return None, self.controller

    async def consume(self):
        while True:
            task_id, completed = await self.queue.get()
            try:
                result = await self.controller.task_scheduler.execute_task(task_id, None)
            except BaseException as exc:
                completed.set_exception(exc)
            else:
                completed.set_result(result)

    async def _execute_round(self, work):
        return await self.run_one_round(work, work.request_id + "-task", None)

    async def run_one_round(self, work, task_id, session):
        await self._setup_task_loop()
        completed = asyncio.get_running_loop().create_future()
        await self.queue.put((task_id, completed))
        return await completed

    async def execute(self, task_id, session):
        preparation = lifecycle._current.get()
        self.observed.append((preparation.request_id if preparation else None,
                              self.gate.stats()["startups"]))
        if self.fail:
            raise RuntimeError("before model failed")
        kwargs = self.react_agent._apply_llm_call_kwargs(None, {"unchanged": True})
        self.observed.append(("model", self.gate.stats()["startups"]))
        return kwargs

    async def stop(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_first_and_continuation_bind_actual_request_without_timing(monkeypatch):
    monkeypatch.setenv("FLASH_STARTUP_TIMING", "0")
    gate = execution.StartupGate()
    monkeypatch.setattr(lifecycle, "get_startup_gate", lambda: gate)
    requests = {}
    instance = _Instance(gate)
    original_class_method = _React._apply_llm_call_kwargs
    lifecycle.bind_instance(instance, requests)
    lifecycle.bind_instance(instance, requests)  # Idempotent on reload.
    try:
        for request_id in ("first", "continuation"):
            with lifecycle.preparing(request_id) as preparation:
                requests[request_id] = preparation
                result = await instance._execute_round(SimpleNamespace(request_id=request_id))
                assert result == {"unchanged": True}
                assert gate.stats()["startups"] == 0
                requests.pop(request_id)
        assert instance.observed == [("first", 1), ("model", 0),
                                     ("continuation", 1), ("model", 0)]
        assert not instance.flash_preparation_tasks
        assert _React._apply_llm_call_kwargs is original_class_method
        # An unrelated task must not inherit the first request from the worker.
        await instance._execute_round(SimpleNamespace(request_id="background"))
        assert instance.observed[-2:] == [(None, 0), ("model", 0)]
    finally:
        await instance.stop()


@pytest.mark.asyncio
async def test_failure_before_model_releases_lease_and_task_mapping(monkeypatch):
    gate = execution.StartupGate()
    monkeypatch.setattr(lifecycle, "get_startup_gate", lambda: gate)
    requests = {}
    instance = _Instance(gate)
    instance.fail = True
    lifecycle.bind_instance(instance, requests)
    try:
        with lifecycle.preparing("failed") as preparation:
            requests["failed"] = preparation
            with pytest.raises(RuntimeError, match="before model failed"):
                await instance._execute_round(SimpleNamespace(request_id="failed"))
            assert gate.stats()["startups"] == 0
            assert not instance.flash_preparation_tasks
    finally:
        await instance.stop()


@pytest.mark.asyncio
async def test_cancellation_releases_preparation(monkeypatch):
    gate = execution.StartupGate()
    monkeypatch.setattr(lifecycle, "get_startup_gate", lambda: gate)
    entered = asyncio.Event()

    async def prepare():
        with lifecycle.preparing("cancelled"):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(prepare())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gate.stats()["startups"] == 0


def test_nested_creation_keeps_outer_lease_until_model_boundary(monkeypatch):
    gate = execution.StartupGate()
    monkeypatch.setattr(lifecycle, "get_startup_gate", lambda: gate)
    with lifecycle.preparing("request") as outer:
        with lifecycle.preparing("request") as inner:
            assert outer is inner
            assert gate.stats()["startups"] == 1
        assert gate.stats()["startups"] == 1
    assert gate.stats()["startups"] == 0


@pytest.mark.asyncio
async def test_real_sdk_reused_scheduler_releases_each_request_before_model(monkeypatch):
    """Exercise the actual SDK controller with a local model double, no network."""
    from openjiuwen.core.single_agent import AgentCard, create_agent_session
    from openjiuwen.harness.deep_agent import DeepAgent
    from openjiuwen.harness.schema.interaction import RoundWorkItem

    monkeypatch.setenv("FLASH_STARTUP_TIMING", "0")
    gate = execution.StartupGate()
    monkeypatch.setattr(lifecycle, "get_startup_gate", lambda: gate)
    agent = DeepAgent(AgentCard(name="flash-startup-gate-test"))
    session = create_agent_session(session_id="flash-startup-gate", card=agent.card)
    observed = []

    async def model_invoke(inputs, sdk_session, **kwargs):
        preparation = lifecycle._current.get()
        observed.append(preparation.request_id)
        assert gate.stats()["startups"] == 1
        agent.react_agent._apply_llm_call_kwargs(None, {})
        assert gate.stats()["startups"] == 0
        await asyncio.sleep(0)
        return {"result_type": "answer", "output": "test"}

    agent._react_agent = SimpleNamespace(
        invoke=model_invoke,
        write_invoke_result_to_stream=AsyncMock(),
        _apply_llm_call_kwargs=lambda ctx, kwargs: kwargs,
    )
    lifecycle.bind_instance(agent, {})
    try:
        with lifecycle.preparing("old-worker-context"):
            _, controller = await agent.prepare_interaction_task_loop(session)
        scheduler = controller.task_scheduler
        for request_id in ("first", "continuation"):
            with lifecycle.preparing(request_id):
                outcome = await asyncio.wait_for(agent.run_one_round(
                    RoundWorkItem.user(request_id=request_id, inputs={"query": "test"}),
                    request_id + "-task", session,
                ), timeout=5)
                assert outcome.error_code is None
                assert gate.stats()["startups"] == 0
            assert controller.task_scheduler is scheduler
            assert not agent.flash_preparation_tasks
        assert observed == ["first", "continuation"]
    finally:
        await agent._force_cleanup_controller()
