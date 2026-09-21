# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Timeout wiring across code/debug entry points and bounded cleanup admission."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.agents.harness.common.tools.subagent_executor import execution_timeout as guard
from jiuwenswarm.server.runtime.debug_trace import context, subagent_capture


def child_with_stream(stream, *, timeout=0.03, task_loop=False, invoke=None):
    child = SimpleNamespace(
        stream=stream,
        invoke=invoke or AsyncMock(return_value={"output": "ok"}),
        deep_config=SimpleNamespace(completion_timeout=timeout, enable_task_loop=task_loop),
    )
    parent = SimpleNamespace(create_subagent=lambda *a, **kw: child)
    guard.install_subagent_timeout_wiring(parent)
    return parent.create_subagent("general-purpose", "test-child-session")


@pytest.fixture
def debug_capture(monkeypatch):
    logger = Mock()
    logger.captures_subagent_flow.return_value = True
    token = context.set_debug_trace_logger(logger)
    monkeypatch.setattr(subagent_capture, "_ensure_observability_rail", lambda child: None)
    try:
        yield logger
    finally:
        context.reset_debug_trace_logger(token)


async def capture(child):
    return await subagent_capture.invoke_subagent_with_trace(
        child, inputs={"query": "test"}, session=None, source_label="test-child",
    )


@pytest.mark.parametrize("session_scoped", [False, True])
async def test_code_creation_installs_timeout_before_initialization(monkeypatch, session_scoped):
    from jiuwenswarm.server.runtime.agent_adapter import interface_code as code

    class InitializationReached(Exception):
        pass

    # Exercise the real create_instance entry point up to SDK initialization;
    # replace configuration/resource setup so no server or model is needed.
    cls = code.JiuwenSwarmCodeAdapter
    monkeypatch.setattr(cls, "__init__", lambda self: None)
    monkeypatch.setattr(cls, "copy_tenant_env_bindings_from", lambda *a: None)
    root = cls()
    root._skill_manager = None
    adapter = root._new_session_scoped_adapter("code-session") if session_scoped else root
    for name, value in {
        "set_checkpoint": AsyncMock(),
        "_refresh_multimodal_configs": Mock(),
        "_skip_own_instance_build": lambda: False,
        "_create_model": lambda cfg: None,
        "_tool_owner_id": lambda: "code-test",
        "_get_tool_cards": AsyncMock(return_value=[]),
        "_build_agent_rails": Mock(return_value=[]),
        "_create_sys_operation": lambda: object(),
        "_build_configured_subagents": Mock(return_value=([], False)),
        "_resolve_runtime_language": lambda: "en",
    }.items():
        monkeypatch.setattr(adapter, name, value)
    monkeypatch.setattr(code, "_resolve_instance_config_base", lambda cfg: cfg)
    monkeypatch.setattr(code, "get_agent_workspace_dir", lambda: "test-workspace")
    monkeypatch.setattr(code, "Workspace", lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr(code, "_set_workspace_coding_memory_directory", lambda *a, **kw: None)
    monkeypatch.setattr(code, "_deep_agent_kv_cache_affinity_config", lambda *a: None)
    monkeypatch.setattr(code, "build_code_system_prompt", lambda: "test")
    monkeypatch.setattr(code, "is_enterprise", lambda: False)
    child = SimpleNamespace(
        invoke=AsyncMock(return_value={"error": "provider_unavailable"}),
        deep_config=SimpleNamespace(enable_task_loop=False, completion_timeout=1),
    )
    parent = SimpleNamespace(
        create_subagent=lambda *a: child,
        ensure_initialized=AsyncMock(side_effect=InitializationReached),
    )
    monkeypatch.setattr(code, "create_deep_agent", lambda **kw: parent)
    with pytest.raises(InitializationReached):
        await adapter.create_instance(config_base={"react": {}})
    bound = parent.create_subagent("explore_agent", "code-session_sub_explore")
    with pytest.raises(RuntimeError, match="subagent_failed: provider_unavailable"):
        await bound.invoke({})


async def test_debug_stream_has_deadline_and_cleans_its_context(debug_capture):
    stopped = asyncio.Event()
    value = ContextVar("stream-test-value", default="parent")

    async def stream(*a, **kw):
        token = value.set("child")
        try:
            yield {"type": "llm_output", "payload": {"content": "partial"}}
            await asyncio.Event().wait()
        finally:
            value.reset(token)
            stopped.set()

    child = child_with_stream(stream)
    with pytest.raises(RuntimeError, match="completion_timeout"):
        await asyncio.wait_for(capture(child), 1)
    assert stopped.is_set()
    assert value.get() == "parent"
    debug_capture.end_subagent.assert_called_once()
    with pytest.raises(RuntimeError, match="unavailable"):
        await child.invoke({})
    with pytest.raises(RuntimeError, match="unavailable"):
        await anext(child.stream({}))


@pytest.mark.parametrize("chunk", [
    {"type": "error", "payload": {"error": "provider_unavailable"}},
    {"type": "answer", "payload": {"result_type": "error", "message": "provider_unavailable"}},
    {"result_type": "error", "error": "provider_unavailable"},
])
@pytest.mark.parametrize("wired", [False, True])
async def test_debug_error_never_reduces_to_empty_success(debug_capture, chunk, wired):
    async def stream(*a, **kw):
        yield chunk

    child = child_with_stream(stream) if wired else SimpleNamespace(stream=stream)
    with pytest.raises(RuntimeError, match="subagent_failed: provider_unavailable"):
        await capture(child)
    if wired:
        with pytest.raises(RuntimeError, match="unavailable"):
            await child.invoke({})


@pytest.mark.parametrize("task_loop", [False, True])
async def test_stream_preserves_recoverable_tool_error_and_success(debug_capture, task_loop):
    async def stream(*a, **kw):
        yield {"type": "tool_result", "payload": {"error": "recoverable"}}
        yield {"type": "answer", "payload": {"output": "done"}}

    child = child_with_stream(stream, task_loop=task_loop)
    assert (await capture(child))["output"] == "done"
    assert (await child.invoke({}))["output"] == "ok"


async def test_stream_and_invoke_share_active_state_and_close_cancels_producer():
    stopped = asyncio.Event()

    async def stream(*a, **kw):
        try:
            yield {"type": "llm_output", "payload": {"content": "partial"}}
            await asyncio.Event().wait()
        finally:
            stopped.set()

    child = child_with_stream(stream, timeout=2)
    iterator = child.stream({})
    await anext(iterator)
    try:
        with pytest.raises(RuntimeError, match="active invocation"):
            await child.invoke({})
    finally:
        await iterator.aclose()
    assert stopped.is_set()
    with pytest.raises(RuntimeError, match="unavailable"):
        await child.invoke({})


async def test_stream_cancellation_has_bounded_cleanup_and_discards_late_answer(monkeypatch, debug_capture):
    monkeypatch.setattr(guard, "_CANCEL_GRACE_SECONDS", 0.01)
    release, stopped = asyncio.Event(), asyncio.Event()
    pending = set()
    monkeypatch.setattr(guard, "_PENDING_CLEANUP", pending)

    async def stream(*a, **kw):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            yield {"type": "answer", "payload": {"output": "late success"}}
        finally:
            stopped.set()

    child = child_with_stream(stream, timeout=0.01)
    try:
        with pytest.raises(RuntimeError, match="coroutine_cleanup=pending"):
            await asyncio.wait_for(capture(child), 1)
        assert not stopped.is_set()
        debug_capture.feed_subagent.assert_not_called()
    finally:
        release.set()
        await asyncio.gather(*list(pending))
        await asyncio.sleep(0)
    assert stopped.is_set()
    assert not pending
    with pytest.raises(RuntimeError, match="unavailable"):
        await anext(child.stream({}))


async def test_task_loop_stream_keeps_existing_deadline_semantics(debug_capture):
    async def stream(*a, **kw):
        await asyncio.sleep(0.04)
        yield {"type": "answer", "payload": {"output": "loop done"}}

    child = child_with_stream(stream, timeout=0.01, task_loop=True)
    assert (await capture(child))["output"] == "loop done"


async def test_real_sdk_single_round_stream_is_guarded(debug_capture):
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard
    from openjiuwen.harness.deep_agent import DeepAgent
    from openjiuwen.harness.schema.config import DeepAgentConfig

    stopped = asyncio.Event()

    async def react_stream(inputs, session, stream_modes):
        try:
            yield {"type": "llm_output", "payload": {"content": "partial"}}
            await asyncio.Event().wait()
        finally:
            stopped.set()

    child = DeepAgent(AgentCard(id="real-stream-child", name="child", description="test"))
    child.configure(DeepAgentConfig(model=None, completion_timeout=0.03))
    child._initialized = True
    child._react_agent = SimpleNamespace(stream=react_stream)
    parent = SimpleNamespace(create_subagent=lambda *a: child)
    guard.install_subagent_timeout_wiring(parent)
    bound = parent.create_subagent("general-purpose", "real-stream-session")
    with pytest.raises(RuntimeError, match="completion_timeout"):
        await asyncio.wait_for(capture(bound), 1)
    assert stopped.is_set()


@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_cleanup_backlog_rejects_new_work_then_recovers(monkeypatch, mode):
    monkeypatch.setattr(guard, "_MAX_PENDING_CLEANUP", 1)
    monkeypatch.setattr(guard, "_CANCEL_GRACE_SECONDS", 0.01)
    pending = set()
    monkeypatch.setattr(guard, "_PENDING_CLEANUP", pending)
    release = asyncio.Event()

    async def slow(*a, **kw):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            return {"output": "late"}

    async def empty_stream(*a, **kw):
        yield {"type": "answer", "payload": {"output": "ok"}}

    slow_child = child_with_stream(empty_stream, timeout=0.01, invoke=slow)
    fresh_invoke = AsyncMock(return_value={"output": "ok"})
    stream_started = []

    async def fresh_stream(*a, **kw):
        stream_started.append(True)
        yield {"type": "answer", "payload": {"output": "ok"}}

    fresh_child = child_with_stream(fresh_stream, invoke=fresh_invoke)
    try:
        with pytest.raises(RuntimeError, match="coroutine_cleanup=pending"):
            await slow_child.invoke({})
        assert len(pending) == 1
        with pytest.raises(RuntimeError, match="subagent_cleanup_backlog"):
            if mode == "invoke":
                await fresh_child.invoke({})
            else:
                await anext(fresh_child.stream({}))
        fresh_invoke.assert_not_called()
        assert not stream_started
        assert len(pending) == 1  # Live tasks remain tracked, never evicted.
    finally:
        release.set()
        await asyncio.gather(*list(pending))
        await asyncio.sleep(0)
    assert not pending
    assert (await fresh_child.invoke({}))["output"] == "ok"


async def test_debug_task_tool_reports_stream_error(monkeypatch, debug_capture):
    from openjiuwen.core.common.exception.errors import BaseError
    from openjiuwen.core.foundation.tool import ToolCard
    from openjiuwen.core.session.agent import Session
    from openjiuwen.harness.tools.subagent.task_tool import TaskTool
    from jiuwenswarm.server.runtime.debug_trace import task_tool_patch

    async def stream(*a, **kw):
        yield {"type": "error", "payload": {"error": "provider_unavailable"}}

    child = child_with_stream(stream)
    parent = SimpleNamespace(create_subagent=lambda *a: child)
    # Restore the SDK class and installation flags when this test ends.
    monkeypatch.setattr(TaskTool, "invoke", TaskTool.invoke)
    monkeypatch.setattr(TaskTool, "debug_trace_patch_applied", False, raising=False)
    monkeypatch.setattr(task_tool_patch, "_PATCH_APPLIED", False)
    task_tool_patch.apply_task_tool_debug_patch()
    tool = TaskTool(ToolCard(id="debug-test", name="task_tool", description="test"), parent)
    with pytest.raises(BaseError, match="provider_unavailable"):
        await tool.invoke(
            {"subagent_type": "general-purpose", "task_description": "test"},
            session=Session(session_id="debug-parent"),
        )
