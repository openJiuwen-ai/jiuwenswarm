# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Delegation deadlines and failures through the real SDK TaskTool dispatcher."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.session.agent import Session
from openjiuwen.harness.tools.subagent.task_tool import TaskTool

from jiuwenswarm.server.runtime.debug_trace import context, task_tool_patch


@pytest.fixture(autouse=True)
def dispatch_hook(monkeypatch):
    from openjiuwen.extensions.observability import setup

    monkeypatch.setattr(setup, "is_initialized", lambda: False)
    monkeypatch.setattr(TaskTool, "_invoke_subagent", TaskTool._invoke_subagent)
    for name in ("debug_trace_patch_applied", "debug_trace_patch_mode"):
        monkeypatch.setattr(TaskTool, name, getattr(TaskTool, name, False), raising=False)
    monkeypatch.setattr(task_tool_patch, "_PATCH_APPLIED", task_tool_patch._PATCH_APPLIED)
    task_tool_patch.apply_task_tool_debug_patch()


@pytest.fixture(params=[False, True], ids=["invoke", "debug-stream"])
def debug_capture(request):
    logger = Mock() if request.param else None
    if logger is not None:
        logger.captures_subagent_flow.return_value = True
    token = context.set_debug_trace_logger(logger)
    try:
        yield logger
    finally:
        context.reset_debug_trace_logger(token)


def child_for(run, *, timeout=1.0, task_loop=False):
    async def stream(*args, **kwargs):
        yield {"type": "answer", "payload": await run(*args, **kwargs)}

    return SimpleNamespace(
        card=SimpleNamespace(id="child", name="general-purpose"),
        deep_config=SimpleNamespace(completion_timeout=timeout, enable_task_loop=task_loop),
        invoke=run, stream=stream,
    )


async def dispatch(child, *, session_id="parent-A", call_id="call-1"):
    parent = SimpleNamespace(create_subagent=lambda *args, **kwargs: child)
    tool = TaskTool(ToolCard(id="test-task", name="task_tool", description="test"), parent)
    return await tool.invoke(
        {"subagent_type": "general-purpose", "task_description": "test"},
        session=Session(session_id=session_id), tool_call_id=call_id,
    )


@pytest.mark.parametrize("task_loop", [False, True])
async def test_deadline_returns_failure_and_cancels_only_this_call(debug_capture, task_loop):
    stopped = asyncio.Event()

    async def run(*args, **kwargs):
        try:
            await asyncio.sleep(0.15)
            return {"output": "late answer"}
        finally:
            stopped.set()

    child = child_for(run, timeout=0.02, task_loop=task_loop)
    with pytest.raises(BaseError, match="completion_timeout"):
        await asyncio.wait_for(dispatch(child), 2)
    assert stopped.is_set()
    with pytest.raises(BaseError, match="unavailable"):
        await dispatch(child)


@pytest.mark.parametrize("result, reason", [
    ({"error": "completion_timeout"}, "completion_timeout"),
    ({"result_type": "error", "message": "provider unavailable"}, "provider unavailable"),
    ({"result_type": "error", "output": "private output"}, "subagent_error"),
])
async def test_error_result_never_becomes_empty_success(debug_capture, result, reason):
    run = AsyncMock(return_value=result)
    child = child_for(run)
    with pytest.raises(BaseError, match=reason) as error:
        await dispatch(child)
    assert "private output" not in str(error.value)
    with pytest.raises(BaseError, match="unavailable"):
        await dispatch(child)
    assert run.await_count == 1


@pytest.mark.parametrize("output", ["", "done"])
async def test_success_including_empty_output_is_preserved(debug_capture, output):
    child = child_for(AsyncMock(return_value={"output": output}))
    result = await dispatch(child)
    assert result.success is True
    assert result.data["output"] == output


@pytest.mark.parametrize("failure", [RuntimeError, TimeoutError, asyncio.CancelledError])
@pytest.mark.parametrize("task_loop", [False, True])
async def test_child_exception_preserves_cause_and_prevents_reuse(debug_capture, failure, task_loop):
    run = AsyncMock(side_effect=failure("inner failure"))
    child = child_for(run, task_loop=task_loop)
    expected = asyncio.CancelledError if failure is asyncio.CancelledError else BaseError
    with pytest.raises(expected, match="inner failure"):
        await dispatch(child)
    with pytest.raises(BaseError, match="unavailable"):
        await dispatch(child)
    assert run.await_count == 1


async def test_debug_error_chunk_is_not_reduced_to_empty_success(debug_capture):
    if debug_capture is None:
        return

    async def stream(*args, **kwargs):
        yield {"type": "error", "payload": {"error": "provider unavailable"}}

    child = child_for(AsyncMock())
    child.stream = stream
    with pytest.raises(BaseError, match="provider unavailable"):
        await dispatch(child)


async def test_parent_cancellation_stops_child_and_prevents_reuse(debug_capture):
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def run(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    child = child_for(run, timeout=2)
    task = asyncio.create_task(dispatch(child))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    with pytest.raises(BaseError, match="unavailable"):
        await dispatch(child)


@pytest.mark.parametrize("other_session", ["parent-A", "parent-B"])
async def test_one_timeout_does_not_cancel_a_parallel_child(debug_capture, other_session):
    async def slow(*args, **kwargs):
        await asyncio.sleep(0.1)
        return {"output": "done"}

    results = await asyncio.gather(
        dispatch(child_for(slow, timeout=0.01)),
        dispatch(child_for(slow, timeout=1), session_id=other_session, call_id="call-2"),
        return_exceptions=True,
    )
    assert isinstance(results[0], BaseError)
    assert "completion_timeout" in str(results[0])
    assert results[1].success is True
    assert results[1].data["output"] == "done"


async def test_active_call_and_session_binding_prevent_unsafe_reuse(debug_capture):
    entered, release = asyncio.Event(), asyncio.Event()

    async def run(*args, **kwargs):
        entered.set()
        await release.wait()
        return {"output": "done"}

    child = child_for(run, timeout=1)
    running = asyncio.create_task(dispatch(child))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(BaseError, match="active invocation"):
            await dispatch(child)
        with pytest.raises(BaseError, match="across sessions"):
            await dispatch(child, session_id="parent-B")
    finally:
        release.set()
        assert (await running).success is True


async def test_interrupt_can_resume_with_a_fresh_deadline(debug_capture):
    interrupt = {"result_type": "interrupt", "interrupt_ids": ["approval-1"]}
    run = AsyncMock(side_effect=[interrupt, {"output": "resumed"}])
    child = child_for(run)
    assert await dispatch(child) == interrupt
    await asyncio.sleep(0.03)  # Human approval waiting does not consume the next budget.
    assert (await dispatch(child)).data["output"] == "resumed"


async def test_pending_cleanup_rejects_new_calls_and_discards_late_output(monkeypatch, debug_capture):
    from jiuwenswarm.agents.harness.common.tools.subagent_executor import execution_timeout as guard

    pending = set()
    monkeypatch.setattr(guard, "_PENDING_CLEANUP", pending)
    monkeypatch.setattr(guard, "_MAX_PENDING_CLEANUP", 1)
    monkeypatch.setattr(guard, "_CANCEL_GRACE_SECONDS", 0.01)
    release = asyncio.Event()

    async def stubborn(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            return {"output": "late answer"}

    child = child_for(stubborn, timeout=0.01)
    fresh_run = AsyncMock(return_value={"output": "ok"})
    fresh_child = child_for(fresh_run)
    try:
        with pytest.raises(BaseError, match="coroutine_cleanup=pending"):
            await asyncio.wait_for(dispatch(child), 1)
        assert pending
        with pytest.raises(BaseError, match="subagent_cleanup_backlog"):
            await dispatch(fresh_child, call_id="call-2")
        fresh_run.assert_not_called()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*list(pending), return_exceptions=True), 1)
        await asyncio.sleep(0)
    assert not pending
    if debug_capture is not None:
        debug_capture.feed_subagent.assert_not_called()
    assert (await dispatch(fresh_child, call_id="call-2")).success is True


async def test_runtime_stop_is_requested_even_when_abort_waits(monkeypatch, debug_capture):
    from jiuwenswarm.agents.harness.common.tools.subagent_executor import execution_timeout as guard

    pending = set()
    monkeypatch.setattr(guard, "_PENDING_CLEANUP", pending)
    monkeypatch.setattr(guard, "_CANCEL_GRACE_SECONDS", 0.01)
    release = asyncio.Event()
    controller = SimpleNamespace(stop=AsyncMock())
    child = child_for(AsyncMock(return_value={"error": "completion_timeout"}), task_loop=True)
    child.abort = release.wait
    child.loop_controller = controller
    try:
        with pytest.raises(BaseError, match="completion_timeout"):
            await asyncio.wait_for(dispatch(child), 1)
        controller.stop.assert_awaited_once()
        assert pending
    finally:
        release.set()
        await asyncio.gather(*list(pending))


async def test_debug_stream_closes_context_in_its_original_task():
    value = ContextVar("child-value", default="parent")
    stopped = asyncio.Event()

    async def stream(*args, **kwargs):
        token = value.set("child")
        try:
            yield {"type": "tool_result", "payload": {"error": "recoverable"}}
            await asyncio.Event().wait()
        finally:
            value.reset(token)
            stopped.set()

    logger = Mock()
    logger.captures_subagent_flow.return_value = True
    token = context.set_debug_trace_logger(logger)
    child = child_for(AsyncMock(), timeout=0.02)
    child.stream = stream
    try:
        with pytest.raises(BaseError, match="completion_timeout"):
            await dispatch(child)
    finally:
        context.reset_debug_trace_logger(token)
    assert stopped.is_set()
    assert value.get() == "parent"
    logger.end_subagent.assert_called_once()


async def test_direct_custom_subagent_entrypoint_has_same_guard(debug_capture):
    from jiuwenswarm.server.runtime.debug_trace import invoke_subagent_with_trace

    child = child_for(AsyncMock(return_value={"error": "provider unavailable"}))
    with pytest.raises(RuntimeError, match="provider unavailable"):
        await invoke_subagent_with_trace(
            child, inputs={"query": "test", "conversation_id": "custom-child"},
            session=None, source_label="subagent:custom:test",
        )


def _build_parent(profile, tmp_path, monkeypatch):
    """Use real SDK factories and team providers; model I/O is replaced later."""
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard
    from openjiuwen.harness import create_deep_agent
    from openjiuwen.harness.schema.config import SubAgentConfig
    from openjiuwen.harness.workspace.workspace import Workspace

    kind = "explore_agent" if profile.startswith("code") else "general-purpose"
    if ":" not in profile:
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
        from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter
        from jiuwenswarm.server.runtime.agent_adapter.interface_flash import JiuwenSwarmFlashAdapter

        cls = {"agent": JiuWenSwarmDeepAdapter, "code": JiuwenSwarmCodeAdapter,
               "flash": JiuwenSwarmFlashAdapter}[profile]
        adapter = cls()
        rail = adapter._build_subagent_rail({})
        return create_deep_agent(
            model=None, rails=[rail],
            subagents=[SubAgentConfig(
                agent_card=AgentCard(name=kind), system_prompt="test", enable_task_loop=False,
            )],
            workspace=Workspace(root_path=str(tmp_path)), auto_create_workspace=False,
            enable_sys_operation=False, enable_read_image_multimodal=False,
        ), kind

    from openjiuwen.agent_teams.harness.team_harness import TeamHarness
    from openjiuwen.agent_teams.schema.blueprint import LeaderSpec, TeamAgentSpec
    from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec, SubAgentSpec, WorkspaceSpec
    from openjiuwen.agent_teams.schema.team import TeamRole
    from jiuwenswarm.agents.swarm import enrich_team_spec_for_swarm

    mode, role = profile.split(":")
    monkeypatch.chdir(tmp_path)
    base = DeepAgentSpec(
        card=AgentCard(id=f"parent-{mode}-{role}", name=role),
        workspace=WorkspaceSpec(root_path=str(tmp_path / role)),
        enable_sys_operation=False, enable_read_image_multimodal=False,
        auto_create_workspace=False,
        subagents=[SubAgentSpec(
            agent_card=AgentCard(name=kind), system_prompt="test", enable_task_loop=False,
        )],
    )
    spec = TeamAgentSpec(
        agents={role: base}, team_name=f"timeout-{mode}-{role}",
        leader=LeaderSpec(member_name="leader"),
        workspace={"root_path": str(tmp_path / "team")},
    )
    enrich_team_spec_for_swarm(
        spec, session_id="team-parent", mode=mode,
        config_base={"react": {"subagents": {"general_agent": {"enabled": True}}},
                     "symphony": {"skill_retrieval": {"enabled": False}}},
    )
    harness = TeamHarness.build(
        agent_spec=spec.agents[role], role=TeamRole(role), member_name=role,
        build_context=spec.build_context.derive(role=role, member_name=role),
    )
    return harness._native, kind


@pytest.mark.parametrize("profile", [
    "agent", "code", "flash", "team:leader", "team:teammate",
    "team.plan:leader", "team.plan:teammate", "code.team:leader", "code.team:teammate",
])
@pytest.mark.parametrize("behavior", ["success", "deadline", "error"])
async def test_real_child_factory_dispatch_across_modes(
    profile, behavior, debug_capture, tmp_path, monkeypatch,
):
    parent, kind = _build_parent(profile, tmp_path, monkeypatch)
    stopped = asyncio.Event()
    create = parent.create_subagent

    async def react_invoke(*args, **kwargs):
        try:
            if behavior == "deadline":
                await asyncio.Event().wait()
            if behavior == "error":
                return {"result_type": "error", "error": "provider unavailable"}
            return {"output": "child done"}
        finally:
            stopped.set()

    async def react_stream(*args, **kwargs):
        yield {"type": "answer", "payload": await react_invoke()}

    def create_controlled(*args, **kwargs):
        child = create(*args, **kwargs)
        assert child.deep_config.enable_task_loop is False
        child.deep_config.completion_timeout = 0.1 if behavior == "deadline" else 5
        child._initialized = True
        child._react_agent = SimpleNamespace(invoke=react_invoke, stream=react_stream)
        return child

    monkeypatch.setattr(parent, "create_subagent", create_controlled)
    tool = TaskTool(ToolCard(name="task_tool", description="test"), parent)

    async def invoke():
        return await tool.invoke(
            {"subagent_type": kind, "task_description": "test"},
            session=Session(session_id="mode-test"), tool_call_id="mode-call",
        )

    if behavior == "success":
        assert (await invoke()).data["output"] == "child done"
    else:
        reason = "completion_timeout" if behavior == "deadline" else "provider unavailable"
        with pytest.raises(BaseError, match=reason):
            await asyncio.wait_for(invoke(), 2)
    assert stopped.is_set()


async def test_real_task_loop_scheduler_stops_after_delegation_timeout():
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard
    from openjiuwen.harness.deep_agent import DeepAgent
    from openjiuwen.harness.schema.config import DeepAgentConfig
    from jiuwenswarm.agents.harness.common.tools.subagent_executor.execution_timeout import (
        run_subagent_with_timeout,
    )

    entered, stopped = asyncio.Event(), asyncio.Event()

    async def react_invoke(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    child = DeepAgent(AgentCard(id="task-loop-child", name="child"))
    child.configure(DeepAgentConfig(model=None, enable_task_loop=True, completion_timeout=0.2))
    child._initialized = True
    child._react_agent = SimpleNamespace(invoke=react_invoke)
    child_session = Session(session_id="isolated-task-loop-child")
    await Runner.start()
    try:
        with pytest.raises(RuntimeError, match="completion_timeout"):
            await asyncio.wait_for(run_subagent_with_timeout(
                child, session_id=child_session.get_session_id(),
                call=lambda: child.invoke({"query": "test"}, session=child_session),
            ), 3)
        assert entered.is_set()
        await asyncio.wait_for(stopped.wait(), 1)
        assert child.loop_controller is None
    finally:
        if child.loop_controller is not None:
            await child.loop_controller.stop()
        await Runner.stop()
