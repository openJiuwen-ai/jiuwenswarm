# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""OfficeAce timeout/error semantics at the unmodified SDK TaskTool boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.session.agent import Session
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.schema.config import DeepAgentConfig
from openjiuwen.harness.tools.subagent.task_tool import TaskTool

from jiuwenswarm.agents.harness.common.tools.subagent_executor import execution_timeout


def _tool(invoke: Any, *, timeout: float = 0.03, task_loop: bool = False, wire: bool = True):
    parent = DeepAgent(AgentCard(id="timeout-parent", name="parent", description="test"))
    parent.configure(DeepAgentConfig(model=None, tools=[], mcps=[], skills=[]))
    child = SimpleNamespace(
        card=AgentCard(id="timeout-child", name="child", description="test"),
        deep_config=SimpleNamespace(
            completion_timeout=timeout, enable_task_loop=task_loop, model=None,
        ),
        invoke=invoke,
    )
    parent.create_subagent = lambda *args, **kwargs: child
    if wire:
        execution_timeout.install_subagent_timeout_wiring(parent)
    tool = TaskTool(ToolCard(id="timeout-task", name="task_tool", description="test"), parent)
    return tool, child, parent


async def _invoke(tool: TaskTool, *, parent: str = "parent-A", call: str = "call-A"):
    return await tool.invoke(
        {"subagent_type": "general-purpose", "task_description": "controlled test"},
        session=Session(session_id=parent), tool_call_id=call,
    )


async def test_single_round_deadline_cancels_execution_and_is_not_empty_success():
    """Without the guard, SDK TaskTool waits for the long child and returns success."""
    stopped = asyncio.Event()

    async def invoke(_inputs):
        try:
            await asyncio.sleep(0.15)
            return {"output": "should not finish"}
        finally:
            stopped.set()

    tool, _, _ = _tool(invoke)
    with pytest.raises(BaseError, match="completion_timeout"):
        await _invoke(tool)
    assert stopped.is_set()


async def test_adapter_installs_guard_even_when_skill_authorization_is_disabled():
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    async def invoke(_inputs):
        return {"error": "completion_timeout"}

    tool, _, parent = _tool(invoke, wire=False)
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = parent
    adapter._resolve_skill_authorization_base_config = lambda: {
        "enabled": False, "skill_authorization": {"enabled": False},
    }
    adapter._bind_subagent_authorization_wiring()
    adapter._bind_subagent_timeout_wiring()
    with pytest.raises(BaseError, match="completion_timeout"):
        await _invoke(tool)


async def test_explicit_child_timeout_is_not_packaged_as_success():
    """SDK currently loses this error dictionary when reading only output."""
    async def invoke(_inputs):
        return {"error": "completion_timeout"}

    tool, _, _ = _tool(invoke)
    with pytest.raises(BaseError, match="completion_timeout"):
        await _invoke(tool)


@pytest.mark.parametrize("output", ["", "done"])
async def test_normal_output_including_empty_remains_successful(output):
    async def invoke(_inputs):
        return {"output": output}

    tool, _, parent = _tool(invoke)
    execution_timeout.install_subagent_timeout_wiring(parent)
    result = await _invoke(tool)
    assert result.success is True
    assert result.data["output"] == output


@pytest.mark.parametrize("result", [
    {"error": "provider_unavailable"},
    {"result_type": "error", "message": "provider_unavailable"},
])
async def test_explicit_failure_keeps_original_reason(result):
    async def invoke(_inputs):
        return result

    tool, _, _ = _tool(invoke)
    with pytest.raises(BaseError, match="provider_unavailable"):
        await _invoke(tool)


@pytest.mark.parametrize("task_loop", [False, True])
@pytest.mark.parametrize("with_output", [False, True])
async def test_error_without_reason_does_not_include_output(task_loop, with_output, caplog):
    private_output = "private-output-marker:" + "x" * 10000
    result = {"result_type": "error"}
    if with_output:
        result["output"] = private_output

    async def invoke(_inputs):
        return result

    tool, child, _ = _tool(invoke, task_loop=task_loop)
    with pytest.raises(BaseError, match="subagent_failed: subagent_error") as caught:
        await _invoke(tool)
    assert "private-output-marker" not in str(caught.value)
    assert "private-output-marker" not in caplog.text
    assert str(caught.value.__cause__) == "subagent_failed: subagent_error"
    with pytest.raises(RuntimeError, match="unavailable"):
        await child.invoke({"query": "next"})


@pytest.mark.parametrize("task_loop", [False, True])
@pytest.mark.parametrize("error_type", [RuntimeError, ValueError, ConnectionError, TimeoutError])
async def test_child_exception_preserves_reason_and_prevents_reuse(task_loop, error_type):
    calls = 0
    error = error_type("original-child-failure")

    async def invoke(_inputs):
        nonlocal calls
        calls += 1
        raise error

    tool, child, _ = _tool(invoke, task_loop=task_loop)
    with pytest.raises(BaseError, match="original-child-failure") as caught:
        await _invoke(tool)
    assert caught.value.__cause__ is error
    assert "completion_timeout" not in str(caught.value)
    with pytest.raises(RuntimeError, match="unavailable"):
        await child.invoke({"query": "next"})
    assert calls == 1


async def test_internal_timeout_is_not_relabelled_as_completion_timeout():
    async def invoke(_inputs):
        raise TimeoutError("tool_connection_timeout")

    tool, _, _ = _tool(invoke)
    with pytest.raises(BaseError, match="tool_connection_timeout") as caught:
        await _invoke(tool)
    assert "completion_timeout" not in str(caught.value)


async def test_user_cancellation_propagates_and_cancels_child():
    started, stopped = asyncio.Event(), asyncio.Event()

    async def invoke(_inputs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    tool, _, _ = _tool(invoke, timeout=5)
    running = asyncio.create_task(_invoke(tool))
    await asyncio.wait_for(started.wait(), 1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert stopped.is_set()


@pytest.mark.parametrize("task_loop", [False, True])
async def test_child_cancellation_also_prevents_reusing_cancelled_state(task_loop):
    async def invoke(_inputs):
        raise asyncio.CancelledError()

    tool, child, _ = _tool(invoke, task_loop=task_loop)
    with pytest.raises(asyncio.CancelledError):
        await _invoke(tool)
    with pytest.raises(RuntimeError, match="unavailable"):
        await child.invoke({"query": "next"})


async def test_interrupt_resume_preserves_result_and_uses_a_new_active_call_budget():
    interrupt = {"result_type": "interrupt", "interrupt_ids": ["approval-1"]}
    calls = 0

    async def invoke(_inputs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return interrupt if calls == 1 else {"output": "approved"}

    tool, _, _ = _tool(invoke, timeout=0.06)
    assert await _invoke(tool) is interrupt
    # Waiting for a human is outside the next active invoke's budget.
    await asyncio.sleep(0.08)
    result = await _invoke(tool)
    assert result.success is True
    assert result.data["output"] == "approved"


async def test_task_loop_timeout_semantics_are_not_changed_in_this_small_patch():
    async def invoke(_inputs):
        await asyncio.sleep(0.06)
        return {"output": "loop done"}

    tool, _, _ = _tool(invoke, timeout=0.01, task_loop=True)
    assert (await _invoke(tool)).success is True


@pytest.mark.parametrize("other_parent", ["parent-A", "parent-B"])
async def test_one_timeout_does_not_cancel_another_child(other_parent):
    stopped = asyncio.Event()

    async def slow(_inputs):
        try:
            await asyncio.sleep(0.2)
            return {"output": "late"}
        finally:
            stopped.set()

    async def fast(_inputs):
        await asyncio.sleep(0.07)
        return {"output": "other child finished"}

    first, child_a, parent = _tool(slow)
    _, child_b, _ = _tool(fast, timeout=1)
    # Two TaskTool calls through the SAME parent wiring, different child identities.
    parent.create_subagent = lambda kind, sid, **kw: child_a if sid.endswith("call-A") else child_b
    # This factory replaces the test parent's already-installed factory only here.
    # Install on a fresh parent to exercise normal wiring rather than its marker.
    fresh = SimpleNamespace(create_subagent=parent.create_subagent)
    execution_timeout.install_subagent_timeout_wiring(fresh)
    parent.create_subagent = fresh.create_subagent
    results = await asyncio.gather(
        _invoke(first), _invoke(first, parent=other_parent, call="call-B"),
        return_exceptions=True,
    )
    assert isinstance(results[0], BaseError)
    assert "completion_timeout" in str(results[0])
    assert results[1].success is True
    assert results[1].data["output"] == "other child finished"
    assert stopped.is_set()


async def test_slow_cancellation_has_bounded_wait_and_cannot_reuse_child(monkeypatch):
    monkeypatch.setattr(execution_timeout, "_CANCEL_GRACE_SECONDS", 0.02, raising=False)
    release, stopped = asyncio.Event(), asyncio.Event()

    async def invoke(_inputs):
        try:
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            await release.wait()
            return {"output": "late success"}
        finally:
            stopped.set()

    tool, child, _ = _tool(invoke)
    running = asyncio.create_task(_invoke(tool))
    try:
        done, _ = await asyncio.wait({running}, timeout=0.4)
        assert done, "cancellation cleanup must not hold the caller indefinitely"
        with pytest.raises(BaseError, match="completion_timeout"):
            await running
        assert not stopped.is_set()
        with pytest.raises(RuntimeError, match="unavailable"):
            await child.invoke({"query": "next"})
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)
        await asyncio.wait_for(stopped.wait(), 1)
        await asyncio.sleep(0)


async def test_real_sdk_single_round_invocation_is_guarded():
    stopped = asyncio.Event()

    async def react_invoke(inputs, session):
        try:
            await asyncio.sleep(0.15)
            return {"output": "late"}
        finally:
            stopped.set()

    # Keep DeepAgent.invoke and its single-round branch real; replace only model work.
    child = DeepAgent(AgentCard(id="real-child", name="child", description="test"))
    child.configure(DeepAgentConfig(model=None, completion_timeout=0.03))
    child._initialized = True
    child._react_agent = SimpleNamespace(invoke=react_invoke)
    parent = SimpleNamespace(create_subagent=lambda *a, **kw: child)
    execution_timeout.install_subagent_timeout_wiring(parent)
    bound = parent.create_subagent("general-purpose", "real-child-session")
    with pytest.raises(RuntimeError, match="completion_timeout"):
        await bound.invoke({"query": "test", "conversation_id": "real-child-session"})
    assert stopped.is_set()


@pytest.mark.parametrize("output, expected_type", [
    ({"output": "spawn done"}, "task_completion"),
    ({"error": "completion_timeout"}, "task_failed"),
])
async def test_async_spawn_preserves_success_and_reports_child_failure(output, expected_type):
    from openjiuwen.core.controller.schema.event import EventType
    from openjiuwen.harness.task_loop.session_spawn_executor import SessionSpawnExecutor

    async def invoke(_inputs):
        return output

    async def get_task(_filter):
        return [SimpleNamespace(metadata={
            "subagent_type": "general-purpose", "task_description": "test",
            "sub_session_id": "spawn-session",
        })]

    _, _, parent = _tool(invoke)
    dependencies = SimpleNamespace(
        config=None, ability_manager=None, context_engine=None,
        task_manager=SimpleNamespace(get_task=get_task), event_queue=None,
    )
    executor = SessionSpawnExecutor(dependencies, parent)
    chunks = [item async for item in executor.execute_ability("spawn-task", Session(session_id="parent"))]
    assert len(chunks) == 1
    expected = EventType.TASK_COMPLETION if expected_type == "task_completion" else EventType.TASK_FAILED
    assert chunks[0].payload.type == expected
    if expected_type == "task_completion":
        assert chunks[0].payload.data[0].data["output"] == "spawn done"
    else:
        assert "completion_timeout" in chunks[0].payload.data[0].text


async def test_timeout_is_reported_to_sdk_cache_cleanup_as_failure(monkeypatch):
    from openjiuwen.harness.kv_cache import kv_cache_hooks

    finished = []

    async def finish(_parent, **kwargs):
        finished.append(kwargs["succeeded"])

    monkeypatch.setattr(kv_cache_hooks, "affinity_enabled", lambda _parent: True)
    monkeypatch.setattr(kv_cache_hooks, "prefetch_sticky_subagent", lambda *a, **kw: None)
    monkeypatch.setattr(kv_cache_hooks, "finish_subagent", finish)

    async def invoke(_inputs):
        return {"error": "completion_timeout"}

    tool, _, _ = _tool(invoke)
    with pytest.raises(BaseError, match="completion_timeout"):
        await _invoke(tool)
    assert finished == [False]


async def test_factory_keeps_model_and_browser_capability_arguments():
    async def invoke(_inputs):
        return {"output": "done"}

    _, child, _ = _tool(invoke)
    selected = object()
    def factory(kind, sid, *, model, browser_capabilities):
        assert kind == "browser_agent"
        assert sid == "browser-session"
        assert model is selected
        assert browser_capabilities == ["pdf"]
        return child

    parent = SimpleNamespace(create_subagent=factory)
    execution_timeout.install_subagent_timeout_wiring(parent)
    bound = parent.create_subagent("browser_agent", "browser-session", model=selected, browser_capabilities=["pdf"])
    assert (await bound.invoke({"query": "test"}))["output"] == "done"
