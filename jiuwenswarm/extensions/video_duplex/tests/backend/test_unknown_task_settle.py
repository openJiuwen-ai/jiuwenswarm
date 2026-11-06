"""#5486: an unknown task is settled by Core's confirmation, not held forever."""

import asyncio
from types import SimpleNamespace

from jiuwenswarm.extensions.video_duplex.backend.task_adapter import AgentTaskExecutor
from jiuwenswarm.extensions.video_duplex.backend.tasks import TaskService, TaskStore
from jiuwenswarm.extensions.video_duplex.tests.backend.test_managed_tasks import Executor, until

SESSION = "task-duplex:s"


class CoreSays:
    """Fake Core: replies to cancel/probe with a settled fact."""

    def __init__(self, *, probe_settled, cancel_settled=True):
        self.probe_settled, self.cancel_settled = probe_settled, cancel_settled
        self.requests = []

    async def send_request(self, request):
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
        assert service.get("alice", SESSION, active)["status"] == "unknown"
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


async def test_restart_keeps_unknown_when_core_still_runs_it(tmp_path):
    store, active, _ = await lost_task(tmp_path)
    executor = Recording(CoreSays(probe_settled=False), store)
    service = TaskService(store, executor)
    service.start()
    try:
        await asyncio.sleep(0.1)
        assert service.get("alice", SESSION, active)["status"] == "unknown"
        assert executor.calls == []
    finally:
        await service.close()


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
