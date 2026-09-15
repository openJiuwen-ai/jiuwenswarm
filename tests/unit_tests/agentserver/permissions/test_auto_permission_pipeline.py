"""Current end-to-end decision ordering for the root Auto Permission rail."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import MethodType, SimpleNamespace, new_class
from unittest.mock import AsyncMock

import jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.before_tool as before_tool_module
import jiuwenswarm.agents.harness.common.rails.permissions.tool_binding as tool_binding_module
import jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.reviewer_override_consume as override_module
import pytest
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall
from openjiuwen.core.foundation.tool import LocalFunction, ToolCard, ToolExposure
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.agent_callback_manager import AgentCallbackManager
from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.interrupt.handler import (
    ResumeContext,
    ToolInterruptHandler,
)
from openjiuwen.core.single_agent.interrupt.state import (
    INTERRUPT_AUTO_CONFIRM_KEY,
    INTERRUPTION_KEY,
    ToolInterruptEntry,
    ToolInterruptionState,
)
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    AgentCallbackEvent,
    InvokeInputs,
    ToolCallInputs,
)
from openjiuwen.harness.tools.subagent.subagent_tools import build_subagent_tools
from openjiuwen.harness.rails.progressive_tool_rail import ProgressiveToolRail
from openjiuwen.harness.deep_agent import DeepAgent
from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    build_permission_rail,
)
from jiuwenswarm.agents.harness.common.rails.permissions.auto_permission_rail import (
    AutoPermissionInterruptRail,
)
from jiuwenswarm.agents.harness.common.rails.permissions.auto_reviewer import (
    AutoReviewer,
    ReviewerOutcome,
)
from jiuwenswarm.agents.harness.common.rails.permissions.openjiuwen_contract import (
    classify_permission_result,
)
from jiuwenswarm.agents.harness.common.rails.permissions.policy_eval import (
    PolicyEvaluation,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import (
    RootPermissionQueue,
    RootPermissionQueueError,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    RootPermissionQueueRail,
    RootPermissionCompletionRail,
    ROOT_PERMISSION_WRAPPERS_KEY,
    ROOT_PERMISSION_WRAPPER_ATTRIBUTE,
    bind_root_permission_request,
    reset_root_permission_request,
)
from jiuwenswarm.server.runtime.agent_adapter.permission_dispatch import RootPermissionDispatch
from jiuwenswarm.agents.harness.common.rails.permissions.session_deny import (
    SessionDenyStore,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    build_tool_decision_facts,
)
from tests.unit_tests.agentserver.permissions.auto_permission_test_support import (
    FakeBaseRail,
    StaticPolicyEvaluator,
    StaticReviewerClient,
)


def _rail(
    tmp_path,
    evaluation: PolicyEvaluation,
    *,
    session_denies: SessionDenyStore | None = None,
) -> tuple[
    AutoPermissionInterruptRail,
    StaticPolicyEvaluator,
    StaticReviewerClient,
    FakeBaseRail,
]:
    base = FakeBaseRail()
    policy = StaticPolicyEvaluator(evaluation)
    reviewer = StaticReviewerClient(outcome=ReviewerOutcome.ALLOW_ONCE)
    rail = AutoPermissionInterruptRail(
        base_rail=base,
        permission_config={"enabled": True, "mode": "auto"},
        workspace_root=tmp_path,
        policy_evaluator=policy,
        auto_reviewer=AutoReviewer(client=reviewer),
        session_deny_store=session_denies,
    )
    return rail, policy, reviewer, base


async def test_task_tool_ask_is_control_silent_after_engine(tmp_path) -> None:
    rail, policy, reviewer, base = _rail(
        tmp_path,
        PolicyEvaluation(level="ask", reason="default_ask"),
    )

    result = await rail.before_tool_call(
        tool_name="task_tool",
        tool_args={"action": "status"},
        session_id="session-a",
    )

    assert result is None
    assert len(policy.calls) == 1
    assert reviewer.requests == []
    assert base.calls == []


@pytest.mark.parametrize("case", [
    "allow", "reject", "deny", "session_deny", "fail_closed", "forged",
    "changed_args", "undiscovered", "bad_mapping",
])
@pytest.mark.parametrize("target_name", ["probe_target", "cron_list_jobs"])
async def test_real_core_nested_permission_resume(tmp_path, case, target_name):
    """Real Core dispatch and AutoPermission; only model/base policy are doubles."""
    data, executed, callbacks = {}, [], []
    session = SimpleNamespace(
        get_session_id=lambda: "session-a", get_state=data.get, update_state=data.update,
    )
    queue = RootPermissionQueue()
    queue_rail, completion = RootPermissionQueueRail(queue), RootPermissionCompletionRail(queue)
    auto, policy, reviewer, _base = _rail(
        tmp_path, PolicyEvaluation(level="ask", reason="default_ask"),
    )
    reviewer.outcome = ReviewerOutcome.MANUAL
    phase = "initial"

    async def callback(ctx):
        event = ctx.event
        if event == AgentCallbackEvent.ON_TOOL_EXCEPTION:
            await queue_rail.on_tool_exception(ctx)
        if event != AgentCallbackEvent.BEFORE_TOOL_CALL:
            return
        await queue_rail.before_tool_call(ctx)
        callbacks.append((phase, ctx.inputs.tool_call.id))
        if phase != "initial" or ctx.inputs.tool_name != "tool_call":
            await auto.before_tool_call(ctx)
            assert not hasattr(ctx, ROOT_PERMISSION_WRAPPER_ATTRIBUTE)
        await completion.before_tool_call(ctx)

    manager = AbilityManager(owner_id="nested-permission")
    callback_manager = AgentCallbackManager("nested-permission")
    agent = SimpleNamespace(
        card=SimpleNamespace(id="nested-permission"), ability_manager=manager,
        agent_callback_manager=callback_manager,
    )
    for event in (AgentCallbackEvent.BEFORE_TOOL_CALL, AgentCallbackEvent.ON_TOOL_EXCEPTION):
        await callback_manager.register_callback(event, callback)
    progressive = ProgressiveToolRail(SimpleNamespace(language="en"))
    progressive.init(agent)
    target = LocalFunction(
        card=ToolCard(
            id="nested-target", name=target_name, description="In-memory target",
            input_params={"type": "object", "properties": {"value": {"type": "integer"}}},
            exposure=ToolExposure.DEFERRED,
        ),
        func=lambda value: executed.append(value) or "done",
    )
    manager.add_ability(target.card, target)
    discovered = await progressive._search_tools(target_name, session=session)
    assert any(tool["name"] == target_name for tool in discovered)
    calls = [
        ToolCall(
            id=f"outer-{i}", type="function", name="tool_call",
            arguments=json.dumps({"name": target_name, "args": {"value": i}}),
        ) for i in range(2)
    ]
    ctx = AgentCallbackContext(agent=agent, session=session, extra={})
    handler = ToolInterruptHandler(agent)

    def snapshot(state):
        entries = [
            {"id": inner, "value": request} for entry in state.interrupted_tools.values()
            for inner, request in entry.interrupt_requests.items()
        ]
        return {"result_type": "interrupt", "interrupt_ids": [e["id"] for e in entries], "state": entries}

    async def execute(context, tool_calls, sdk_session, _context):
        return await manager.execute(context, tool_calls, sdk_session, parallel_tool_calls=False)

    token = bind_root_permission_request(
        root_session_id="session-a", request_id="initial", enabled=True, queue=queue,
    )
    try:
        results = await execute(ctx, calls, session, None)
        assert all(isinstance(result, ToolInterruptException) for result, _ in results)
        state, _ = handler.build_interrupt_state(results, calls, AssistantMessage(tool_calls=calls), 0)
        data[INTERRUPTION_KEY] = state
        cards = queue.reconcile(snapshot(state), root_session_id="session-a").cards
        assert len(cards) == 2 and executed == []
        answer = InteractiveInput()
        answer.update(cards[0].key.invocation_id, {
            "approved": case != "reject", "auto_confirm": False, "feedback": "",
        })
        dispatch = RootPermissionDispatch(queue)
        if case == "bad_mapping":
            requests = state.interrupted_tools["outer-0"].interrupt_requests
            requests["not-the-inner"] = requests.pop("outer-0:target")
            with pytest.raises(RootPermissionQueueError, match="identity_mismatch"):
                dispatch.prepare_resume({"query": answer}, root_session_id="session-a", loop_session=session)
            assert queue.get(cards[0].key).state == "pending" and executed == []
            return
        prepared = dispatch.prepare_resume(
            {"query": answer}, root_session_id="session-a", loop_session=session,
        )
        ctx.extra["run_context"] = DeepAgent._normalize_inputs(None, deepcopy(prepared)).run_context
        if case == "deny":
            policy.result = PolicyEvaluation(level="deny", reason="explicit_deny")
        elif case == "session_deny":
            auto.session_deny_store.record_denial(
                session_id="session-a", tool_name="tool_call",
                tool_args=json.loads(calls[0].arguments), reason="user_rejected",
            )
        elif case == "fail_closed":
            policy.result = PolicyEvaluation(level="ask", reason="unevaluable", source="fail_closed")
        elif case == "forged":
            manager.remove_ability("tool_call")
            impostor = LocalFunction(
                card=ToolCard(id="impostor", name="tool_call", description="Not the SDK wrapper"),
                func=lambda **_: executed.append("impostor"),
            )
            manager.add_ability(impostor.card, impostor)
        elif case == "changed_args":
            # Mutate the replayed copy, not the Core continuation that authorized it.
            execute_original = execute

            async def execute(context, tool_calls, sdk_session, _context):
                tool_calls[0].arguments = json.dumps({"name": target_name, "args": {"value": 99}})
                return await execute_original(context, tool_calls, sdk_session, _context)
        elif case == "undiscovered":
            data["__progressive_discovered_tool_names__"] = []
        phase = "resume"
        handler.commit_interrupt = AsyncMock(side_effect=lambda state, *_: snapshot(state))
        resumed = await handler.handle_resume(ResumeContext(
            state=state, user_input=prepared["query"], ctx=ctx, context=SimpleNamespace(),
            session=session, execute_tool_call=execute,
        ))
        assert executed == ([0] if case == "allow" else [])
        markers = prepared["run"]["context"]["extra"][ROOT_PERMISSION_WRAPPERS_KEY]
        assert all(marker.consumed for marker in markers)
        if case in {"allow", "reject"}:
            remaining = queue.reconcile(resumed, root_session_id="session-a").cards
            assert len(remaining) == 1 and remaining[0].key == cards[1].key
            assert callbacks.count(("resume", "outer-0:target")) == 1
            next_answer = InteractiveInput()
            next_answer.update(cards[1].key.invocation_id, {
                "approved": True, "auto_confirm": False, "feedback": "",
            })
            next_prepared = dispatch.prepare_resume(
                {"query": next_answer}, root_session_id="session-a", loop_session=session,
            )
            ctx.extra["run_context"] = DeepAgent._normalize_inputs(None, deepcopy(next_prepared)).run_context
            final = await handler.handle_resume(ResumeContext(
                state=state, user_input=next_prepared["query"], ctx=ctx,
                context=SimpleNamespace(), session=session, execute_tool_call=execute,
            ))
            assert final is None and not queue.has_live(root_session_id="session-a")
            assert executed == ([0, 1] if case == "allow" else [1])
            if target_name == "cron_list_jobs":
                assert reviewer.requests == []
            # The same run context cannot authorize a second outer re-entry.
            again = await execute(ctx, calls[1], session, None)
            assert isinstance(again[0][0], ToolInterruptException)
            assert executed == ([0, 1] if case == "allow" else [1])
        else:
            assert ("resume", "outer-0:target") not in callbacks
    finally:
        reset_root_permission_request(token)
        await callback_manager.clear()
        progressive.uninit(agent)
        for name in (target_name, "tool_call"):
            if manager.get(name) is not None:
                manager.remove_ability(name)


async def test_task_tool_alias_does_not_use_control_silent_path(
    tmp_path,
    monkeypatch,
) -> None:
    import jiuwenswarm.common.permission_tools as permission_tools

    monkeypatch.setitem(
        permission_tools.PERMISSION_TOOL_ALIASES,
        "task_tool_alias",
        "task_tool",
    )
    rail, policy, reviewer, base = _rail(
        tmp_path,
        PolicyEvaluation(level="ask", reason="default_ask"),
    )

    result = await rail.before_tool_call(
        tool_name="task_tool_alias",
        tool_args={"action": "status"},
        session_id="session-a",
    )

    assert classify_permission_result(result) == "allow"
    assert len(policy.calls) == 1
    assert len(reviewer.requests) == 1
    assert base.calls == []


@pytest.mark.parametrize(
    ("tool_name", "tool_args"),
    [
        (
            "subagent_spawn",
            {"agent_name": "general-purpose", "task_description": "inspect this task"},
        ),
        ("subagent_wait", {"subagent_ids": ["sub-a", "sub-b"]}),
        ("subagent_list", {}),
        ("subagent_send_input", {"subagent_id": "sub-a", "query": "continue"}),
        ("subagent_close", {"subagent_id": "sub-a"}),
        ("subagent_resume", {"subagent_id": "sub-a"}),
    ],
)
async def test_subagent_runtime_control_ask_uses_internal_fast_path(
    tmp_path,
    monkeypatch,
    tool_name: str,
    tool_args: dict[str, object],
) -> None:
    rail, policy, reviewer, base = _rail(
        tmp_path,
        PolicyEvaluation(level="ask", reason="default_ask"),
    )
    ctx, _resource = _runtime_control_ctx(monkeypatch, tool_name, tool_args)

    result = await rail.before_tool_call(ctx)

    assert result is None
    assert len(policy.calls) == 1
    assert reviewer.requests == []
    assert base.calls == []


async def test_subagent_runtime_same_id_shadow_does_not_use_internal_fast_path(
    tmp_path,
    monkeypatch,
) -> None:
    rail, policy, reviewer, base = _rail(
        tmp_path,
        PolicyEvaluation(level="ask", reason="default_ask"),
    )
    ctx, resource = _runtime_control_ctx(
        monkeypatch,
        "subagent_list",
        {},
    )
    shadow = SimpleNamespace(
        card=resource.card,
        _parent_agent=resource._parent_agent,
    )
    monkeypatch.setattr(
        tool_binding_module,
        "Runner",
        SimpleNamespace(
            resource_mgr=SimpleNamespace(get_tool=lambda *_args, **_kwargs: shadow)
        ),
    )

    with pytest.raises(AbortError):
        await rail.before_tool_call(ctx)

    assert len(policy.calls) == 1
    assert reviewer.requests == []
    assert base.calls == []


async def test_subagent_runtime_subclass_shadow_does_not_use_internal_fast_path(
    tmp_path,
    monkeypatch,
) -> None:
    rail, policy, reviewer, base = _rail(
        tmp_path,
        PolicyEvaluation(level="ask", reason="default_ask"),
    )
    ctx, resource = _runtime_control_ctx(monkeypatch, "subagent_list", {})
    shadow_type = new_class("ShadowSubagentListTool", (resource.__class__,))
    shadow = object.__new__(shadow_type)
    shadow.__dict__.update(resource.__dict__)
    monkeypatch.setattr(
        tool_binding_module,
        "Runner",
        SimpleNamespace(
            resource_mgr=SimpleNamespace(get_tool=lambda *_args, **_kwargs: shadow)
        ),
    )

    with pytest.raises(AbortError):
        await rail.before_tool_call(ctx)

    assert len(policy.calls) == 1
    assert reviewer.requests == []
    assert base.calls == []


@pytest.mark.parametrize("binding_failure", ["card_identity", "agent_bridge"])
async def test_subagent_runtime_unverified_sdk_binding_requires_manual_approval(
    tmp_path,
    monkeypatch,
    binding_failure: str,
) -> None:
    rail, policy, reviewer, base = _rail(
        tmp_path,
        PolicyEvaluation(level="ask", reason="default_ask"),
    )
    ctx, resource = _runtime_control_ctx(monkeypatch, "subagent_list", {})
    if binding_failure == "card_identity":
        resource._card = SimpleNamespace(
            name="subagent_list",
            id="subagent_list_root",
        )
    else:
        resource._parent_agent.react_agent = SimpleNamespace()

    with pytest.raises(AbortError):
        await rail.before_tool_call(ctx)

    assert len(policy.calls) == 1
    assert reviewer.requests == []
    assert base.calls == []


async def test_subagent_runtime_missing_callback_context_requires_manual_approval(
    tmp_path,
) -> None:
    rail, policy, reviewer, base = _rail(
        tmp_path,
        PolicyEvaluation(level="ask", reason="default_ask"),
    )

    result = await rail.before_tool_call(
        tool_name="subagent_list",
        tool_args={},
        session_id="session-a",
    )

    assert classify_permission_result(result) == "interrupt"
    assert len(policy.calls) == 1
    assert reviewer.requests == []
    assert base.calls == []


async def test_subagent_runtime_manual_approval_resumes_unverified_binding(
    tmp_path,
    monkeypatch,
) -> None:
    rail, policy, reviewer, base = _rail(
        tmp_path,
        PolicyEvaluation(level="ask", reason="default_ask"),
    )
    ctx, resource = _runtime_control_ctx(monkeypatch, "subagent_list", {})
    resource._parent_agent.react_agent = SimpleNamespace()

    result = await rail.before_tool_call(
        ctx,
        user_input={"approved": True, "auto_confirm": False},
    )

    assert result is None
    assert len(policy.calls) == 1
    assert reviewer.requests == []
    assert base.calls == []


@pytest.mark.parametrize(
    ("tool_name", "tool_args"),
    [
        (
            "subagent_spawn",
            {"agent_name": "general-purpose", "task_description": "inspect this task"},
        ),
        ("task_tool", {"action": "status"}),
    ],
)
async def test_real_engine_deny_precedes_internal_fast_paths(
    tmp_path, tool_name: str, tool_args: dict[str, object]
) -> None:
    permissions = {
        "enabled": True,
        "mode": "auto",
        "tools": {tool_name: "deny"},
    }
    rail = AutoPermissionInterruptRail(
        base_rail=build_permission_rail({"permissions": permissions}),
        permission_config=permissions,
        workspace_root=tmp_path,
    )

    result = await rail.before_tool_call(
        tool_name=tool_name,
        tool_args=tool_args,
        session_id="session-a",
    )

    assert classify_permission_result(result) == "denied"


async def test_engine_fail_closed_has_zero_reviewer_or_base_effect(tmp_path) -> None:
    rail, policy, reviewer, base = _rail(
        tmp_path,
        PolicyEvaluation(
            level="ask",
            reason="permission_engine_failed",
            source="fail_closed",
        ),
    )

    result = await rail.before_tool_call(
        tool_name="read_file",
        tool_args={"path": str(tmp_path / "README.md")},
        session_id="session-a",
    )

    assert classify_permission_result(result) == "interrupt"
    assert len(policy.calls) == 1
    assert reviewer.requests == []
    assert base.calls == []


async def test_admitted_manual_rejection_skips_policy_re_evaluation(
    tmp_path, monkeypatch
) -> None:
    rail, policy, reviewer, base = _rail(
        tmp_path,
        PolicyEvaluation(
            level="ask",
            reason="permission_engine_failed",
            source="fail_closed",
        ),
    )
    monkeypatch.setattr(
        before_tool_module,
        "root_permission_resume_from_context",
        lambda _ctx: SimpleNamespace(
            card=SimpleNamespace(key=SimpleNamespace(request_id="original-request"))
        ),
    )

    result = await rail.before_tool_call(
        tool_name="mcp_free_search",
        tool_args={"query": "blocked"},
        session_id="session-a",
        user_input={
            "approved": False,
            "auto_confirm": False,
            "feedback": "用户拒绝",
        },
    )

    assert classify_permission_result(result) == "user_rejection"
    assert policy.calls == []
    assert reviewer.requests == []
    assert base.calls == []


async def test_exact_session_deny_precedes_engine_allow_and_reviewer(tmp_path) -> None:
    denies = SessionDenyStore()
    denies.record_denial(
        session_id="session-a",
        tool_name="write_file",
        tool_args={"path": "report.md", "content": "draft"},
        reason="user_rejected",
    )
    rail, policy, reviewer, base = _rail(
        tmp_path,
        PolicyEvaluation(level="allow", reason="engine_allow"),
        session_denies=denies,
    )

    result = await rail.before_tool_call(
        tool_name="write_file",
        tool_args={"path": "report.md", "content": "draft"},
        session_id="session-a",
    )

    assert classify_permission_result(result) == "denied"
    assert len(policy.calls) == 1
    assert reviewer.requests == []
    assert base.calls == []


async def test_generic_mcp_keeps_manual_ceiling(tmp_path) -> None:
    rail, policy, reviewer, base = _rail(
        tmp_path,
        PolicyEvaluation(level="allow", reason="engine_allow"),
    )

    result = await rail.before_tool_call(
        tool_name="mcp_docs_lookup",
        tool_args={"query": "asyncio"},
        session_id="session-a",
    )

    assert classify_permission_result(result) == "interrupt"
    assert len(policy.calls) == 1
    assert reviewer.requests == []
    assert base.calls == []


class _Session:
    def __init__(self) -> None:
        self.session_id = "session-a"
        self.state: dict[str, object] = {}

    def get_state(self, key: str) -> object | None:
        return self.state.get(key)

    def update_state(self, values: dict[str, object]) -> None:
        self.state.update(values)


def _runtime_ctx(
    session: _Session,
    *,
    tool_name: str,
    tool_args: dict[str, object],
) -> AgentCallbackContext:
    tool_call = SimpleNamespace(id="call-1", name=tool_name, arguments=tool_args)
    return AgentCallbackContext(
        # Routing doubles have no installed native executors. Their contracts
        # are covered separately with a real AbilityManager and filesystem.
        agent=SimpleNamespace(ability_manager=SimpleNamespace(get=lambda _name: None)),
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name=tool_name,
            tool_args=tool_args,
        ),
        session=session,
        extra={},
    )


def _runtime_control_ctx(
    monkeypatch,
    tool_name: str,
    tool_args: dict[str, object],
) -> tuple[AgentCallbackContext, object]:
    session = _Session()
    ctx = _runtime_ctx(session, tool_name=tool_name, tool_args=tool_args)
    callback_agent = ctx.agent
    outer_agent = SimpleNamespace()
    resource = next(
        tool
        for tool in build_subagent_tools(outer_agent)
        if tool.card.name == tool_name
    )
    card = resource.card
    manager = SimpleNamespace(get=lambda name: card if name == tool_name else None)
    callback_agent.ability_manager = manager
    outer_agent.react_agent = callback_agent
    outer_agent.ability_manager = manager

    def get_raw_tool(tool_id, *, session):
        assert tool_id == card.id
        assert session is None
        return resource

    monkeypatch.setattr(
        tool_binding_module,
        "Runner",
        SimpleNamespace(resource_mgr=SimpleNamespace(get_tool=get_raw_tool)),
    )
    return ctx, resource


def test_subagent_runtime_binding_rejects_card_identity_mismatch(monkeypatch) -> None:
    ctx, resource = _runtime_control_ctx(monkeypatch, "subagent_list", {})
    resource._card = SimpleNamespace(name="subagent_list", id="subagent_list_root")
    invocation = before_tool_module._extract_invocation((ctx,), {})

    assert (
        before_tool_module._trusted_subagent_runtime_control_binding(invocation)
        is False
    )


def test_subagent_runtime_binding_rejects_wrong_inner_outer_bridge(
    monkeypatch,
) -> None:
    ctx, resource = _runtime_control_ctx(monkeypatch, "subagent_list", {})
    resource._parent_agent.react_agent = SimpleNamespace()
    invocation = before_tool_module._extract_invocation((ctx,), {})

    assert (
        before_tool_module._trusted_subagent_runtime_control_binding(invocation)
        is False
    )


def test_subagent_runtime_binding_rejects_missing_callback_context() -> None:
    invocation = SimpleNamespace(ctx=None, tool_name="subagent_list")

    assert (
        before_tool_module._trusted_subagent_runtime_control_binding(invocation)
        is False
    )


_READONLY_CASES = [
    ("cron_list_jobs", {}), ("cron_get_job", {"job_id": "job-a"}),
    ("cron_preview_job", {"job_id": "job-a"}), ("heartbeat_list_jobs", {"scope": "current"}),
    ("heartbeat_get_job", {"job_id": "job-a"}), ("heartbeat_preview_job", {"job_id": "job-a"}),
    ("read_terminal_output", {"terminal_id": "terminal-a"}),
    ("wait_for_terminal_exit", {"terminal_id": "terminal-a"}),
    ("convert_timestamp_to_utc8_time", {"timestamp": 1700000000}),
]


def _readonly_resource(name, tmp_path, monkeypatch, session_id="session-a", *, no_create=False):
    from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission import readonly_tool_bindings as bindings
    from jiuwenswarm.agents.harness.common.tools.cron.cron_runtime import CronRuntimeBridge
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.cron.cron_tools.get_cron_jobs_path",
        lambda: tmp_path / "cron.json",
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.code.rails.heartbeat.runtime.get_heartbeat_jobs_path",
        lambda: tmp_path / "heartbeat.json",
    )
    context = SimpleNamespace(session_id=session_id, channel_id="web", user_id="", metadata={}, tool_scope="readonly")
    if name.startswith("cron_"):
        bridge = CronRuntimeBridge()
        bridge.set_backend(bindings._CronToolsCronBackend(bindings.CronTools()))
        tools = bridge.build_tools(context=context, agent_id="readonly", allow_create=not no_create)
    elif name.startswith("heartbeat_"):
        tools = bindings.HeartbeatRuntimeBridge(bindings.HeartbeatRailRuntime(SimpleNamespace())).build_tools(context=context)
    elif name == "convert_timestamp_to_utc8_time":
        tools = [bindings.convert_timestamp_to_utc8_time]
    else:
        tools = bindings.acp.get_tools(session_id=session_id)
    return next(tool for tool in tools if tool.card.name == name)


@pytest.mark.parametrize(("name", "args"), _READONLY_CASES)
@pytest.mark.parametrize("variant", ["valid", "shadow", "callable", "cross_session", "deny", "session_deny", "fail_closed"])
async def test_readonly_real_binding_fast_path(tmp_path, monkeypatch, name, args, variant):
    from openjiuwen.core.runner.resources_manager.resource_manager import ResourceMgr

    # Stateless registration keeps an existing singleton; this case must own the actual binding.
    monkeypatch.setattr(tool_binding_module.Runner, "resource_mgr", ResourceMgr())
    tool = _readonly_resource(name, tmp_path, monkeypatch, "another-session" if variant == "cross_session" else "session-a")
    # SDK registration may rewrite the shared card. Restore both fields after this case.
    monkeypatch.setattr(tool.card, "id", tool.card.id)
    monkeypatch.setattr(tool.card, "stateless", name == "convert_timestamp_to_utc8_time")
    if variant == "shadow":
        tool = LocalFunction(card=tool.card, func=lambda **kwargs: "not the builtin")
    if variant == "callable":
        monkeypatch.setattr(tool, "_func", lambda **kwargs: "replaced")
    manager = AbilityManager(owner_id="readonly-permission")
    manager.add_ability(tool.card, tool)
    assert tool_binding_module.Runner.resource_mgr.get_tool(tool.card.id, session=None) is tool
    denies = SessionDenyStore()
    if variant == "session_deny":
        denies.record_denial(session_id="session-a", tool_name=name, tool_args=args, reason="user_rejected")
    try:
        for level in ("ask", "allow"):
            policy_result = PolicyEvaluation(
                level="deny" if variant == "deny" else level,
                reason="configured", source="fail_closed" if variant == "fail_closed" else "engine",
            )
            rail, policy, reviewer, _base = _rail(tmp_path, policy_result, session_denies=denies)
            ctx = _runtime_ctx(_Session(), tool_name=name, tool_args=args)
            ctx.agent.ability_manager = manager
            expected = "allow" if variant == "valid" or (variant == "cross_session" and name.startswith("convert_")) else "interrupt"
            if variant in {"deny", "session_deny"}:
                expected = "denied"
            try:
                result = await rail.before_tool_call(ctx)
            except AbortError:
                assert expected == "interrupt"
            else:
                assert classify_permission_result(result) == expected
            assert len(policy.calls) == 1 and not reviewer.requests
    finally:
        manager.remove(name)


@pytest.mark.parametrize(("name", "args"), _READONLY_CASES)
@pytest.mark.parametrize("defect", ["owner", "missing", "card", "lookup", "code", "invoke", "invoke_proxy"])
def test_readonly_binding_rejects_unverified_dependencies(tmp_path, monkeypatch, name, args, defect):
    from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission import readonly_tool_bindings as bindings
    tool = _readonly_resource(name, tmp_path, monkeypatch, no_create=name.startswith("cron_"))
    ctx = _runtime_ctx(_Session(), tool_name=name, tool_args=args)
    card = tool.card
    ctx.agent.ability_manager = SimpleNamespace(get=lambda _: card)
    monkeypatch.setattr(tool_binding_module.Runner.resource_mgr, "get_tool", lambda *a, **k: tool)
    invocation = before_tool_module._extract_invocation((ctx,), {})
    assert bindings.trusted_readonly_binding(invocation, "session-a")
    if defect == "owner":
        captured = dict(zip(tool._func.__code__.co_freevars, tool._func.__closure__ or ()))
        if name.startswith("cron_"):
            captured["backend"].cell_contents._inner = object()
        elif name.startswith("heartbeat_"):
            captured["self"].cell_contents._service = object()
        elif name in bindings._ACP_DELEGATES:
            monkeypatch.setattr(bindings.acp, name, lambda **kwargs: "replaced delegate")
        else:
            monkeypatch.setattr(tool, "_func", lambda **kwargs: "replaced")
    elif defect == "missing":
        ctx.agent.ability_manager = None
    elif defect == "card":
        monkeypatch.setattr(tool, "_card", deepcopy(card))
    elif defect == "lookup":
        monkeypatch.setattr(tool_binding_module.Runner.resource_mgr, "get_tool", lambda *a, **k: 1 / 0)
    elif defect == "code":
        monkeypatch.setattr(tool, "_func", lambda **kwargs: None)
    elif defect == "invoke_proxy":
        original = tool.invoke

        class InvokeProxy:
            @property
            def __class__(self):
                return original.__class__

            def __getattr__(self, key):
                return getattr(original, key)

            async def __call__(self, *args, **kwargs):
                raise AssertionError("a proxy must never receive the builtin fast path")

        proxy = InvokeProxy()
        assert isinstance(proxy, original.__class__)
        monkeypatch.setattr(tool, "invoke", proxy)
    else:
        monkeypatch.setattr(tool, "invoke", AsyncMock())
    assert not bindings.trusted_readonly_binding(invocation, "session-a")


@pytest.mark.parametrize("defect", [
    "cron_method", "cron_route", "cron_tools", "heartbeat_bridge", "heartbeat_service", "cron_wrapper",
])
@pytest.mark.parametrize("level", ["ask", "allow"])
async def test_readonly_rejects_rebound_methods(tmp_path, monkeypatch, defect, level):
    from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission import readonly_tool_bindings as bindings
    name = "heartbeat_list_jobs" if defect.startswith("heartbeat_") else "cron_list_jobs"
    tool = _readonly_resource(name, tmp_path, monkeypatch, no_create=defect == "cron_wrapper")
    other = _readonly_resource(name, tmp_path, monkeypatch, session_id="another-session")
    manager = AbilityManager(owner_id="readonly-method-binding")
    manager.add_ability(tool.card, tool)
    ctx = _runtime_ctx(_Session(), tool_name=name, tool_args={})
    ctx.agent.ability_manager = manager
    invocation = before_tool_module._extract_invocation((ctx,), {})
    rail, _policy, reviewer, _base = _rail(tmp_path, PolicyEvaluation(level=level, reason="configured"))

    def closure(resource):
        return dict(zip(resource._func.__code__.co_freevars, (c.cell_contents for c in resource._func.__closure__)))

    try:
        assert bindings.trusted_readonly_binding(invocation, "session-a")
        assert await rail.before_tool_call(ctx) is None
        current, foreign = closure(tool), closure(other)
        if defect.startswith("cron_"):
            owner, other_owner = current["backend"], foreign["backend"]
            if defect == "cron_wrapper":
                async def replacement(self, **kwargs):
                    raise AssertionError("unverified delegate must not execute")
                monkeypatch.setattr(owner, "list_jobs", MethodType(replacement, owner._inner))
            else:
                method = "_with_route" if defect == "cron_route" else "list_jobs"
                if defect == "cron_tools":
                    owner, other_owner = owner._cron_tools, other_owner._cron_tools
                monkeypatch.setattr(owner, method, getattr(other_owner, method))
        else:
            owner, other_owner = current["self"], foreign["self"]
            method = "_send"
            if defect == "heartbeat_service":
                owner, other_owner = owner._service, other_owner._service
                method = "handle_operation"
            monkeypatch.setattr(owner, method, getattr(other_owner, method))
        # Exercise the actual rail, not only a helper-level rejection.
        with pytest.raises(AbortError):
            await rail.before_tool_call(ctx)
        assert not bindings.trusted_readonly_binding(invocation, "session-a")
        assert not reviewer.requests
    finally:
        manager.remove(name)


@pytest.mark.parametrize("case", [
    "valid", "other_owner", "other_func", "unbound", "missing", "none",
    "empty_owner", "empty_func", "empty_both",
])
def test_bound_method_requires_both_expected_identities(case):
    class Owner:
        def first(self):
            pass

        def second(self):
            pass

    owner = Owner()
    methods = {
        "other_owner": Owner().first, "other_func": owner.second,
        "unbound": Owner.first, "missing": object(), "none": None, "empty_both": None,
    }
    assert tool_binding_module.matches_bound_method(
        methods.get(case, owner.first),
        expected_owner=None if case in {"empty_owner", "empty_both"} else owner,
        expected_func=None if case in {"empty_func", "empty_both"} else Owner.first,
    ) is (case == "valid")


def _install_core_auto_confirm_contract(base: FakeBaseRail) -> None:
    base._get_auto_confirm_key = lambda tool_call: tool_call.name
    base._is_auto_confirmed = lambda config, key: bool(
        isinstance(config, dict) and config.get(key) is True
    )

    def store(ctx, key):
        config = ctx.session.get_state(INTERRUPT_AUTO_CONFIRM_KEY) or {}
        ctx.session.update_state(
            {INTERRUPT_AUTO_CONFIRM_KEY: {**dict(config), key: True}}
        )

    base._store_auto_confirm = store


async def test_reviewer_cancellation_finishes_only_active_permission_card(
    tmp_path,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingReviewerClient:
        async def assess(self, request: object) -> str:
            del request
            started.set()
            await release.wait()
            raise AssertionError("cancelled reviewer must not complete")

    queue = RootPermissionQueue(id_factory=lambda: "invocation-1")
    queue_rail = RootPermissionQueueRail(queue)
    base = FakeBaseRail()
    rail = AutoPermissionInterruptRail(
        base_rail=base,
        permission_config={"enabled": True, "mode": "auto"},
        workspace_root=tmp_path,
        policy_evaluator=StaticPolicyEvaluator(
            PolicyEvaluation(level="ask", reason="default_ask")
        ),
        auto_reviewer=AutoReviewer(client=BlockingReviewerClient()),
    )
    session = _Session()
    ctx = _runtime_ctx(
        session,
        tool_name="read_file",
        tool_args={"path": str(tmp_path / "README.md")},
    )
    token = bind_root_permission_request(
        root_session_id="session-a",
        request_id="request-a",
        enabled=True,
        queue=queue,
    )
    try:
        await queue_rail.before_tool_call(ctx)
        task = asyncio.create_task(rail.before_tool_call(ctx))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        reset_root_permission_request(token)

    assert queue.has_live(root_session_id="session-a") is False
    assert queue.begin_cutover(root_session_id="session-a") is False


async def test_auto_manual_session_choice_reuses_core_state_before_reviewer(
    tmp_path, monkeypatch
) -> None:
    rail, policy, reviewer, base = _rail(
        tmp_path,
        PolicyEvaluation(level="ask", reason="default_ask"),
    )
    _install_core_auto_confirm_contract(base)
    session = _Session()
    args = {"file_path": str(tmp_path / "report.md")}
    ctx = _runtime_ctx(session, tool_name="edit_file", tool_args=args)
    monkeypatch.setattr(
        override_module,
        "root_permission_resume_from_context",
        lambda _ctx: SimpleNamespace(card=SimpleNamespace(auto_manual=True)),
    )
    invocation = before_tool_module._extract_invocation((ctx,), {})
    facts = build_tool_decision_facts(
        "edit_file",
        args,
        workspace_root=tmp_path,
        original_args_were_valid_object=True,
    )

    first = await rail._consume_reviewer_override(
        facts,
        invocation=invocation,
        user_input={"approved": True, "auto_confirm": True, "feedback": ""},
        domain_route=None,
    )

    assert first.handled is True
    assert session.get_state(INTERRUPT_AUTO_CONFIRM_KEY) == {"edit_file": True}

    monkeypatch.setattr(
        before_tool_module,
        "root_permission_resume_from_context",
        lambda _ctx: None,
    )
    result = await rail.before_tool_call(ctx)

    assert result is None
    assert len(policy.calls) == 1
    assert reviewer.requests == []


async def test_session_auto_confirm_cannot_bypass_unknown_manual_ceiling(
    tmp_path,
) -> None:
    rail, policy, reviewer, base = _rail(
        tmp_path,
        PolicyEvaluation(level="ask", reason="default_ask"),
    )
    _install_core_auto_confirm_contract(base)
    session = _Session()
    session.update_state({INTERRUPT_AUTO_CONFIRM_KEY: {"mcp_docs_lookup": True}})
    ctx = _runtime_ctx(
        session,
        tool_name="mcp_docs_lookup",
        tool_args={"query": "asyncio"},
    )

    with pytest.raises(AbortError):
        await rail.before_tool_call(ctx)

    assert len(policy.calls) == 1
    assert reviewer.requests == []


async def test_resource_scoped_external_send_never_uses_tool_key_remember(
    tmp_path,
    monkeypatch,
) -> None:
    rail, _policy, _reviewer, base = _rail(
        tmp_path,
        PolicyEvaluation(level="ask", reason="default_ask"),
    )
    _install_core_auto_confirm_contract(base)
    session = _Session()
    args = {"file_path": str(tmp_path / "report.md")}
    ctx = _runtime_ctx(session, tool_name="upload_file", tool_args=args)
    monkeypatch.setattr(
        override_module,
        "root_permission_resume_from_context",
        lambda _ctx: SimpleNamespace(card=SimpleNamespace(auto_manual=True)),
    )
    invocation = before_tool_module._extract_invocation((ctx,), {})
    facts = build_tool_decision_facts(
        "upload_file",
        args,
        workspace_root=tmp_path,
        original_args_were_valid_object=True,
    )

    result = await rail._consume_reviewer_override(
        facts,
        invocation=invocation,
        user_input={"approved": True, "auto_confirm": True, "feedback": ""},
        domain_route=None,
    )

    assert result.handled is True
    assert session.get_state(INTERRUPT_AUTO_CONFIRM_KEY) in (None, {})


async def test_ineligible_auto_manual_never_reaches_core_session_remember(
    tmp_path,
) -> None:
    rail, policy, reviewer, base = _rail(
        tmp_path,
        PolicyEvaluation(level="ask", reason="default_ask"),
    )
    _install_core_auto_confirm_contract(base)
    session = _Session()
    unknown_ctx = _runtime_ctx(session, tool_name="edit_file", tool_args={})
    invocation = before_tool_module._extract_invocation((unknown_ctx,), {})
    request = rail._build_runtime_interrupt_request(
        invocation,
        {
            "status": "interrupt",
            "metadata": {"auto_permission_manual": True},
        },
    )
    assert request.auto_confirm_key == ""

    tool_call = ToolCall(
        id="call-1",
        type="function",
        name="edit_file",
        arguments="{}",
    )
    state = ToolInterruptionState(
        ai_message=AssistantMessage(tool_calls=[tool_call]),
        iteration=0,
        interrupted_tools={
            tool_call.id: ToolInterruptEntry(
                tool_call=tool_call,
                interrupt_requests={tool_call.id: request},
            )
        },
        auto_confirm_mapping={tool_call.id: request.auto_confirm_key},
    )
    answer = InteractiveInput()
    answer.update(
        tool_call.id,
        {"approved": True, "auto_confirm": True, "feedback": ""},
    )

    async def execute_tool_call(*_args):
        return [(None, None)]

    handler = ToolInterruptHandler(SimpleNamespace())
    await handler.handle_resume(
        ResumeContext(
            state=state,
            user_input=answer,
            ctx=unknown_ctx,
            context=SimpleNamespace(),
            session=session,
            invoke_inputs=InvokeInputs(query=answer),
            execute_tool_call=execute_tool_call,
        )
    )

    assert session.get_state(INTERRUPT_AUTO_CONFIRM_KEY) in (None, {})

    known_args = {"file_path": str(tmp_path / "report.md")}
    known_ctx = _runtime_ctx(session, tool_name="edit_file", tool_args=known_args)
    result = await rail.before_tool_call(known_ctx)

    assert result is None
    assert len(policy.calls) == 1
    assert len(reviewer.requests) == 1
