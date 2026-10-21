"""Mode dispatch and native Design round ownership without cloud model calls."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.extensions.video_duplex.backend.mode_routing import execution_target, resolve_execution_target
from jiuwenswarm.extensions.video_duplex.backend.task_adapter import AgentTaskExecutor
from jiuwenswarm.extensions.video_duplex.backend.tasks import TaskService, TaskStore
from jiuwenswarm.extensions.video_duplex.backend.tasks import design_execution
from jiuwenswarm.extensions.video_duplex.tests.backend.task_bridge_support import install_bridge


@pytest.mark.parametrize("metadata,mode,work", [
    ({}, "agent.work.normal", "work"),
    ({"mode": "agent", "work_mode": "code"}, "agent.code.normal", "code"),
    ({"mode": "code.plan", "work_mode": "code"}, "agent.code.plan", "code"),
    ({"mode": "agent.code.plan", "work_mode": "work"}, "agent.code.plan", "code"),
    ({"work_mode": "design"}, "team.design", "design"),
    ({"mode": "team.design"}, "team.design", "design"),
])
def test_target_preserves_profile_and_plan(metadata, mode, work):
    target = execution_target({**metadata, "project_id": "p", "project_dir": "/project", "api_key": "private"})
    assert target == {"mode": mode, "work_mode": work, "project_id": "p", "project_dir": "/project"}


def test_ordinary_team_is_not_silently_converted_to_work():
    with pytest.raises(ValueError, match="single-agent"):
        execution_target({"mode": "team"})


async def test_target_requires_the_actual_session_owner():
    client = SimpleNamespace(send_request=AsyncMock(return_value=SimpleNamespace(ok=True, payload={
        "user_id": "bob", "mode": "code.normal", "project_dir": "/other",
    })))
    with pytest.raises(ValueError, match="unavailable"):
        await resolve_execution_target(client, "alice", "task-duplex:chat")


async def test_admission_uses_server_route_even_if_scheduling_supplies_another_target():
    from jiuwenswarm.extensions.video_duplex.backend.task_adapter import VideoSearchManager

    metadata = {"user_id": "alice", "mode": "agent", "work_mode": "code", "project_dir": "/code"}
    client = SimpleNamespace(send_request=AsyncMock(return_value=SimpleNamespace(ok=True, payload=metadata)))
    manager = VideoSearchManager(None, client, log_event=lambda *_: None, qwen_active=lambda: True,
                                 authorize=lambda ws, scope: ("alice", scope))
    submitted = []
    def submit(owner, scope, command, text, request):
        submitted.append(request)
        return TaskService._new(owner, scope, text, request)
    manager._service = SimpleNamespace(submit=submit)
    await manager.start(object(), question="Fix bug", query="Fix bug", search_session_id="task-duplex:chat",
                        scheduling={"execution_target": {"mode": "agent", "project_dir": "/other"}})
    assert submitted[0]["execution_target"]["mode"] == "agent.code.normal"
    assert submitted[0]["execution_target"]["project_dir"] == "/code"


@pytest.mark.parametrize("mode,work", [("agent.code.plan", "code"), ("team.design", "design")])
async def test_native_cancel_routes_to_the_bound_mode_and_project(tmp_path, mode, work):
    store = TaskStore(tmp_path / "cancel.sqlite")
    task = TaskService._new("alice", "task-duplex:chat", "Task", {
        "execution_target": {"mode": mode, "work_mode": work, "project_dir": "/original"},
    })
    with store.transaction() as db:
        store.put(db, task)
    requests = []
    async def send(env):
        requests.append(e2a_to_agent_request(env))
        store.update(task["id"], lambda t: t.update(execution_settled=True))
        return SimpleNamespace(ok=True, payload={"success": True})
    executor = AgentTaskExecutor(SimpleNamespace(send_request=send), store, None)
    await executor.cancel(task)
    request = requests[0]
    assert request.req_method == ReqMethod.CHAT_CANCEL
    assert request.params["mode"] == mode and request.params["project_dir"] == "/original"
    assert request.params["managed_task_request"] == task["request_id"]
    assert request.session_id == "chat"
    await executor.close()


@pytest.mark.parametrize("mode,work", [("agent.code.normal", "code"), ("agent.code.plan", "code"), ("team.design", "design")])
async def test_executor_routes_same_session_and_project_and_rejects_changed_binding(tmp_path, mode, work):
    requests = []
    metadata = {"user_id": "alice", "mode": mode, "work_mode": work, "project_id": "p", "project_dir": "/project"}

    async def send(env):
        request = e2a_to_agent_request(env)
        requests.append(request)
        return SimpleNamespace(ok=True, payload=metadata if request.req_method == ReqMethod.SESSION_GET_METADATA
                               else {"content": "The requested assistant turn has finished."})

    executor = AgentTaskExecutor(SimpleNamespace(send_request=send), TaskStore(tmp_path / "tasks.sqlite"), None)
    target = execution_target(metadata)
    task = TaskService._new("alice", "task-duplex:chat", "Update this project", {"execution_target": target})
    result = await executor._run(task, AsyncMock(), [])
    request = requests[-1]
    assert result["answer"]
    assert request.params["mode"] == mode
    assert request.params["project_dir"] == "/project"
    assert request.session_id == "chat"
    assert request.params["managed_task_binding"]["task_id"] == task["id"]
    if work == "design":
        assert "does not mean canvas rendering has completed" in request.params["query"]
    metadata["project_dir"] = "/other"
    count = len(requests)
    with pytest.raises(ValueError, match="changed"):
        await executor._run(task, AsyncMock(), [])
    assert len(requests) == count + 1, "metadata check must fail before execution"
    await executor.close()


@pytest.mark.parametrize("mode,work", [("agent.code.normal", "code"), ("team.design", "design")])
def test_interaction_answer_keeps_mode_and_project(mode, work):
    task = TaskService._new("alice", "task-duplex:chat", "Task", {
        "execution_target": {"mode": mode, "work_mode": work, "project_dir": "/project"},
    })
    task["interaction"] = {"operation_id": "op", "request_id": "question", "source": "ask_user_interrupt",
                           "answers": [{"question": "Which scene?", "answer": "Scene two"}]}
    request = AgentTaskExecutor.answer_input(task).to_agent_request()
    assert request.params["mode"] == mode
    assert request.params["project_dir"] == "/project"
    assert request.req_method == ReqMethod.CHAT_SEND


@pytest.fixture
def design_host(monkeypatch, tmp_path):
    import jiuwenswarm.agents.harness.team as team_module

    monkeypatch.setattr(team_module, "get_team_manager", lambda *_: SimpleNamespace(is_round_active=lambda _: False))
    design_execution._rounds.clear()
    install_bridge(monkeypatch)
    store = TaskStore(tmp_path / "tasks.sqlite")
    from jiuwenswarm.extensions.video_duplex.backend.tasks.bridge import GatewayTaskEndpoint

    endpoint = GatewayTaskEndpoint(store)
    task = TaskService._new("alice", "task-duplex:design-chat", "Update scene", {})
    task.update(status="running", checkpoint_open=True)
    with store.transaction() as db:
        store.put(db, task)
    request = SimpleNamespace(user_id="alice", session_id="design-chat", request_id=task["request_id"],
                              channel_id="video_tool", params={"mode": "team.design", "managed_task_binding": endpoint.binding(task)})
    yield store, task, request
    endpoint.close()
    design_execution._rounds.clear()


async def test_design_round_reports_native_binding_and_settlement(design_host):
    store, task, request = design_host
    async with design_execution.bind_design_voice_task(request):
        assert store.read(task["id"])["execution_bound"]
        assert not store.read(task["id"])["execution_settled"]
        with pytest.raises(RuntimeError, match="owns"):
            async with design_execution.bind_design_voice_task(SimpleNamespace(
                user_id="alice", session_id="design-chat", params={"mode": "team.design"}, channel_id="web",
            )):
                pass
    saved = store.read(task["id"])
    assert saved["execution_settled"] and not saved["execution_cancelled"]


async def test_design_cancel_waits_for_native_stop_and_rejects_stale_request(design_host):
    store, task, request = design_host
    async with design_execution.bind_design_voice_task(request):
        store.update(task["id"], lambda t: t.update(status="cancelling"))
    assert not store.read(task["id"])["execution_settled"], "closing the reply is not a stop receipt"
    control = SimpleNamespace(user_id="alice", session_id="design-chat", params={
        "mode": "team.design", "managed_task_request": "old-request",
    })
    with pytest.raises(ValueError, match="STALE"):
        design_execution.control_target(control)
    control.params["managed_task_request"] = task["request_id"]
    await design_execution.control_target(control).settle(cancelled=True)
    assert store.read(task["id"])["execution_cancelled"]
    assert store.read(task["id"])["execution_settled"]


async def test_detached_design_round_does_not_invent_settlement(design_host):
    store, task, request = design_host
    with pytest.raises(ConnectionError):
        async with design_execution.bind_design_voice_task(request):
            raise ConnectionError("transport lost")
    assert not store.read(task["id"])["execution_settled"]


async def test_real_team_stream_entry_uses_design_binding(monkeypatch, design_host):
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers

    store, task, request = design_host
    async def native_stream(*_):
        assert store.read(task["id"])["execution_bound"]
        yield "native-design-output"
    monkeypatch.setattr(team_helpers, "_process_team_message_stream_with_design", native_stream)
    assert [chunk async for chunk in team_helpers.process_team_message_stream(request, {}, None)] == ["native-design-output"]
    assert store.read(task["id"])["execution_settled"]


async def test_design_through_real_adapter_wrapper_binds_only_once(monkeypatch, design_host):
    from contextlib import asynccontextmanager, nullcontext
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    store, task, request = design_host

    @asynccontextmanager
    async def admission(*_):
        yield

    async def native_stream(*_):
        assert store.read(task["id"])["execution_bound"]
        yield "native-design-output"

    monkeypatch.setattr(team_helpers, "_process_team_message_stream_with_design", native_stream)
    adapter = SimpleNamespace(
        _permission_request_admission=admission,
        validate_auto_permission_workspace_request=lambda _: None,
        _bind_permission_request_context=lambda _: nullcontext(),
        _process_message_stream_impl=lambda req, inputs: team_helpers.process_team_message_stream(req, inputs, None),
        _is_session_scoped_adapter=True,
        task_execution_binding=(SimpleNamespace(_react_agent=object(), active_round=None), SimpleNamespace(managed_tasks={})),
    )
    chunks = [chunk async for chunk in JiuWenSwarmDeepAdapter.process_message_stream_impl(adapter, request, {})]
    assert chunks == ["native-design-output"]
    saved = store.read(task["id"])
    assert saved["execution_settled"] and not saved["execution_cancelled"]


async def test_real_team_cancel_entry_requires_exact_round_and_native_ack(monkeypatch, design_host):
    import jiuwenswarm.agents.harness.team as team_module
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

    store, task, request = design_host
    manager = SimpleNamespace(is_round_active=lambda _: False, cancel_session_runtime=AsyncMock(return_value=True))
    monkeypatch.setattr(team_module, "get_team_manager", lambda *_: manager)
    host = SimpleNamespace(_session_manager=SimpleNamespace(cancel_session_task=AsyncMock()),
                           _build_interrupt_result_response=lambda req, **result: result)
    async with design_execution.bind_design_voice_task(request):
        store.update(task["id"], lambda t: t.update(status="cancelling"))
    control = SimpleNamespace(user_id="alice", session_id="design-chat", channel_id="video_tool", metadata={}, params={
        "mode": "team.design", "managed_task_request": "wrong",
    })
    with pytest.raises(ValueError, match="STALE"):
        await JiuWenSwarm._process_team_interrupt(host, request=control, intent="cancel", session_id="design-chat")
    manager.cancel_session_runtime.assert_not_awaited()
    control.params["managed_task_request"] = task["request_id"]
    result = await JiuWenSwarm._process_team_interrupt(host, request=control, intent="cancel", session_id="design-chat")
    assert result["success"] is True
    assert store.read(task["id"])["execution_cancelled"]
    assert store.read(task["id"])["execution_settled"]


async def test_design_information_answer_reuses_team_input_and_preserves_question(monkeypatch, design_host):
    import jiuwenswarm.agents.harness.team as team_module

    _, task, request = design_host
    manager = SimpleNamespace(is_round_active=lambda _: False, interact=AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(team_module, "get_team_manager", lambda *_: manager)
    native_answer = object()
    async with design_execution.bind_design_voice_task(request):
        control = SimpleNamespace(user_id="alice", session_id="design-chat", request_id="answer", channel_id="video_tool", params={
            "mode": "team.design", "managed_task_request": task["request_id"], "answers": [{"answer": "Scene two"}],
        })
        result = await design_execution.answer_design_task(control, lambda _: ({"query": native_answer}, None, None))
        assert result.ok
        manager.interact.assert_awaited_once_with("design-chat", native_answer)


async def test_design_voice_context_is_bounded_background_and_does_not_replace_current_request(monkeypatch, design_host):
    from jiuwenswarm.server.runtime.agent_adapter.user_turn import UserTurn, TEAM_USER_TURN_KEY
    from jiuwenswarm.server.runtime.session import session_history

    _, _, request = design_host
    monkeypatch.setattr(session_history, "flush_pending_writes", lambda **_: True)
    monkeypatch.setattr(session_history, "load_history_records", lambda _: [
        {"id": str(i), "channel_id": "video_duplex", "role": "user", "content": "Cedar " + "x" * 2000}
        for i in range(12)
    ])
    turn = UserTurn(text="Change scene two", channel="video_tool", language="en", files={}, skills=["design"])
    inputs = {TEAM_USER_TURN_KEY: turn}
    await design_execution.add_voice_context(request, inputs)
    restored = inputs[TEAM_USER_TURN_KEY]
    assert restored.text.endswith("Current request:\nChange scene two")
    assert "not a new user request" in restored.text
    assert restored.text.count("Cedar") == 6
    assert len(restored.text) < 8000
    assert restored.skills == ["design"]
    request.params["answers"] = [{"answer": "yes"}]
    await design_execution.add_voice_context(request, inputs)
    assert inputs[TEAM_USER_TURN_KEY] is restored


async def test_design_history_prepare_failure_is_settled_before_any_native_work(monkeypatch, design_host):
    store, task, request = design_host
    monkeypatch.setattr(design_execution, "add_voice_context", AsyncMock(side_effect=RuntimeError("History still saving")))
    with pytest.raises(RuntimeError, match="History"):
        async with design_execution.bind_design_voice_task(request, {}):
            pytest.fail("native work must not start")
    assert store.read(task["id"])["execution_settled"]


async def test_typed_design_turn_also_receives_saved_voice_context(monkeypatch, design_host):
    _, _, request = design_host
    request.channel_id = "web"
    request.params.pop("managed_task_binding")
    restore = AsyncMock()
    monkeypatch.setattr(design_execution, "add_voice_context", restore)
    inputs = {"query": "Use the color I mentioned"}
    async with design_execution.bind_design_voice_task(request, inputs):
        restore.assert_awaited_once_with(request, inputs)
    assert not design_execution._rounds
