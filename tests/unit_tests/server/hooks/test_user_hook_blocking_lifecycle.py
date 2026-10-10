# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""PreToolUse blocking must preserve feedback and stop automatic continuation."""

import asyncio
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall, ToolMessage
from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.agents.react_agent import ReActAgent
from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.core.single_agent.interrupt.state import ToolInterruptEntry, ToolInterruptionState
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext, AgentCallbackEvent, RunContext, TaskIterationInputs, rail as with_rails,
)
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.goal.evaluation import GoalEvaluator
from openjiuwen.harness.goal.manager import GoalManager
from openjiuwen.harness.goal.schema import GoalRecord, GoalStatus, GoalStopConfig, GoalStopStrategy
from openjiuwen.harness.goal.store import SessionGoalStore
from openjiuwen.harness.rails.task_completion_rail import TaskCompletionRail
from openjiuwen.harness.schema.config import DeepAgentConfig
from openjiuwen.harness.schema.interaction import RoundWorkItem
from openjiuwen.harness.task_loop.event_manager import EventManager
from openjiuwen.harness.task_loop.loop_coordinator import LoopCoordinator

from jiuwenswarm.common.hooks_config import HooksConfig, HookMatcher
from jiuwenswarm.server.hooks.executor import HookResult
from jiuwenswarm.server.hooks.user_hook_rail import UserHookRail


@pytest.mark.asyncio
@pytest.mark.parametrize("later_tool", [False, True])
@pytest.mark.parametrize("hook", [
    {"command": "echo 'policy denies this operation' >&2; exit 2"},
    {"command": "echo '{\"decision\":\"block\",\"reason\":\"policy denies this operation\"}'"},
    {"type": "prompt", "prompt": "review this operation"},
])
async def test_blocked_tool_ends_react_without_another_model_call(monkeypatch, hook, later_tool):
    rail = UserHookRail(HooksConfig(events={
        "PreToolUse": [HookMatcher(matcher="Write", hooks=[hook])],
        "PostToolUse": [HookMatcher(matcher="*", hooks=[{"command": "exit 0"}])],
    }))
    if hook.get("type") == "prompt":
        monkeypatch.setattr(rail._executor, "_query_llm", AsyncMock(
            return_value='{"decision":"block","reason":"policy denies this operation"}',
        ))
    run_hooks = AsyncMock(wraps=rail._executor.run_all)
    monkeypatch.setattr(rail._executor, "run_all", run_hooks)
    agent = ReActAgent(AgentCard(id=uuid4().hex, name="hook-test", description="hook-test"))
    agent._config.parallel_tool_calls = False
    await agent.register_rail(rail)
    context = SimpleNamespace(add_messages=AsyncMock(), get_messages=lambda: [])
    monkeypatch.setattr(agent, "_init_context", AsyncMock(return_value=context))
    monkeypatch.setattr(agent, "_sync_prompt_attachments", AsyncMock())
    monkeypatch.setattr(agent.context_engine, "save_contexts", AsyncMock())
    tool_calls = [ToolCall(id="blocked-call", type="function", name="Write", arguments="{}")]
    if later_tool:
        tool_calls.append(ToolCall(id="alternative-call", type="function", name="Bash", arguments="{}"))
    model = AsyncMock(side_effect=[
        AssistantMessage(content="", tool_calls=tool_calls),
        AssistantMessage(content="The model was allowed to retry."),
    ])
    @with_rails(before=AgentCallbackEvent.BEFORE_MODEL_CALL, after=AgentCallbackEvent.AFTER_MODEL_CALL)
    async def model_call(_agent, ctx):
        return await model()

    monkeypatch.setattr(agent, "_railed_model_call", MethodType(model_call, agent))
    tool = AsyncMock(return_value=("ran", ToolMessage(content="ran", tool_call_id="alternative-call")))
    monkeypatch.setattr(agent.ability_manager, "_execute_single_tool_call", tool)

    session_id = uuid4().hex
    try:
        result = await agent.invoke({"query": "write a file", "conversation_id": session_id})

        tool.assert_not_awaited()
        assert model.await_count == 1
        assert result["result_type"] == "answer"
        assert result["stop_reason"] == "hook_blocked"
        assert "policy denies this operation" in result["output"]
        messages = [call.args[0] for call in context.add_messages.await_args_list]
        tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
        assert [message.tool_call_id for message in tool_messages] == [call.id for call in tool_calls]
        assert all("policy denies this operation" in message.content for message in tool_messages)
        assert run_hooks.await_count == 1  # skipped tools must not run PostToolUse

        model.reset_mock(side_effect=True)
        model.side_effect = [
            AssistantMessage(content="", tool_calls=[ToolCall(
                id="alternative-call", type="function", name="Bash", arguments="{}",
            )]),
            AssistantMessage(content="new request handled"),
        ]
        result = await agent.invoke({"query": "a new request", "conversation_id": session_id})
        assert result == {"output": "new request handled", "result_type": "answer"}
        assert model.await_count == 2
        tool.assert_awaited_once()
    finally:
        await agent.unregister_rail(rail)


@pytest.mark.asyncio
async def test_parallel_hook_allow_cannot_override_another_tools_block(monkeypatch):
    rail = UserHookRail(HooksConfig(events={
        "PreToolUse": [HookMatcher(matcher="*", hooks=[{"command": "unused"}])],
    }))
    read_started = asyncio.Event()
    write_blocked = asyncio.Event()

    async def run_hooks(_configs, hook_input):
        if hook_input["tool_name"] == "Write":
            await read_started.wait()
            return [HookResult(outcome="blocking", error="policy block")]
        read_started.set()
        await write_blocked.wait()
        return [HookResult()]

    async def after_tool(ctx):
        if ctx.inputs.tool_name == "Write":
            write_blocked.set()

    monkeypatch.setattr(rail._executor, "run_all", run_hooks)
    agent = ReActAgent(AgentCard(id=uuid4().hex, name="parallel-hooks", description="parallel-hooks"))
    await agent.register_rail(rail)
    await agent.register_callback(AgentCallbackEvent.AFTER_TOOL_CALL, after_tool)
    tool = AsyncMock()
    monkeypatch.setattr(agent.ability_manager, "_execute_single_tool_call", tool)
    ctx = AgentCallbackContext(agent=agent)
    await ctx.fire(AgentCallbackEvent.BEFORE_INVOKE)
    calls = [ToolCall(id=name, type="function", name=name, arguments="{}") for name in ("Write", "Read")]
    try:
        results = await asyncio.wait_for(
            agent.ability_manager.execute(ctx, calls, session=None, parallel_tool_calls=True),
            timeout=5,
        )
    finally:
        await agent.agent_callback_manager.clear()

    tool.assert_not_awaited()
    assert [message.tool_call_id for _, message in results] == ["Write", "Read"]
    assert all(result["status"] == "blocked" for result, _ in results)
    assert all("policy block" in message.content for _, message in results)
    assert ctx.consume_force_finish().result["stop_reason"] == "hook_blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize(("goal_change", "resume_mode"), [
    ("unchanged", "blocked"), ("unchanged", "interrupt_then_block"), ("unchanged", "allowed"),
    ("replaced", "blocked"), ("resumed", "blocked"), ("unrelated", "blocked"),
])
async def test_goal_approval_resume_stops_only_its_blocked_original_goal(monkeypatch, goal_change, resume_mode):
    inner = ReActAgent(AgentCard(id=uuid4().hex, name="goal-resume", description="goal-resume"))
    inner._config.parallel_tool_calls = False
    outer = DeepAgent(AgentCard(id=uuid4().hex, name="outer-goal-resume", description="outer-goal-resume"))
    outer._react_agent = inner
    outer._loop_coordinator = LoopCoordinator()
    outer.loop_coordinator.reset()
    record = GoalRecord.create(session_id=uuid4().hex, objective="original goal")
    store = SimpleNamespace(session_id=record.session_id, load=lambda: record, save=Mock(), commit=AsyncMock())
    outer.goal_manager = GoalManager(
        store=store, event_manager=outer.event_manager, control_lock=outer._interaction_control_lock,
        has_output_stream=lambda: True, cancel_active_round=AsyncMock(), emit_event=Mock(), notify_work=Mock(),
    )
    config = HooksConfig(events={
        "PreToolUse": [HookMatcher(matcher="Write", hooks=[{"command": "unused"}])],
    })
    rail = UserHookRail(config)
    await outer._register_rail_selective(rail)
    context = SimpleNamespace(add_messages=AsyncMock(), get_messages=lambda: [])
    monkeypatch.setattr(inner, "_init_context", AsyncMock(return_value=context))
    monkeypatch.setattr(inner, "_sync_prompt_attachments", AsyncMock())
    monkeypatch.setattr(inner.context_engine, "save_contexts", AsyncMock())
    model = AsyncMock(return_value=AssistantMessage(content="done"))

    async def execute_tool(*, tool_call, **_kwargs):
        return "ran", ToolMessage(content="ran", tool_call_id=tool_call.id)

    tool = AsyncMock(side_effect=execute_tool)
    monkeypatch.setattr(inner, "_railed_model_call", model)
    monkeypatch.setattr(inner.ability_manager, "_execute_single_tool_call", tool)
    monkeypatch.setattr(outer, "prepare_interaction_task_loop", AsyncMock(
        return_value=(outer.loop_coordinator, SimpleNamespace()),
    ))
    monkeypatch.setattr(outer, "_sync_expert_role_attachment", AsyncMock())
    write_result = AsyncMock()
    monkeypatch.setattr(outer, "_write_round_result_to_stream", write_result)
    monkeypatch.setattr(outer, "save_state", Mock())
    monkeypatch.setattr(outer, "clear_state", Mock())
    session = create_agent_session(session_id=record.session_id, card=inner.card)
    await session.pre_run()
    calls = [ToolCall(id=name, type="function", name=name, arguments="{}") for name in ("Write", "Bash")]
    try:
        # The original goal round has reached HITL and persisted its interrupted tools.
        inner._hitl_handler.save(ToolInterruptionState(
            ai_message=AssistantMessage(content="", tool_calls=calls), iteration=0,
            interrupted_tools={call.id: ToolInterruptEntry(tool_call=call) for call in calls},
        ), session)
        ctx = AgentCallbackContext(agent=outer, session=session, inputs=TaskIterationInputs(
            iteration=1, loop_event=None, query="original goal",
            run_kind="normal" if goal_change == "unrelated" else "goal",
            run_context=RunContext(session_id=record.session_id, extra={
                "goal_id": record.goal_id, "revision": record.revision,
            }),
            result={"result_type": "interrupt", "interrupt_ids": [call.id for call in calls]},
        ))
        await ctx.fire(AgentCallbackEvent.BEFORE_TASK_ITERATION)
        await ctx.fire(AgentCallbackEvent.AFTER_TASK_ITERATION)
        assert record.status is GoalStatus.ACTIVE

        # Rail replacement must not lose the pending approval's goal identity.
        await outer.unregister_rail(rail)
        rail = UserHookRail(config)
        monkeypatch.setattr(rail._executor, "run_all", AsyncMock(
            return_value=[HookResult() if resume_mode == "allowed"
                          else HookResult(outcome="blocking", error="policy block")],
        ))
        await outer._register_rail_selective(rail)
        if goal_change == "replaced":
            record = GoalRecord.create(session_id=record.session_id, objective="replacement goal")
        elif goal_change == "resumed":
            record.revision += 1

        # An ACTIVE goal can already have a continuation queued while waiting for approval.
        assert outer.goal_manager.ensure_active_goal_work_locked()
        after_task = AsyncMock()
        await outer.register_callback(AgentCallbackEvent.AFTER_TASK_ITERATION, after_task)
        work = RoundWorkItem.user(request_id="approval", inputs={
            "query": InteractiveInput({"approved": True}), "conversation_id": record.session_id,
        }, reset_loop=False)
        if resume_mode == "interrupt_then_block":
            async def ask_again(tool_ctx):
                raise AbortError(
                    reason="confirm again", cause=ToolInterruptException(
                        InterruptRequest(message="confirm again"), tool_call=tool_ctx.inputs.tool_call,
                    ),
                )

            await inner.register_callback(AgentCallbackEvent.BEFORE_TOOL_CALL, ask_again, priority=100)
            outcome = await outer.run_one_round(work, "second-approval", session)
            assert outcome.error_code is None
            assert write_result.await_args.args[0]["result_type"] == "interrupt"
            assert record.status is GoalStatus.ACTIVE
            store.commit.assert_not_awaited()
            await inner.agent_callback_manager.unregister(AgentCallbackEvent.BEFORE_TOOL_CALL, ask_again)
        outcome = await outer.run_one_round(work, "resume-round", session)

        assert outcome.error_code is None
        after_task.assert_not_awaited()  # actual approval route bypasses the task-iteration callbacks
        if resume_mode == "allowed":
            assert write_result.await_args.args[0] == {"output": "done", "result_type": "answer"}
            assert tool.await_count == 2
            model.assert_awaited_once()
            assert not outer.loop_coordinator.is_aborted
            assert record.status is GoalStatus.ACTIVE
            assert outer.event_manager.next_work().kind == "goal"
            assert session.get_state("_user_hook_pending_goal") is None
            store.commit.assert_not_awaited()
            return

        assert write_result.await_args.args[0]["stop_reason"] == "hook_blocked"
        tool.assert_not_awaited()
        model.assert_not_awaited()
        assert outer.loop_coordinator.is_aborted
        assert session.get_state("_user_hook_pending_goal") is None
        if goal_change == "unchanged":
            assert record.status is GoalStatus.BLOCKED
            assert "policy block" in record.last_assessment.evidence
            store.commit.assert_awaited_once()
            assert outer.event_manager.next_work() is None
            assert not outer._should_keep_interaction_open_locked()
            assert outer.event_manager.next_work() is None
            await outer.goal_manager.resume()
            assert record.status is GoalStatus.ACTIVE
            assert outer.event_manager.next_work().kind == "goal"
        else:
            assert record.status is GoalStatus.ACTIVE
            assert record.last_assessment is None
            store.commit.assert_not_awaited()
            assert outer.event_manager.next_work().context["goal_id"] == record.goal_id
    finally:
        await inner.agent_callback_manager.clear()
        await outer.agent_callback_manager.clear()
        await session.post_run()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [True, False])
async def test_only_hook_blocking_stops_the_outer_task_loop(blocked):
    coordinator = LoopCoordinator()
    coordinator.reset()
    result = {"output": "finished", "result_type": "answer"}
    if blocked:
        result["stop_reason"] = "hook_blocked"
    ctx = AgentCallbackContext(
        agent=SimpleNamespace(loop_coordinator=coordinator),
        inputs=TaskIterationInputs(iteration=1, loop_event=None, result=result),
    )

    await UserHookRail(HooksConfig()).after_task_iteration(ctx)

    assert coordinator.should_continue() is not blocked
    coordinator.reset()
    assert coordinator.should_continue()  # a new user request is still allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["react", "deep_resume", "deep_task_resume"])
async def test_resumed_batch_blocks_later_tools_and_allows_a_new_request(monkeypatch, entrypoint):
    rail = UserHookRail(HooksConfig(events={
        "PreToolUse": [HookMatcher(matcher="Write", hooks=[{"command": "unused"}])],
    }))
    run_hooks = AsyncMock(return_value=[HookResult(outcome="blocking", error="policy block")])
    monkeypatch.setattr(rail._executor, "run_all", run_hooks)
    agent = ReActAgent(AgentCard(id=uuid4().hex, name="resume-hooks", description="resume-hooks"))
    agent._config.parallel_tool_calls = False
    outer = DeepAgent(AgentCard(id=uuid4().hex, name="outer", description="outer"))
    outer._react_agent = agent
    if entrypoint == "react":
        await agent.register_rail(rail)
    else:
        # Exercise DeepAgent's actual routing: BEFORE_INVOKE is outer-only.
        await outer._register_rail_selective(rail)
    context = SimpleNamespace(add_messages=AsyncMock(), get_messages=lambda: [])
    monkeypatch.setattr(agent, "_init_context", AsyncMock(return_value=context))
    monkeypatch.setattr(agent, "_sync_prompt_attachments", AsyncMock())
    monkeypatch.setattr(agent.context_engine, "save_contexts", AsyncMock())
    calls = [ToolCall(id=name, type="function", name=name, arguments="{}") for name in ("Write", "Bash")]

    def load_interrupt(_session):
        return ToolInterruptionState(
            ai_message=AssistantMessage(content="", tool_calls=calls), iteration=0,
            interrupted_tools={call.id: ToolInterruptEntry(tool_call=call) for call in calls},
        )

    monkeypatch.setattr(agent._hitl_handler, "load", load_interrupt)
    model = AsyncMock(return_value=AssistantMessage(content="new request handled"))
    monkeypatch.setattr(agent, "_railed_model_call", model)

    async def execute_tool(*, tool_call, **_kwargs):
        return "ran", ToolMessage(content="ran", tool_call_id=tool_call.id)

    tool = AsyncMock(side_effect=execute_tool)
    monkeypatch.setattr(agent.ability_manager, "_execute_single_tool_call", tool)
    session_id = uuid4().hex

    async def invoke():
        if entrypoint != "react":
            event = (AgentCallbackEvent.BEFORE_INVOKE if entrypoint == "deep_resume"
                     else AgentCallbackEvent.BEFORE_TASK_ITERATION)
            await AgentCallbackContext(agent=outer).fire(event)
        # The inner invocation may run in a child task of the outer lifecycle.
        return await asyncio.create_task(agent.invoke({"query": "approved", "conversation_id": session_id}))

    try:
        result = await invoke()
        tool.assert_not_awaited()
        model.assert_not_awaited()
        assert result["stop_reason"] == "hook_blocked"
        messages = [call.args[0] for call in context.add_messages.await_args_list]
        assert [msg.tool_call_id for msg in messages if isinstance(msg, ToolMessage)] == ["Write", "Bash"]
        assert all("policy block" in msg.content for msg in messages if isinstance(msg, ToolMessage))

        # Another resume on the same session also starts with a clean verdict.
        run_hooks.return_value = [HookResult()]
        result = await invoke()
        assert result == {"output": "new request handled", "result_type": "answer"}
        assert tool.await_count == 2
        model.assert_awaited_once()
    finally:
        await agent.agent_callback_manager.clear()
        await outer.agent_callback_manager.clear()


@pytest.mark.asyncio
async def test_concurrent_invocations_do_not_share_a_blocking_verdict(monkeypatch):
    rail = UserHookRail(HooksConfig(events={
        "PreToolUse": [HookMatcher(matcher="Write", hooks=[{"command": "unused"}])],
    }))
    blocked = asyncio.Event()
    allowed_started = asyncio.Event()

    async def run_hooks(_configs, hook_input):
        await allowed_started.wait()
        return [HookResult(outcome="blocking", error="policy block")]

    async def after_tool(ctx):
        if ctx.inputs.tool_name == "Write":
            blocked.set()

    monkeypatch.setattr(rail._executor, "run_all", run_hooks)
    agent = ReActAgent(AgentCard(id=uuid4().hex, name="isolated-hooks", description="isolated-hooks"))
    await agent.register_rail(rail)
    await agent.register_callback(AgentCallbackEvent.AFTER_TOOL_CALL, after_tool)
    tool = AsyncMock(return_value=("ran", ToolMessage(content="ran", tool_call_id="Read")))
    monkeypatch.setattr(agent.ability_manager, "_execute_single_tool_call", tool)

    async def invoke(tool_name):
        ctx = AgentCallbackContext(agent=agent)
        await ctx.fire(AgentCallbackEvent.BEFORE_INVOKE)
        if tool_name == "Read":
            allowed_started.set()
            await blocked.wait()
        calls = [ToolCall(id=tool_name, type="function", name=tool_name, arguments="{}")]
        results = await agent.ability_manager.execute(ctx, calls, session=None)
        return results, ctx.consume_force_finish()

    try:
        blocked_result, allowed_result = await asyncio.wait_for(
            asyncio.gather(invoke("Write"), invoke("Read")), timeout=5,
        )
        assert blocked_result[1].result["stop_reason"] == "hook_blocked"
        assert allowed_result[0][0][0] == "ran"
        assert allowed_result[1] is None
        tool.assert_awaited_once()
    finally:
        await agent.agent_callback_manager.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("structured_context", [False, True])
@pytest.mark.parametrize("stale", [False, True])
async def test_hook_blocking_stops_goal_continuation_until_explicit_resume(structured_context, stale):
    record = GoalRecord.create(session_id="goal-session", objective="finish the task")
    store = SimpleNamespace(session_id=record.session_id, load=lambda: record, save=Mock(), commit=AsyncMock())
    events = EventManager()
    manager = GoalManager(
        store=store, event_manager=events, control_lock=asyncio.Lock(),
        has_output_stream=lambda: True, cancel_active_round=AsyncMock(),
        emit_event=Mock(), notify_work=Mock(),
    )
    completion = TaskCompletionRail()
    completion.set_goal_manager(manager)
    completion._goal_evaluator = GoalEvaluator(GoalStopConfig(strategy=GoalStopStrategy.AGENT_REPORT))
    agent = DeepAgent(AgentCard(id=uuid4().hex, name="goal-hooks", description="goal-hooks"))
    agent.goal_manager = manager
    agent._loop_coordinator = LoopCoordinator()
    agent.loop_coordinator.reset()
    rail = UserHookRail(HooksConfig())
    await agent._register_rail_selective(completion)
    await agent._register_rail_selective(rail)
    run_context = {"session_id": record.session_id, "goal_id": record.goal_id, "revision": record.revision}
    if structured_context:
        run_context = RunContext(session_id=record.session_id, extra=run_context)
    ctx = AgentCallbackContext(agent=agent, inputs=TaskIterationInputs(
        iteration=1, loop_event=None, run_kind="goal", run_context=run_context, query="finish the task",
    ))
    try:
        await ctx.fire(AgentCallbackEvent.BEFORE_TASK_ITERATION)
        if stale:
            record.revision += 1
        ctx.inputs.result = {
            "output": "[Hook blocked] PreToolUse blocked Write: policy block",
            "result_type": "answer", "stop_reason": "hook_blocked",
        }
        await ctx.fire(AgentCallbackEvent.AFTER_TASK_ITERATION)

        assert not agent.loop_coordinator.should_continue()
        assert events.next_work() is None
        if stale:
            assert record.status is GoalStatus.ACTIVE
            assert record.last_assessment is None
            store.commit.assert_not_awaited()
        else:
            assert record.status is GoalStatus.BLOCKED
            assert "policy block" in record.last_assessment.evidence
            store.commit.assert_awaited_once()
            assert not manager.ensure_active_goal_work_locked()
            assert await manager.begin_attempt(goal_id=record.goal_id, revision=record.revision) is None

            await manager.resume()
            assert record.status is GoalStatus.ACTIVE
            work = events.next_work()
            assert work is not None and work.kind == "goal"
            assert work.context["revision"] == record.revision
    finally:
        await agent.agent_callback_manager.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(("run_kind", "user_action", "approvals"), [
    ("goal", "none", 0), ("goal", "resume", 0), ("goal", "replace", 0), ("normal", "none", 0),
    ("goal", "none", 1), ("goal", "none", 2), ("normal", "none", 1),
])
async def test_full_task_loop_handles_a_block_once_and_preserves_user_action(
    monkeypatch, run_kind, user_action, approvals,
):
    """Use the real task scheduler, task executor, ReAct and session Goal store."""
    inner = ReActAgent(AgentCard(id=uuid4().hex, name="full-loop-inner", description="hook lifecycle"))
    inner._config.parallel_tool_calls = False
    outer = DeepAgent(AgentCard(id=uuid4().hex, name="full-loop-outer", description="hook lifecycle"))
    outer._react_agent = inner
    outer._deep_config = DeepAgentConfig(enable_task_loop=True, completion_timeout=5)
    session = create_agent_session(session_id=uuid4().hex, card=outer.card)
    await session.pre_run()
    store = SessionGoalStore(session)
    manager = GoalManager(
        store=store, event_manager=outer.event_manager, control_lock=outer._interaction_control_lock,
        has_output_stream=lambda: True, cancel_active_round=AsyncMock(), emit_event=Mock(), notify_work=Mock(),
    )
    outer.goal_manager = manager
    record = GoalRecord.create(session_id=session.get_session_id(), objective="original goal")
    if run_kind == "goal":
        store.save(record)
    completion = TaskCompletionRail()
    completion.set_goal_manager(manager)
    completion._goal_evaluator = GoalEvaluator(GoalStopConfig(strategy=GoalStopStrategy.AGENT_REPORT))
    outer._task_completion_rail = completion
    await outer._register_rail_selective(completion)
    rail = UserHookRail(HooksConfig(events={
        "PreToolUse": [HookMatcher(matcher="Write", hooks=[{"command": "echo 'policy block' >&2; exit 2"}])],
    }))
    await outer._register_rail_selective(rail)
    remaining_approvals = approvals

    async def request_approval(ctx):
        if remaining_approvals:
            raise AbortError(reason="approval required", cause=ToolInterruptException(
                InterruptRequest(message="approval required"), tool_call=ctx.inputs.tool_call,
            ))

    await inner.register_callback(AgentCallbackEvent.BEFORE_TOOL_CALL, request_approval, priority=100)
    context = SimpleNamespace(add_messages=AsyncMock(), get_messages=lambda: [])
    monkeypatch.setattr(inner, "_init_context", AsyncMock(return_value=context))
    monkeypatch.setattr(inner, "_sync_prompt_attachments", AsyncMock())
    monkeypatch.setattr(inner.context_engine, "save_contexts", AsyncMock())
    monkeypatch.setattr(outer, "_sync_expert_role_attachment", AsyncMock())
    model = AsyncMock(return_value=AssistantMessage(content="", tool_calls=[
        ToolCall(id=name, type="function", name=name, arguments="{}") for name in ("Write", "Bash")
    ]))

    @with_rails(before=AgentCallbackEvent.BEFORE_MODEL_CALL, after=AgentCallbackEvent.AFTER_MODEL_CALL)
    async def model_call(_agent, ctx):
        return await model()

    monkeypatch.setattr(inner, "_railed_model_call", MethodType(model_call, inner))
    tool = AsyncMock()
    monkeypatch.setattr(inner.ability_manager, "_execute_single_tool_call", tool)
    apply_assessment = AsyncMock(wraps=manager.apply_assessment)
    monkeypatch.setattr(manager, "apply_assessment", apply_assessment)
    final_results = []

    async def write_result(result, _session):
        final_results.append(result)
        if result.get("result_type") == "interrupt":
            if run_kind == "goal":
                assert (await manager.get()).status is GoalStatus.ACTIVE
            return
        if result.get("stop_reason") != "hook_blocked":
            return
        if run_kind == "goal":
            # An approval goes straight to AFTER_INVOKE; ordinary task rounds
            # have already committed their block in AFTER_TASK_ITERATION.
            expected = GoalStatus.ACTIVE if approvals else GoalStatus.BLOCKED
            assert (await manager.get()).status is expected
        # This is after AFTER_TASK_ITERATION, before AFTER_INVOKE. The goal
        # work is still active, so resume deliberately keeps its revision.
        if user_action == "resume":
            resumed = await manager.resume()
            assert resumed.status is GoalStatus.ACTIVE
            assert resumed.revision == record.revision
        elif user_action == "replace":
            await manager.set("replacement goal", overwrite_confirmed=True)

    monkeypatch.setattr(outer, "_write_round_result_to_stream", write_result)
    if run_kind == "goal":
        work = RoundWorkItem.goal(inputs={"query": "original goal"}, goal_id=record.goal_id,
                                  revision=record.revision, session_id=record.session_id)
    else:
        work = RoundWorkItem.user(request_id="normal", inputs={"query": "write a file"})
    outer.event_manager.mark_started(work)
    try:
        if run_kind == "goal":
            await manager.begin_attempt(goal_id=record.goal_id, revision=record.revision)
        outcome = await asyncio.wait_for(outer.run_one_round(work, uuid4().hex, session), timeout=10)
        outer.event_manager.mark_finished(work)
        if approvals:
            await outer.unregister_rail(rail)
            rail = UserHookRail(rail._config)
            await outer._register_rail_selective(rail)
        for _ in range(approvals):
            assert outcome.error_code is None
            assert final_results[-1]["result_type"] == "interrupt"
            apply_assessment.assert_not_awaited()
            remaining_approvals -= 1
            work = RoundWorkItem.user(request_id="approval", inputs={
                "query": InteractiveInput({"approved": True}),
                "conversation_id": session.get_session_id(),
            }, reset_loop=False)
            outer.event_manager.mark_started(work)
            outcome = await asyncio.wait_for(outer.run_one_round(work, uuid4().hex, session), timeout=10)
            outer.event_manager.mark_finished(work)
        assert outcome.error_code is None
        assert outcome.next_work is None
        assert final_results[-1]["stop_reason"] == "hook_blocked"
        model.assert_awaited_once()
        tool.assert_not_awaited()
        assert outer.loop_coordinator.is_aborted
        tool_messages = [call.args[0] for call in context.add_messages.await_args_list
                         if isinstance(call.args[0], ToolMessage)]
        assert [message.tool_call_id for message in tool_messages] == ["Write", "Bash"]
        assert all("policy block" in message.content for message in tool_messages)
        if run_kind == "goal":
            current = await manager.get()
            if user_action == "none":
                assert current.status is GoalStatus.BLOCKED
                assert not outer._should_keep_interaction_open_locked()
            else:
                assert current.status is GoalStatus.ACTIVE
                assert outer._should_keep_interaction_open_locked()
                next_work = outer.event_manager.next_work()
                assert next_work.context["goal_id"] == current.goal_id
            apply_assessment.assert_awaited_once()
            if user_action == "resume":
                # Resume must actually run a new round on the same controller,
                # with no leftover blocking verdict from the previous one.
                model.reset_mock()
                model.side_effect = [
                    AssistantMessage(content="", tool_calls=[ToolCall(
                        id="resumed-call", type="function", name="Bash", arguments="{}",
                    )]),
                    AssistantMessage(content="resumed successfully"),
                ]
                tool.return_value = ("ran", ToolMessage(content="ran", tool_call_id="resumed-call"))
                outer.event_manager.mark_started(next_work)
                await manager.begin_attempt(goal_id=current.goal_id, revision=current.revision)
                outcome = await asyncio.wait_for(outer.run_one_round(next_work, uuid4().hex, session), timeout=10)
                outer.event_manager.mark_finished(next_work)
                assert outcome.error_code is None
                assert final_results[-1] == {"output": "resumed successfully", "result_type": "answer"}
                assert model.await_count == 2
                tool.assert_awaited_once()
                assert not outer.loop_coordinator.is_aborted
        else:
            apply_assessment.assert_not_awaited()
            assert not outer._should_keep_interaction_open_locked()
    finally:
        await outer._force_cleanup_controller()
        await inner.agent_callback_manager.clear()
        await outer.agent_callback_manager.clear()
        await session.post_run()
