"""Real DeepAgent/ReAct routing coverage for Symphony HITL resumes."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    BaseModelClient,
    ModelClientConfig,
    ModelRequestConfig,
    ToolCall,
    UsageMetadata,
)
from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    HeartbeatReason,
    InvokeInputs,
    RunContext,
    ToolCallInputs,
)
from openjiuwen.harness import create_deep_agent
from openjiuwen.harness.rails import AskUserRail

from jiuwenswarm.agents.harness.common.rails.symphony import SymphonyOrchestrationRail
from jiuwenswarm.agents.harness.common.rails.symphony.orchestration_rail import (
    _INVOKE_ROUTE_KEY,
)
from jiuwenswarm.agents.swarm.providers.member_rails import (
    _build_symphony_orchestration_rail,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


class _ScriptedClient(BaseModelClient):
    __client_name__ = "symphony-test"

    def __init__(self, responses: list[AssistantMessage]) -> None:
        super().__init__(
            model_config=ModelRequestConfig(model_name="symphony-test"),
            model_client_config=ModelClientConfig(
                client_provider="OpenAI",
                api_key="test",
                api_base="http://test.invalid",
                verify_ssl=False,
            ),
        )
        self.responses, self.index = responses, 0

    async def invoke(self, *_args: Any, **_kwargs: Any) -> AssistantMessage:
        result = self.responses[self.index]
        self.index += 1
        return result

    async def stream(self, *_args: Any, **_kwargs: Any) -> Any:
        if False:
            yield None

    async def generate_image(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError

    async def generate_speech(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError

    async def generate_video(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError


class _RuntimeModel:
    def __init__(self, client: _ScriptedClient) -> None:
        self.client = client
        self.model_client_config, self.model_config = (
            client.model_client_config,
            client.model_config,
        )

    async def invoke(self, *args: Any, **kwargs: Any) -> Any:
        return await self.client.invoke(*args, **kwargs)


def _call(name: str, args: str, call_id: str) -> AssistantMessage:
    return AssistantMessage(
        content="",
        tool_calls=[ToolCall(id=call_id, type="function", name=name, arguments=args)],
        usage_metadata=UsageMetadata(model_name="symphony-test"),
    )


def _tool(name: str, func: Any) -> LocalFunction:
    properties = {
        "symphony_compose_graph": {
            "query": {"type": "string"},
            "candidate_skill_ids": {"type": "array", "items": {"type": "string"}},
        },
        "skill_tool": {"skill_name": {"type": "string"}},
    }[name]
    return LocalFunction(
        card=ToolCard(
            id=name,
            name=name,
            description=name,
            input_params={"type": "object", "properties": properties},
        ),
        func=func,
    )


def _inputs(query: object, *, mode: str = "agent", owner: str = "owner-1") -> dict:
    key = "team_id" if mode == "team" else "member_id"
    return {
        "query": query,
        "conversation_id": "symphony-hitl",
        "run": {"context": {"extra": {"capture_mode": mode, key: owner}}},
    }


def _outer_ctx(
    query: object,
    *,
    session_id: str = "s-1",
    mode: str = "agent",
    owner: str = "owner-1",
    run_context: RunContext | None = None,
) -> AgentCallbackContext:
    key = "team_id" if mode == "team" else "member_id"
    return AgentCallbackContext(
        agent=SimpleNamespace(card=SimpleNamespace(id="agent-card")),
        inputs=InvokeInputs(
            query=cast(Any, query),
            conversation_id=session_id,
            run_context=run_context
            or RunContext(extra={"capture_mode": mode, key: owner}),
        ),
        session=SimpleNamespace(get_session_id=lambda: session_id),
    )


async def _pause_for_test(
    rail: SymphonyOrchestrationRail,
    ctx: AgentCallbackContext,
    component_id: str,
    *,
    interrupt_ids: bool = False,
    both_ids: bool = False,
    conflict: bool = False,
) -> None:
    await rail.before_invoke(ctx)
    state = rail._outer_states[id(ctx)]
    state.awaiting_input = True
    result: dict[str, Any] = {"result_type": "interrupt"}
    if interrupt_ids:
        result["interrupt_ids"] = [component_id]
    else:
        result["component_ids"] = [component_id]
    if both_ids:
        result["interrupt_ids"] = ["other"] if conflict else [component_id]
    ctx.inputs.result = result
    await rail.after_invoke(ctx)


async def _resumed_state_for_test() -> tuple[
    SymphonyOrchestrationRail, AgentCallbackContext
]:
    rail = SymphonyOrchestrationRail()
    await _pause_for_test(rail, _outer_ctx("task"), "ask")
    answer = InteractiveInput()
    answer.update("ask", {"answers": {"Audience": "engineering"}})
    resumed = _outer_ctx(answer)
    await rail.before_invoke(resumed)
    return rail, resumed


def _inner_tool_ctx(
    outer: AgentCallbackContext,
    *,
    name: str,
    call_id: str,
    args: dict[str, Any] | None = None,
    result: Any = None,
    extra: dict[str, Any] | None = None,
) -> AgentCallbackContext:
    return AgentCallbackContext(
        agent=outer.agent,
        session=outer.session,
        extra=extra if extra is not None else {"run_context": outer.inputs.run_context},
        inputs=ToolCallInputs(
            tool_call=ToolCall(
                id=call_id,
                type="function",
                name=name,
                arguments=json.dumps(args or {}),
            ),
            tool_name=name,
            tool_args=args or {},
            tool_result=result,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["agent", "team"])
async def test_real_deep_agent_routes_needs_input_resume_without_shared_extra(
    mode: str,
) -> None:
    compose_calls: list[dict] = []
    skill_calls: list[dict] = []

    async def compose(**kwargs: Any) -> dict:
        compose_calls.append(kwargs)
        return {
            "planned_graph": {
                "graph": {
                    "metadata": {
                        "status": "needs_input" if len(compose_calls) == 1 else "ready"
                    }
                }
            }
        }

    async def skill(**kwargs: Any) -> dict:
        skill_calls.append(kwargs)
        return {"success": True}

    client = _ScriptedClient(
        [
            _call(
                "symphony_compose_graph",
                '{"query":"task","candidate_skill_ids":["skill-a"]}',
                "compose-1",
            ),
            _call("skill_tool", '{"skill_name":"skill-a"}', "skill-blocked-1"),
            _call(
                "ask_user",
                '{"questions":[{"header":"Audience","question":"Audience"}]}',
                "ask-1",
            ),
            _call("skill_tool", '{"skill_name":"skill-a"}', "skill-blocked-2"),
            _call(
                "symphony_compose_graph",
                '{"query":"drift","candidate_skill_ids":["other"]}',
                "compose-2",
            ),
            _call("skill_tool", '{"skill_name":"skill-a"}', "skill-allowed"),
            AssistantMessage(
                content="done", usage_metadata=UsageMetadata(model_name="symphony-test")
            ),
        ]
    )
    rail = (
        SymphonyOrchestrationRail(config_base={"symphony": {"enabled": True}})
        if mode == "agent"
        else _build_symphony_orchestration_rail(
            {}, type("Leader", (), {"role": "leader"})()
        )
    )
    assert rail is not None
    agent = create_deep_agent(
        model=cast(Any, _RuntimeModel(client)),
        tools=[
            _tool("symphony_compose_graph", compose),
            _tool("skill_tool", skill),
        ],
        rails=[rail, AskUserRail()],
        auto_create_workspace=False,
        enable_task_loop=False,
        max_iterations=8,
    )
    first = await agent.invoke(_inputs("task", mode=mode))
    assert first["result_type"] == "interrupt"
    assert not skill_calls
    answer = InteractiveInput()
    answer.update(
        "ask-1", {"answers": {"Audience": "engineering", "api_key": "secret"}}
    )
    resumed = await agent.invoke(_inputs(answer, mode=mode))
    assert resumed["result_type"] == "answer"
    assert compose_calls[1]["candidate_skill_ids"] == ["skill-a"]
    assert (
        "task" in compose_calls[1]["query"]
        and "engineering" in compose_calls[1]["query"]
    )
    assert "secret" not in compose_calls[1]["query"]
    assert skill_calls == [{"skill_name": "skill-a"}]
    assert client.index == len(client.responses)


@pytest.mark.asyncio
async def test_real_deep_agent_rekeys_two_needs_input_rounds() -> None:
    compose_calls: list[dict[str, Any]] = []
    skill_calls: list[dict[str, Any]] = []

    async def compose(**kwargs: Any) -> dict:
        compose_calls.append(kwargs)
        status = "ready" if len(compose_calls) == 3 else "needs_input"
        return {"planned_graph": {"graph": {"metadata": {"status": status}}}}

    async def skill(**kwargs: Any) -> dict:
        skill_calls.append(kwargs)
        return {"success": True}

    client = _ScriptedClient(
        [
            _call(
                "symphony_compose_graph", '{"candidate_skill_ids":["skill-a"]}', "c1"
            ),
            _call("skill_tool", '{"skill_name":"skill-a"}', "blocked-1"),
            _call(
                "symphony_compose_graph",
                '{"query":"drift","candidate_skill_ids":["drift"]}',
                "compose-blocked-original",
            ),
            _call("ask_user", '{"questions":[{"question":"Audience"}]}', "ask-1"),
            _call("skill_tool", '{"skill_name":"skill-a"}', "blocked-2"),
            _call("symphony_compose_graph", '{"candidate_skill_ids":["drift"]}', "c2"),
            _call("skill_tool", '{"skill_name":"skill-a"}', "blocked-3"),
            _call("ask_user", '{"questions":[{"question":"Scope"}]}', "ask-2"),
            _call("skill_tool", '{"skill_name":"skill-a"}', "blocked-4"),
            _call("symphony_compose_graph", '{"candidate_skill_ids":["drift"]}', "c3"),
            _call("skill_tool", '{"skill_name":"skill-a"}', "allowed"),
            AssistantMessage(
                content="done", usage_metadata=UsageMetadata(model_name="symphony-test")
            ),
        ]
    )
    agent = create_deep_agent(
        model=cast(Any, _RuntimeModel(client)),
        tools=[
            _tool("symphony_compose_graph", compose),
            _tool("skill_tool", skill),
        ],
        rails=[SymphonyOrchestrationRail(), AskUserRail()],
        auto_create_workspace=False,
        enable_task_loop=False,
        max_iterations=12,
    )
    assert (await agent.invoke(_inputs("task")))["result_type"] == "interrupt"
    # The original turn is not allowed to retry compose before the exact
    # AskUser response; its model call is consumed but the tool is not run.
    assert len(compose_calls) == 1
    first_answer = InteractiveInput()
    first_answer.update("ask-1", {"answers": {"Audience": "engineering"}})
    assert (await agent.invoke(_inputs(first_answer)))["result_type"] == "interrupt"
    second_answer = InteractiveInput()
    second_answer.update("ask-2", {"answers": {"Scope": "platform"}})
    assert (await agent.invoke(_inputs(second_answer)))["result_type"] == "answer"

    assert [call["candidate_skill_ids"] for call in compose_calls] == [
        ["skill-a"],
        ["skill-a"],
        ["skill-a"],
    ]
    assert "engineering" in compose_calls[-1]["query"]
    assert "platform" in compose_calls[-1]["query"]
    assert skill_calls == [{"skill_name": "skill-a"}]
    assert client.index == len(client.responses)


@pytest.mark.asyncio
async def test_route_binding_clones_run_context_without_losing_runtime_fields() -> None:
    rail = SymphonyOrchestrationRail()
    nested = {"keep": ["shared-shallow-value"]}
    caller_context = RunContext(
        reason=HeartbeatReason.MANUAL,
        session_id="runtime-session",
        context_mode="background",
        extra={
            "capture_mode": "agent",
            "member_id": "owner-1",
            "existing": "kept",
            "nested": nested,
        },
    )
    outer = _outer_ctx("task", run_context=caller_context)

    await rail.before_invoke(outer)

    bound = outer.inputs.run_context
    assert bound is not caller_context
    assert bound.reason is HeartbeatReason.MANUAL
    assert bound.session_id == "runtime-session"
    assert bound.context_mode == "background"
    assert bound.extra is not caller_context.extra
    assert bound.extra["existing"] == "kept"
    assert bound.extra["nested"] is nested
    assert _INVOKE_ROUTE_KEY in bound.extra
    assert _INVOKE_ROUTE_KEY not in caller_context.extra


@pytest.mark.asyncio
async def test_late_interrupted_invokes_cannot_revive_invalidated_generation() -> None:
    rail = SymphonyOrchestrationRail()
    invokes = [_outer_ctx(f"task-{index}") for index in range(3)]
    await asyncio.gather(*(rail.before_invoke(ctx) for ctx in invokes))
    for index, ctx in enumerate(invokes):
        rail._outer_states[id(ctx)].awaiting_input = True
        ctx.inputs.result = {
            "result_type": "interrupt",
            "component_ids": [f"i-{index}"],
        }
    await asyncio.gather(*(rail.after_invoke(ctx) for ctx in invokes))

    assert not rail._paused_states
    assert not rail._quarantined_scopes
    assert not rail._outer_states


@pytest.mark.asyncio
async def test_new_turn_or_uninit_rejects_late_interrupt_after_callbacks() -> None:
    rail = SymphonyOrchestrationRail()
    old = _outer_ctx("old")
    await rail.before_invoke(old)
    rail._outer_states[id(old)].awaiting_input = True
    new = _outer_ctx("new")
    await rail.before_invoke(new)
    old.inputs.result = {"result_type": "error", "success": False}
    await rail.after_invoke(old)
    assert not rail._paused_states
    assert not rail._active_states

    new.inputs.result = {"result_type": "answer"}
    await rail.after_invoke(new)
    fresh = _outer_ctx("fresh")
    await rail.before_invoke(fresh)
    assert next(iter(rail._active_states.values())) is rail._outer_states[id(fresh)]

    late = _outer_ctx("late")
    await rail.before_invoke(late)
    rail._outer_states[id(late)].awaiting_input = True
    rail.uninit(SimpleNamespace())
    late.inputs.result = {"result_type": "interrupt", "component_ids": ["late-id"]}
    await rail.after_invoke(late)
    assert not rail._paused_states


@pytest.mark.asyncio
async def test_exact_ids_and_valid_ask_user_payload_are_required_to_claim() -> None:
    rail = SymphonyOrchestrationRail()
    await _pause_for_test(rail, _outer_ctx("task"), "same", both_ids=True)
    assert len(rail._paused_states) == 1

    malformed = InteractiveInput()
    malformed.update("same", {"answer": "not the ask_user payload"})
    await rail.before_invoke(_outer_ctx(malformed))
    assert not rail._paused_states

    conflict_rail = SymphonyOrchestrationRail()
    await _pause_for_test(
        conflict_rail, _outer_ctx("task"), "conflict", both_ids=True, conflict=True
    )
    assert not conflict_rail._paused_states
    alias_rail = SymphonyOrchestrationRail()
    await _pause_for_test(alias_rail, _outer_ctx("task"), "alias", interrupt_ids=True)
    assert [key.component_id for key in alias_rail._paused_states] == ["alias"]


@pytest.mark.asyncio
async def test_cross_session_cannot_claim_and_sensitive_only_answer_stays_safe() -> (
    None
):
    rail = SymphonyOrchestrationRail()
    await _pause_for_test(rail, _outer_ctx("task", session_id="s-a"), "ask-a")
    answer = InteractiveInput()
    answer.update("ask-a", {"answers": {"api_key": "leak", "token": "also-leak"}})
    await rail.before_invoke(_outer_ctx(answer, session_id="s-b"))
    assert len(rail._paused_states) == 1

    await rail.before_invoke(_outer_ctx(answer, session_id="s-a"))
    state = next(
        state for state in rail._active_states.values() if state.session_id == "s-a"
    )
    assert state.answered and not state.answers
    assert "leak" not in rail._resume_query(state)
    assert "已回答" in rail._resume_query(state)


@pytest.mark.asyncio
async def test_cancelled_after_invoke_drops_pending_state() -> None:
    rail = SymphonyOrchestrationRail()
    ctx = _outer_ctx("task")
    await rail.before_invoke(ctx)
    rail._outer_states[id(ctx)].awaiting_input = True
    ctx.inputs.result = {"result_type": "error", "success": False}
    await rail.after_invoke(ctx)
    assert not rail._paused_states
    assert not rail._active_states


@pytest.mark.asyncio
async def test_late_inner_after_tool_cannot_mutate_new_outer_invocation() -> None:
    rail = SymphonyOrchestrationRail()
    shared_context = RunContext(extra={"capture_mode": "agent", "member_id": "owner-1"})
    old_outer = _outer_ctx("old", run_context=shared_context)
    await rail.before_invoke(old_outer)
    assert _INVOKE_ROUTE_KEY not in shared_context.extra
    old_inner_context = old_outer.inputs.run_context
    assert old_inner_context is not shared_context
    old_outer.inputs.result = {"result_type": "answer"}
    await rail.after_invoke(old_outer)

    new_outer = _outer_ctx("new", run_context=shared_context)
    await rail.before_invoke(new_outer)
    assert _INVOKE_ROUTE_KEY not in shared_context.extra
    assert new_outer.inputs.run_context is not old_inner_context
    new_state = rail._outer_states[id(new_outer)]
    old_inner = AgentCallbackContext(
        agent=old_outer.agent,
        session=old_outer.session,
        extra={"run_context": old_inner_context},
        inputs=ToolCallInputs(
            tool_name=rail.COMPOSE_TOOL_NAME,
            tool_args={"candidate_skill_ids": ["old-skill"]},
            tool_result={
                "planned_graph": {"graph": {"metadata": {"status": "needs_input"}}}
            },
        ),
    )
    await rail.after_tool_call(old_inner)

    assert not new_state.awaiting_input
    assert new_state.candidate_skill_ids == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answers",
    [
        {},
        {"Audience": {"nested": "not a string"}},
    ],
)
async def test_invalid_ask_user_answers_do_not_claim_paused_state(
    answers: dict[str, Any],
) -> None:
    rail = SymphonyOrchestrationRail()
    await _pause_for_test(rail, _outer_ctx("task"), "ask")
    response = InteractiveInput()
    response.update("ask", {"answers": answers})
    await rail.before_invoke(_outer_ctx(response))
    assert not rail._paused_states
    state = next(iter(rail._active_states.values()))
    assert not state.answered and not state.answers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {},
        {"success": False},
        {"planned_graph": {"graph": {"metadata": {"status": "invalid"}}}},
    ],
)
async def test_resumed_nonready_compose_keeps_skill_gate(result: Any) -> None:
    rail, resumed = await _resumed_state_for_test()
    compose = _inner_tool_ctx(
        resumed,
        name=rail.COMPOSE_TOOL_NAME,
        call_id="compose",
        args={"candidate_skill_ids": ["old-skill"]},
        result=result,
    )
    await rail.after_tool_call(compose)
    skill = _inner_tool_ctx(
        resumed, name="skill_tool", call_id="skill", args={"skill_name": "old-skill"}
    )
    await rail.before_tool_call(skill)

    assert skill.inputs.tool_result["reason"] == "symphony_plan_not_ready"
    assert skill.extra["_skip_tool_calls"] == {"skill": True}
    assert rail._active_state_for_ctx(skill).pending_recompose


@pytest.mark.asyncio
async def test_resumed_compose_exception_keeps_skill_gate() -> None:
    rail, resumed = await _resumed_state_for_test()
    compose = _inner_tool_ctx(
        resumed,
        name=rail.COMPOSE_TOOL_NAME,
        call_id="compose-exception",
        args={"query": "drift", "candidate_skill_ids": ["drift"]},
    )
    await rail.before_tool_call(compose)
    assert isinstance(compose.inputs.tool_call.arguments, dict)
    await rail.on_tool_exception(compose)
    assert isinstance(compose.inputs.tool_call.arguments, str)
    skill = _inner_tool_ctx(resumed, name="skill_tool", call_id="skill-exception")
    await rail.before_tool_call(skill)

    assert skill.inputs.tool_result["reason"] == "symphony_plan_not_ready"
    assert rail._active_state_for_ctx(skill).pending_recompose


@pytest.mark.asyncio
async def test_parallel_skill_gates_are_keyed_by_each_tool_call_id() -> None:
    rail, resumed = await _resumed_state_for_test()
    shared_extra = {"run_context": resumed.inputs.run_context}
    first = _inner_tool_ctx(
        resumed,
        name="skill_tool",
        call_id="skill-one",
        extra=shared_extra,
    )
    second = _inner_tool_ctx(
        resumed,
        name="skill_tool",
        call_id="skill-two",
        extra=shared_extra,
    )
    await asyncio.gather(rail.before_tool_call(first), rail.before_tool_call(second))

    assert shared_extra.get("_skip_tool") is None
    assert shared_extra["_skip_tool_calls"] == {"skill-one": True, "skill-two": True}
    assert first.inputs.tool_result["reason"] == second.inputs.tool_result["reason"]


@pytest.mark.asyncio
async def test_pending_skill_without_provider_id_gets_its_own_skip_key() -> None:
    rail, resumed = await _resumed_state_for_test()
    skill = _inner_tool_ctx(
        resumed,
        name="skill_tool",
        call_id="",
        args={"skill_name": "old-skill"},
    )
    await rail.before_tool_call(skill)

    tool_call_id = skill.inputs.tool_call.id
    assert tool_call_id.startswith("symphony-skip-")
    assert skill.extra["_skip_tool_calls"] == {tool_call_id: True}
    assert skill.inputs.tool_msg.tool_call_id == tool_call_id


@pytest.mark.asyncio
async def test_completed_scope_releases_generation_but_paused_scope_keeps_it() -> None:
    rail = SymphonyOrchestrationRail()
    for session_id in ("completed-1", "completed-2"):
        completed = _outer_ctx("task", session_id=session_id)
        await rail.before_invoke(completed)
        completed.inputs.result = {"result_type": "answer"}
        await rail.after_invoke(completed)

    assert not rail._scope_generations

    paused = _outer_ctx("task", session_id="paused")
    await _pause_for_test(rail, paused, "ask")
    assert len(rail._scope_generations) == 1


def test_agent_and_team_leader_install_the_rail_but_member_does_not() -> None:
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._config_base_cache = {"symphony": {"enabled": True}}
    assert isinstance(
        adapter._build_symphony_orchestration_rail(), SymphonyOrchestrationRail
    )
    assert isinstance(
        _build_symphony_orchestration_rail({}, type("Ctx", (), {"role": "leader"})()),
        SymphonyOrchestrationRail,
    )
    assert (
        _build_symphony_orchestration_rail({}, type("Ctx", (), {"role": "member"})())
        is None
    )
