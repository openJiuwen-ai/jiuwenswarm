"""#5486: an unknown task is settled by Core's confirmation, not held forever."""

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.message import ReqMethod

from jiuwenswarm.extensions.video_duplex.backend.task_adapter import AgentTaskExecutor
from jiuwenswarm.extensions.video_duplex.backend.tasks import TaskService, TaskStore
from jiuwenswarm.extensions.video_duplex.tests.backend.test_managed_tasks import Executor, until

SESSION = "task-duplex:s"


class CoreSays:
    """Fake Core: replies to cancel/probe with a settled fact."""

    server_ready = True
    agent_ready = True

    def __init__(self, *, probe_settled, cancel_settled=True):
        self.probe_settled, self.cancel_settled = probe_settled, cancel_settled
        self.requests = []
        self.health_requests = 0

    async def send_request(self, request):
        if request.method == ReqMethod.VOICE_TASK_PENDING_INTERRUPT.value:
            return SimpleNamespace(ok=True, payload={"pending": False})
        if request.method == ReqMethod.HEALTH_CHECK_GET_CONF.value:
            self.health_requests += 1
            if not self.server_ready:
                raise ConnectionError("Core is disconnected")
            return SimpleNamespace(ok=True, payload={
                "agent_ready": getattr(self, "runtime_ready", self.agent_ready),
            })
        probe = bool((request.params or {}).get("managed_task_probe"))
        self.requests.append(("probe" if probe else "cancel", request.params["managed_task_request"]))
        settled = self.probe_settled if probe else self.cancel_settled
        return SimpleNamespace(ok=True, payload={"success": True, "managed_task_settled": settled})


class Recording(AgentTaskExecutor):
    def __init__(self, client, store):
        super().__init__(client, store, None)
        self.calls = []

    async def run(self, task, progress):
        self.calls.append(task["id"])
        await asyncio.Event().wait()


async def lost_task(tmp_path):
    """A bound, unsettled task left unknown by a restart, plus a queued second task."""
    store = TaskStore(tmp_path / "tasks.sqlite")
    first = TaskService(store, Executor(store))
    active = first.submit("alice", SESSION, "a", "打开浏览器")
    await until(lambda: first.executor.calls)
    store.update(active["id"], lambda t: t.update(execution_bound=True))
    second = first.submit("alice", SESSION, "b", "继续")
    await first.close()
    return TaskStore(store.path), active["id"], second["id"]


async def test_cancel_unknown_task_settles_via_core_and_dispatches_next(tmp_path):
    store, active, second = await lost_task(tmp_path)
    core = CoreSays(probe_settled=False)
    executor = Recording(core, store)
    service = TaskService(store, executor)
    service.start()
    try:
        await asyncio.sleep(0.05)
        assert service.get("alice", SESSION, active)["status"] == "running"
        assert executor.calls == []
        await service.cancel("alice", SESSION, active, "stop-a")
        await asyncio.wait_for(
            until(lambda: service.get("alice", SESSION, active)["status"] == "cancelled"), 2
        )
        await until(lambda: executor.calls == [second])
        assert ("cancel", store.read(active)["request_id"]) in core.requests
    finally:
        await service.close()


async def test_restart_reconciles_unknown_task_and_dispatches_queue(tmp_path):
    store, active, second = await lost_task(tmp_path)
    executor = Recording(CoreSays(probe_settled=True), store)
    service = TaskService(store, executor)
    service.start()
    try:
        await until(lambda: service.get("alice", SESSION, active)["status"] == "failed")
        assert service.get("alice", SESSION, active)["error"] == "执行已中断，未重放"
        await until(lambda: executor.calls == [second])
    finally:
        await service.close()


async def test_restart_restores_running_when_core_still_runs_it(tmp_path):
    store, active, _ = await lost_task(tmp_path)
    executor = Recording(CoreSays(probe_settled=False), store)
    service = TaskService(store, executor)
    service.start()
    try:
        await asyncio.sleep(0.1)
        assert service.get("alice", SESSION, active)["status"] == "running"
        assert executor.calls == []
    finally:
        await service.close()


@pytest.mark.parametrize("server_ready, agent_ready", [(False, False), (True, False), (False, True)])
async def test_waits_for_ready_core_then_queries_once_even_on_not_found(
    tmp_path, monkeypatch, server_ready, agent_ready,
):
    from jiuwenswarm.extensions.video_duplex.backend.tasks import service as module

    monkeypatch.setattr(module, "RECONCILE_INTERVAL", 0.01)
    store, active, second = await lost_task(tmp_path)

    class MissingCore(CoreSays):
        agent_ready = False

        async def send_request(self, request):
            if request.method in {ReqMethod.HEALTH_CHECK_GET_CONF.value, ReqMethod.VOICE_TASK_PENDING_INTERRUPT.value}:
                return await super().send_request(request)
            self.requests.append(request)
            return SimpleNamespace(ok=False, payload={"code": "NOT_FOUND"})

    core = MissingCore(probe_settled=False)
    core.server_ready, core.agent_ready = server_ready, agent_ready
    executor = Recording(core, store)
    service = TaskService(store, executor)
    service.start()
    try:
        await asyncio.sleep(0.05)
        assert core.requests == []
        assert store.read(active)["status"] == "unknown"
        core.server_ready = core.agent_ready = True
        await until(lambda: store.read(active)["status"] == "failed")
        assert "NOT_FOUND" in store.read(active)["error"]
        await until(lambda: executor.calls == [second])
        await asyncio.sleep(0.05)
        assert len(core.requests) == 1
    finally:
        await service.close()
    assert service.reconcile_monitor.done()
    assert not service.reconciles


async def test_late_settlement_of_runtime_unknown_releases_queue(tmp_path, monkeypatch):
    from jiuwenswarm.extensions.video_duplex.backend.tasks import service as module
    from jiuwenswarm.extensions.video_duplex.backend.tasks.bridge import GatewayTaskEndpoint

    monkeypatch.setattr(module, "RECONCILE_INTERVAL", 0.01)
    store = TaskStore(tmp_path / "tasks.sqlite")

    class LostResult(Recording):
        async def run(self, task, progress):
            self.calls.append(task["id"])
            if len(self.calls) > 1:
                await asyncio.Event().wait()
            store.update(task["id"], lambda t: t.update(execution_bound=True))
            raise module.ExecutionUncertain("Final result was lost")

    core = CoreSays(probe_settled=False)
    executor = LostResult(core, store)
    service = TaskService(store, executor)
    endpoint = GatewayTaskEndpoint(store)
    try:
        active = service.submit("alice", SESSION, "a", "打开浏览器")
        second = service.submit("alice", SESSION, "b", "继续")
        await until(lambda: store.read(active["id"])["status"] == "unknown")
        await until(lambda: len(core.requests) > 0)
        current = store.read(active["id"])
        endpoint.execute({
            "action": "settle", "task_id": current["id"], "owner": current["owner"],
            "session_id": current["core_session_id"], "request_id": current["request_id"],
            "command_id": "late-settlement", "cancelled": False,
        })
        await until(lambda: store.read(active["id"])["status"] == "failed")
        assert store.read(active["id"])["result"] is None
        await until(lambda: executor.calls == [active["id"], second["id"]])
    finally:
        await service.close()
        endpoint.close()


async def test_retry_preserves_stop_intent_after_restart(tmp_path, monkeypatch):
    from jiuwenswarm.extensions.video_duplex.backend.tasks import service as module

    monkeypatch.setattr(module, "RECONCILE_INTERVAL", 0.01)
    store, active, second = await lost_task(tmp_path)
    store.update(active, lambda t: t.update(status="cancelling"))
    core = CoreSays(probe_settled=False)
    executor = Recording(core, store)
    service = TaskService(store, executor)
    service.start()
    try:
        await until(lambda: len(core.requests) > 0)
        await until(lambda: store.read(active)["status"] == "cancelling")
        store.update(active, lambda t: t.update(execution_settled=True, output_closed=True))
        await until(lambda: store.read(active)["status"] == "cancelled")
        assert len(core.requests) == 1
        await until(lambda: executor.calls == [second])
    finally:
        await service.close()


async def test_reconcile_probes_do_not_overlap_and_close_cancels_them(tmp_path, monkeypatch):
    from jiuwenswarm.extensions.video_duplex.backend.tasks import service as module

    monkeypatch.setattr(module, "RECONCILE_INTERVAL", 0.01)
    store, _, _ = await lost_task(tmp_path)

    class SlowCore(CoreSays):
        async def send_request(self, request):
            if request.method in {ReqMethod.HEALTH_CHECK_GET_CONF.value, ReqMethod.VOICE_TASK_PENDING_INTERRUPT.value}:
                return await super().send_request(request)
            self.requests.append(request)
            await asyncio.Event().wait()

    core = SlowCore(probe_settled=False)
    service = TaskService(store, Recording(core, store))
    service.start()
    try:
        await until(lambda: len(core.requests) > 0)
        await asyncio.sleep(0.06)
        assert len(core.requests) == 1
    finally:
        await service.close()
    assert not service.reconciles
    assert not service.reconciling


@pytest.mark.parametrize("status, expected", [
    ("completed", "completed"), ("failed", "failed"), ("cancelled", "cancelled"),
    ("unknown", "failed"), (None, "failed"),
])
async def test_reconcile_uses_execution_status_not_rpc_success(tmp_path, monkeypatch, status, expected):
    from jiuwenswarm.extensions.video_duplex.backend.tasks import service as module

    monkeypatch.setattr(module, "RECONCILE_INTERVAL", 0.01)
    store, active, second = await lost_task(tmp_path)
    if status == "completed":
        store.update(active, lambda t: t.update(cancellation_requested=True))

    class OutcomeCore(CoreSays):
        async def send_request(self, request):
            if request.method in {ReqMethod.HEALTH_CHECK_GET_CONF.value, ReqMethod.VOICE_TASK_PENDING_INTERRUPT.value}:
                return await super().send_request(request)
            self.requests.append(request)
            payload = {"success": True}
            if status is not None:
                payload["managed_task_status"] = status
            if status == "completed":
                payload["managed_task_result"] = {"answer": "done"}
            return SimpleNamespace(ok=True, payload=payload)

    core = OutcomeCore(probe_settled=False)
    executor = Recording(core, store)
    service = TaskService(store, executor)
    service.start()
    try:
        await until(lambda: store.read(active)["status"] == expected)
        assert store.read(active)["result"] == ({"answer": "done"} if expected == "completed" else None)
        await until(lambda: executor.calls == [second])
        await asyncio.sleep(0.05)
        assert len(core.requests) == 1
    finally:
        await service.close()


async def test_recovered_running_task_waits_for_late_completion_without_another_query(tmp_path, monkeypatch):
    from jiuwenswarm.extensions.video_duplex.backend.tasks import service as module

    monkeypatch.setattr(module, "RECONCILE_INTERVAL", 0.01)
    store, active, second = await lost_task(tmp_path)
    core = CoreSays(probe_settled=False)
    executor = Recording(core, store)
    service = TaskService(store, executor)
    service.start()
    try:
        await until(lambda: store.read(active)["status"] == "running")
        await asyncio.sleep(0.05)
        assert len(core.requests) == 1
        assert executor.calls == []
        store.update(active, lambda t: t.update(
            execution_settled=True, output_closed=True, observed_result={"answer": "done"},
        ))
        await until(lambda: store.read(active)["status"] == "completed")
        assert store.read(active)["result"] == {"answer": "done"}
        await until(lambda: executor.calls == [second])
        assert len(core.requests) == 1
    finally:
        await service.close()


async def test_complete_stream_result_survives_settlement_timeout_and_is_not_queried_twice(tmp_path, monkeypatch):
    from jiuwenswarm.extensions.video_duplex.backend.tasks import service as module

    monkeypatch.setattr(module, "RECONCILE_INTERVAL", 0.01)
    real_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda seconds: real_timeout(0.01 if seconds == 20 else seconds))
    store = TaskStore(tmp_path / "tasks.sqlite")

    class CompletedStream(Recording):
        async def _run(self, task, progress, context):
            store.update(task["id"], lambda t: t.update(execution_bound=True))
            return {"answer": "done"}

        async def run(self, task, progress):
            self.calls.append(task["id"])
            if len(self.calls) > 1:
                await asyncio.Event().wait()
            return await AgentTaskExecutor.run(self, task, progress)

    core = CoreSays(probe_settled=False)
    executor = CompletedStream(core, store)
    service = TaskService(store, executor)
    try:
        active = service.submit("alice", SESSION, "a", "执行", {
            "execution_target": {"mode": "agent", "work_mode": "work"},
        })
        second = service.submit("alice", SESSION, "b", "继续")
        await until(lambda: store.read(active["id"]).get("recovery_observer"))
        assert store.read(active["id"])["observed_result"] == {"answer": "done"}
        assert len(core.requests) == 1
        current = store.read(active["id"])
        executor.endpoint.execute({
            "action": "settle", "task_id": current["id"], "owner": current["owner"],
            "session_id": current["core_session_id"], "request_id": current["request_id"],
            "command_id": "late-completion", "cancelled": False,
        })
        await until(lambda: store.read(active["id"])["status"] == "completed")
        assert store.read(active["id"])["result"] == {"answer": "done"}
        await until(lambda: executor.calls == [active["id"], second["id"]])
        assert len(core.requests) == 1
    finally:
        await service.close()


async def test_core_failure_is_not_treated_as_successful_settlement(tmp_path, monkeypatch):
    from jiuwenswarm.extensions.video_duplex.backend.tasks.service import ExecutionUncertain

    real_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda seconds: real_timeout(0.01 if seconds == 20 else seconds))
    store, active, _ = await lost_task(tmp_path)

    class FailedCore(CoreSays):
        async def send_request(self, request):
            if request.method in {ReqMethod.HEALTH_CHECK_GET_CONF.value, ReqMethod.VOICE_TASK_PENDING_INTERRUPT.value}:
                return await super().send_request(request)
            self.requests.append(request)
            return SimpleNamespace(ok=True, payload={
                "success": True, "managed_task_status": "failed", "managed_task_settled": True,
            })

    core = FailedCore(probe_settled=True)
    executor = Recording(core, store)
    try:
        with pytest.raises(ExecutionUncertain):
            await executor.wait_settled(store.read(active))
        assert not store.read(active)["execution_settled"]
        assert (await executor.reconcile(store.read(active)))["status"] == "failed"
        assert len(core.requests) == 1
    finally:
        await executor.close()


@pytest.mark.parametrize("cached_ready", [False, True])
async def test_live_readiness_overrides_stale_connection_ack(tmp_path, monkeypatch, cached_ready):
    from jiuwenswarm.extensions.video_duplex.backend.tasks import service as module

    monkeypatch.setattr(module, "RECONCILE_INTERVAL", 0.01)
    store, active, second = await lost_task(tmp_path)
    core = CoreSays(probe_settled=True)
    core.agent_ready = cached_ready
    core.runtime_ready = False
    executor = Recording(core, store)
    service = TaskService(store, executor)
    service.start()
    try:
        await until(lambda: core.health_requests >= 2)
        assert store.read(active)["status"] == "unknown"
        assert core.requests == []
        assert executor.calls == []
        # The Runtime becomes ready without reconnecting or receiving a new ack.
        core.runtime_ready = True
        await until(lambda: store.read(active)["status"] == "failed")
        await until(lambda: executor.calls == [second])
        await asyncio.sleep(0.05)
        assert core.agent_ready is cached_ready
        assert len(core.requests) == 1
    finally:
        await service.close()


async def test_failed_readiness_request_does_not_consume_the_single_task_query(tmp_path, monkeypatch):
    from jiuwenswarm.extensions.video_duplex.backend.tasks import service as module

    monkeypatch.setattr(module, "RECONCILE_INTERVAL", 0.01)
    store, active, second = await lost_task(tmp_path)

    class RecoveringHealth(CoreSays):
        unavailable = True

        async def send_request(self, request):
            if request.method == ReqMethod.HEALTH_CHECK_GET_CONF.value and self.unavailable:
                self.health_requests += 1
                raise TimeoutError("Readiness request timed out")
            return await super().send_request(request)

    core = RecoveringHealth(probe_settled=True)
    executor = Recording(core, store)
    service = TaskService(store, executor)
    service.start()
    try:
        await until(lambda: core.health_requests >= 2)
        assert store.read(active)["status"] == "unknown"
        assert core.requests == []
        core.unavailable = False
        await until(lambda: store.read(active)["status"] == "failed")
        await until(lambda: executor.calls == [second])
        assert len(core.requests) == 1
    finally:
        await service.close()


async def test_concurrent_tasks_share_inflight_readiness_request(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowHealth(CoreSays):
        async def send_request(self, request):
            assert request.method == ReqMethod.HEALTH_CHECK_GET_CONF.value
            self.health_requests += 1
            entered.set()
            await release.wait()
            return SimpleNamespace(ok=True, payload={"agent_ready": True})

    core = SlowHealth(probe_settled=True)
    executor = Recording(core, TaskStore(tmp_path / "tasks.sqlite"))
    checks = [asyncio.create_task(executor.reconciliation_ready()) for _ in range(3)]
    try:
        await entered.wait()
        release.set()
        assert await asyncio.gather(*checks) == [True, True, True]
        assert core.health_requests == 1
    finally:
        await executor.close()
        await asyncio.gather(*checks, return_exceptions=True)


async def test_core_cancel_of_detached_request_does_not_touch_session_round():
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    touched = []

    async def cancel_round(**_):
        touched.append("cancel_round")
        return True

    other = SimpleNamespace(request_id="other-round")
    adapter = SimpleNamespace(
        _resolve_interrupt_session_id=lambda s: s,
        _voice_agent_task_rail=SimpleNamespace(managed_tasks={"core-s": other}),
        _cancel_scheduler_running_tasks=lambda: touched.append("scheduler"),
        _instance=SimpleNamespace(cancel_round=cancel_round),
    )
    request = AgentRequest(
        request_id="cancel-gone", channel_id="video_tool", session_id="core-s",
        params={"intent": "cancel", "managed_task_request": "gone"},
    )
    response = await JiuWenSwarmDeepAdapter._process_interaction_interrupt(
        adapter, request, "cancel", None
    )
    assert touched == []
    assert response.ok and response.payload["managed_task_settled"] is True


async def test_failed_round_cancel_falls_back_to_cancelling_the_active_cua():
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.runtime.agent_adapter import managed_subagent_control as control
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    registered = asyncio.Event()

    async def cua():
        binding = control.bind_managed_task_session("core-s", "cua-request")
        try:
            control.register_cua_execution(parent_session_id="core-s", sub_session_id="sub")
            registered.set()
            await asyncio.sleep(60)
        finally:
            control.unbind_managed_task_session(binding)

    async def cancel_round(**_):
        raise RuntimeError("round owner unavailable")

    async def no_cutover(_):
        return None

    running = asyncio.create_task(cua())
    await registered.wait()
    adapter = SimpleNamespace(
        _resolve_interrupt_session_id=lambda s: s,
        _voice_agent_task_rail=SimpleNamespace(managed_tasks={}),
        _cancel_scheduler_running_tasks=lambda: None,
        _instance=SimpleNamespace(cancel_round=cancel_round),
        _get_goal_manager=lambda: None,
        _permission_dispatch=SimpleNamespace(start_cutover=no_cutover),
        _collect_cancelled_tools_for_session=lambda *_, **__: [],
    )
    request = AgentRequest(
        request_id="cancel-cua", channel_id="video_tool", session_id="core-s",
        params={"intent": "cancel", "managed_task_request": "cua-request"},
    )
    response = await JiuWenSwarmDeepAdapter._process_interaction_interrupt(
        adapter, request, "cancel", None
    )
    await asyncio.wait({running}, timeout=1)
    cancelled_by_interrupt = running.cancelled()
    running.cancel()
    assert cancelled_by_interrupt
    assert response.payload["cua_cancel_requested"] is True
