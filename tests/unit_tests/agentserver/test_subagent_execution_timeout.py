# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Delegation deadlines and failures through the real SDK TaskTool dispatcher."""

from __future__ import annotations

import asyncio
import sqlite3
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


async def test_unlimited_parent_delegates_with_child_deadline(tmp_path, monkeypatch, mocker):
    from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
    from openjiuwen.core.foundation.llm.schema.message import AssistantMessage
    from openjiuwen.harness import create_deep_agent
    from openjiuwen.harness.tools.subagent.task_tool import create_task_tool

    model = Model(
        ModelClientConfig(client_provider="OpenAI", api_key="offline", api_base="https://example.invalid"),
        ModelRequestConfig(model="offline"),
    )
    monkeypatch.setattr(Model, "invoke", AsyncMock(return_value=AssistantMessage(content="done")))
    parent = create_deep_agent(
        model=model, workspace=str(tmp_path), completion_timeout=None, add_general_purpose_agent=True,
        enable_sys_operation=False,
    )
    create_child = mocker.spy(parent, "create_subagent")
    (tool,) = create_task_tool(parent, available_agents="general-purpose")
    result = await tool.invoke(
        {"subagent_type": "general-purpose", "task_description": "test"},
        session=Session(session_id="unlimited-parent"), tool_call_id="delegate",
    )

    assert parent.deep_config.completion_timeout is None
    assert create_child.spy_return.deep_config.completion_timeout == 1800.0
    assert result.success and result.data["output"] == "done"


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
    ({"result_type": "error", "output": "private output", "finish_reason": "max_iterations"}, "max_iterations_reached"),
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


def _assistant(content, tool_calls=None):
    return SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls)


def _child_with_context(child, messages):
    engine = SimpleNamespace(
        get_context=lambda session_id: SimpleNamespace(get_messages=lambda: list(messages)),
    )
    child._react_agent = SimpleNamespace(context_engine=engine)
    return child


def _persist_failure() -> Exception:
    """与生产一致的存储层异常形态：SQLite 驱动直抛。"""
    return sqlite3.OperationalError("disk I/O error")


def _sqlalchemy_persist_failure(blob: str = "") -> Exception:
    """SQLAlchemy 包装形态：message 内联 SQL 与绑定参数，.orig 是驱动异常。"""
    from sqlalchemy.exc import OperationalError as SAOperationalError

    return SAOperationalError(
        "INSERT INTO kv_store",
        [("agent_state_blobs", blob)],
        sqlite3.OperationalError("disk I/O error"),
    )


@pytest.mark.parametrize("task_loop", [False, True])
async def test_checkpoint_save_failure_recovers_final_answer_with_degraded_note(
    debug_capture, task_loop,
):
    run = AsyncMock(side_effect=_persist_failure())
    child = child_for(run, task_loop=task_loop)
    _child_with_context(child, [
        SimpleNamespace(role="user", content="generate the report file"),
        _assistant("TASK-01 done, file written to /workspace/child_result.txt"),
    ])
    result = await dispatch(child)
    assert result.success is True
    assert "child_result.txt" in result.data["output"]
    assert "checkpoint_degraded" in result.data["output"]
    # 持久化失败同样永久退役该实例：checkpoint 可能已损坏，不能复用。
    with pytest.raises(BaseError, match="unavailable"):
        await dispatch(child)


async def test_sqlalchemy_wrapped_persist_failure_recovers_answer(debug_capture):
    run = AsyncMock(side_effect=_sqlalchemy_persist_failure())
    child = child_for(run)
    _child_with_context(child, [_assistant("TASK-01 done")])
    result = await dispatch(child)
    assert result.success is True
    assert "checkpoint_degraded" in result.data["output"]


def _sdk_stream_error_chunk(exc: Exception, *, with_origin: bool = True) -> dict:
    """SDK 流式转换层的真实帧形态：外层 type=answer，payload.result_type=error。"""
    payload = {"output": str(exc), "result_type": "error"}
    if with_origin:
        payload["error_origin_class"] = f"{type(exc).__module__}.{type(exc).__qualname__}"
    return {"type": "answer", "payload": payload}


async def test_stream_error_chunk_preserves_storage_degradation(debug_capture):
    # 复刻 SDK 流式转换层真实帧形态（外层 type=answer、payload.result_type=error）：
    # origin_class 经 payload 透传 + output 兜底 reason，降级在 debug 通道成立。
    run = AsyncMock(side_effect=_sqlalchemy_persist_failure())
    child = child_for(run)

    async def stream(*args, **kwargs):
        try:
            await run(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 — 复刻 SDK 转换层形态
            yield _sdk_stream_error_chunk(e)

    child.stream = stream
    _child_with_context(child, [_assistant("TASK-01 done")])
    result = await dispatch(child)
    assert result.success is True
    assert "checkpoint_degraded" in result.data["output"]
    # reason 兜底为 output 失败文本，不再只剩 subagent_error。
    assert "disk I/O error" in result.data["output"]
    with pytest.raises(BaseError, match="unavailable"):
        await dispatch(child)


async def test_error_typed_chunk_preserves_storage_degradation(debug_capture):
    # type=error 的裸 chunk（其它 emitter 形态）同样走 error 分支兜底。
    run = AsyncMock(side_effect=_sqlalchemy_persist_failure())
    child = child_for(run)

    async def stream(*args, **kwargs):
        try:
            await run(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            yield {"type": "error", "payload": {
                "output": str(e),
                "result_type": "error",
                "error_origin_class": f"{type(e).__module__}.{type(e).__qualname__}",
            }}

    child.stream = stream
    _child_with_context(child, [_assistant("TASK-01 done")])
    result = await dispatch(child)
    assert result.success is True
    assert "checkpoint_degraded" in result.data["output"]


async def test_error_typed_chunk_output_fallback_is_truncated(debug_capture):
    if debug_capture is None:
        return  # type=error 兜底只经流路径；invoke 通道异常带链走降级，不适用
    # type=error 分支的 output 兜底与 answer 分支同口径：巨型异常文本（生产见
    # 51 万字符 blob 参数）截断 400，不整段进异常与日志（CR-4 回归钉）。
    run = AsyncMock(side_effect=_sqlalchemy_persist_failure())
    child = child_for(run)

    async def stream(*args, **kwargs):
        try:
            await run(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            yield {"type": "error", "payload": {
                "output": str(e) + "[parameters: ('agent_state_blobs', '" + "y" * 5000 + "')]",
                "result_type": "error",
            }}

    child.stream = stream
    _child_with_context(child, [_assistant("TASK-01 done")])
    with pytest.raises(BaseError, match="disk I/O error") as error:
        await dispatch(child)
    assert len(str(error.value)) < 2000
    assert "y" * 5000 not in str(error.value)


async def test_locked_core_answer_chunk_without_origin_stays_fail_closed(debug_capture):
    if debug_capture is None:
        return  # error chunk 只经 debug 捕获流路径
    # 锁定 core（未含 origin_class）的过渡期：answer 帧形态无来源标记时不得降级
    # （保持原样失败），output 兜底以 origin_class 为门，reason 仍为 subagent_error
    # ——invoke 返回的 error dict 的 output 是业务输出，不得借道进错误文本。
    async def stream(*args, **kwargs):
        yield _sdk_stream_error_chunk(_sqlalchemy_persist_failure(), with_origin=False)

    child = child_for(AsyncMock())
    child.stream = stream
    with pytest.raises(BaseError, match="subagent_error"):
        await dispatch(child)


async def test_error_chunk_without_origin_class_never_degrades(debug_capture):
    if debug_capture is None:
        return  # error chunk 只经 debug 捕获流路径，invoke 参数下无此形态

    async def stream(*args, **kwargs):
        yield {"type": "error", "payload": {
            "output": "tool output mentioned (sqlite3.OperationalError) disk I/O error",
            "result_type": "error",
        }}

    child = child_for(AsyncMock())
    child.stream = stream
    # 无来源类名但有失败文本：不降级，reason 兜底为 output 文本原样透传。
    with pytest.raises(BaseError, match=r"mentioned \(sqlite3"):
        await dispatch(child)


@pytest.mark.parametrize("messages", [
    [_assistant("reading inputs", tool_calls=[{"id": "t1", "name": "write_file"}])],
    [],
])
async def test_checkpoint_save_failure_without_final_answer_is_progress_unknown(
    debug_capture, messages,
):
    run = AsyncMock(side_effect=_persist_failure())
    child = child_for(run)
    _child_with_context(child, messages)
    with pytest.raises(BaseError, match="subagent_progress_unknown") as error:
        await dispatch(child)
    # 错误必须保留存储层原始信息用于定位失败阶段，且不得声称业务未执行。
    assert "disk I/O error" in str(error.value)
    assert "workspace" in str(error.value)
    with pytest.raises(BaseError, match="unavailable"):
        await dispatch(child)


async def test_checkpoint_save_failure_without_context_engine_is_progress_unknown(debug_capture):
    run = AsyncMock(side_effect=_persist_failure())
    child = child_for(run)
    with pytest.raises(BaseError, match="subagent_progress_unknown"):
        await dispatch(child)


async def test_non_persistence_failure_never_recovers_answer(debug_capture):
    run = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    child = child_for(run)
    _child_with_context(child, [_assistant("TASK-01 done, file written")])
    with pytest.raises(BaseError, match="provider unavailable"):
        await dispatch(child)


class _BrokenStrSqliteError(sqlite3.OperationalError):
    def __str__(self) -> str:
        raise RuntimeError("__str__ is broken")


async def test_broken_str_exception_chain_never_masks_original(debug_capture):
    # 判定链上任一节点 __str__ 损坏时按非存储层处理：原样 re-raise，不得让
    # 边界判定引入新的失败模式掩盖原异常（CR-1 回归钉）。
    def broken_cause(*args, **kwargs):
        err = RuntimeError("business failure")
        err.__cause__ = _BrokenStrSqliteError("disk I/O error")
        raise err

    child = child_for(broken_cause)
    _child_with_context(child, [_assistant("TASK-01 done")])
    with pytest.raises(BaseError, match="business failure"):
        await dispatch(child)


async def test_sqlite_text_in_business_error_is_not_degraded(debug_capture):
    # 业务异常文本恰好携带 "(sqlite3." 字样时不得按存储层降级（文本嗅探误判回归钉）。
    run = AsyncMock(side_effect=RuntimeError(
        "tool output mentioned (sqlite3.OperationalError) disk I/O error in its result text"
    ))
    child = child_for(run)
    _child_with_context(child, [_assistant("TASK-01 done")])
    with pytest.raises(BaseError, match="in its result text"):
        await dispatch(child)


async def test_cancelled_error_with_sqlite_context_is_never_swallowed(debug_capture):
    async def run(*args, **kwargs):
        try:
            raise _persist_failure()
        except sqlite3.OperationalError:
            raise asyncio.CancelledError()

    child = child_for(run)
    _child_with_context(child, [_assistant("TASK-01 done")])
    # 取消契约：CancelledError 即使 __context__ 挂着 sqlite 错误也必须原样重抛。
    with pytest.raises(asyncio.CancelledError):
        await dispatch(child)


async def test_recovery_stops_at_current_turn_user_boundary(debug_capture):
    run = AsyncMock(side_effect=_persist_failure())
    child = child_for(run)
    # 本轮还没有任何 assistant 消息：不得越过本轮 user 输入取上一轮旧回答当成本次交付。
    _child_with_context(child, [
        _assistant("stale answer from the previous turn"),
        SimpleNamespace(role="user", content="new task of this turn"),
    ])
    with pytest.raises(BaseError, match="subagent_progress_unknown"):
        await dispatch(child)


async def test_degraded_note_strips_sql_and_params(debug_capture):
    run = AsyncMock(side_effect=_sqlalchemy_persist_failure(blob="x" * 5000))
    child = child_for(run)
    _child_with_context(child, [_assistant("TASK-01 done")])
    result = await dispatch(child)
    assert result.success is True
    # 模型可见文本只保异常类型与简短原因：SQL 语句与绑定参数原文不外泄。
    assert "INSERT INTO kv_store" not in result.data["output"]
    assert "x" * 5000 not in result.data["output"]
    assert "disk I/O error" in result.data["output"]
    assert len(result.data["output"]) < 2000


def test_recovery_uses_real_deep_agent_property_path():
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard
    from openjiuwen.harness.deep_agent import DeepAgent

    from jiuwenswarm.agents.harness.common.tools.subagent_executor.execution_timeout import (
        _recover_last_assistant_text,
    )

    # 真实 DeepAgent 的公开 react_agent property → context_engine 属性链防漂移钉。
    child = DeepAgent(AgentCard(id="recover-child", name="child"))
    child._react_agent = SimpleNamespace(context_engine=SimpleNamespace(
        get_context=lambda session_id: SimpleNamespace(
            get_messages=lambda: [_assistant("final answer")],
        ),
    ))
    assert _recover_last_assistant_text(child, "recover-sid") == "final answer"


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
