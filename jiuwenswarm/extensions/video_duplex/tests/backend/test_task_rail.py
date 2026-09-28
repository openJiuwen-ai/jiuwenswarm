"""Voice rail ownership at the Host lifecycle and native callback boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.core.runner.callback.errors import AbortError

from jiuwenswarm.extensions.video_duplex.backend.tasks.rail import (
    VoiceAgentTaskRail,
)
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.extensions.video_duplex.tests.backend import test_task_bridge

routed_task_fixture = test_task_bridge.routed_task_fixture


class TaskRailHost(interface_deep.JiuWenSwarmDeepAdapter):
    """Exercise Host lifecycle with an injected Agent, without runtime setup."""

    @classmethod
    def for_agent(cls, owner):
        adapter = object.__new__(cls)
        adapter._instance = owner
        adapter._voice_agent_task_rail = None
        return adapter

    def replace_agent(self, owner):
        self._instance = owner


@pytest.mark.parametrize("managed", [False, True])
async def test_host_installs_task_rail_only_for_managed_session_before_start(
    monkeypatch, managed
):
    from jiuwenswarm.server.runtime.session.kv_cache import kv_cache_application_runtime

    registered = []
    owner = SimpleNamespace(
        ensure_initialized=AsyncMock(),
        register_rail=AsyncMock(side_effect=registered.append),
        unregister_rail=AsyncMock(),
    )
    adapter = TaskRailHost.for_agent(owner)
    adapter.install_session_input_guard = AsyncMock()
    session = SimpleNamespace(pre_run=AsyncMock())
    monkeypatch.setattr(interface_deep, "create_agent_session", lambda **_: session)
    monkeypatch.setattr(
        kv_cache_application_runtime, "get_kv_cache_runtime", lambda: None
    )

    async def start(**kwargs):
        assert kwargs["session"] is session
        assert len(registered) == int(managed)
        if managed:
            assert adapter.task_execution_binding == (owner, registered[0])

    owner.start = AsyncMock(side_effect=start)
    session_id = "managed-task-one" if managed else "ordinary-chat"
    await adapter.start_interaction(session_id)
    await adapter.start_interaction(session_id)
    assert owner.register_rail.await_count == int(managed)
    assert adapter.install_session_input_guard.await_count == 2

    if managed:
        rail = registered[0]
        binding = object()
        rail.managed_tasks[session_id] = binding
        # DeepAgent.configure drops registrations. Reinstall the same owned rail
        # without losing a live binding or registering it twice on ordinary starts.
        await adapter.install_voice_task_rail(reload=True)
        owner.unregister_rail.assert_awaited_once_with(rail)
        assert registered == [rail, rail]
        assert rail.managed_tasks[session_id] is binding

        replacement = SimpleNamespace(
            ensure_initialized=AsyncMock(), register_rail=AsyncMock()
        )
        adapter.replace_agent(replacement)
        await adapter.install_voice_task_rail()
        active_owner, active_rail = adapter.task_execution_binding
        assert active_owner is replacement
        assert active_rail.owner is replacement
        assert active_rail.managed_tasks == {}


async def test_callbacks_ignore_ordinary_and_child_requests_and_reject_closed_binding():
    root = object()
    binding = SimpleNamespace(
        request_id="current",
        root=root,
        check=AsyncMock(),
        capture_execution=Mock(),
        before_model=AsyncMock(),
        after_model=AsyncMock(),
    )
    rail = VoiceAgentTaskRail()
    rail.managed_tasks["managed-task-one"] = binding
    ctx = SimpleNamespace(agent=object(), extra={})
    for hook in (rail.before_model_call, rail.after_model_call, rail.before_tool_call):
        await hook(ctx)
    ctx.extra = {"run_context": {"extra": {"managed_task_request": "current"}}}
    ctx.agent = object()
    for hook in (rail.before_model_call, rail.after_model_call, rail.before_tool_call):
        await hook(ctx)
    binding.check.assert_not_called()
    binding.capture_execution.assert_not_called()
    binding.before_model.assert_not_awaited()
    binding.after_model.assert_not_called()

    ctx.agent = root
    await rail.before_model_call(ctx)
    await rail.after_model_call(ctx)
    await rail.before_tool_call(ctx)
    assert binding.check.call_count == 2
    binding.capture_execution.assert_called_once_with()
    binding.before_model.assert_awaited_once_with(ctx)
    binding.after_model.assert_called_once_with(ctx)

    rail.managed_tasks.clear()
    for hook in (rail.before_model_call, rail.after_model_call, rail.before_tool_call):
        with pytest.raises(AbortError):
            await hook(ctx)
    assert binding.check.call_count == 2


async def test_active_root_without_execution_marker_cannot_bypass_checkpoint(routed_task):
    from jiuwenswarm.extensions.video_duplex.backend.tasks.bridge import RemoteTaskEndpoint
    from jiuwenswarm.extensions.video_duplex.backend.tasks.execution import (
        TaskExecutionBinding,
    )

    r = routed_task
    root = object()
    endpoint = RemoteTaskEndpoint(r.request)
    endpoint.call = AsyncMock(wraps=endpoint.call)
    before = r.store.read(r.task["id"])
    binding = TaskExecutionBinding(endpoint, r.task, root)
    rail = VoiceAgentTaskRail()
    rail.managed_tasks["managed-task-one"] = binding
    ctx = SimpleNamespace(agent=root, extra={})
    for hook in (rail.before_model_call, rail.after_model_call, rail.before_tool_call):
        with pytest.raises(AbortError):
            await hook(ctx)
    endpoint.call.assert_not_awaited()
    assert r.store.read(r.task["id"]) == before
