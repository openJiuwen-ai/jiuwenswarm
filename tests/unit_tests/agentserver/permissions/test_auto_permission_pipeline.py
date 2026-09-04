"""Current end-to-end decision ordering for the root Auto Permission rail."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace, new_class

import jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.before_tool as before_tool_module
import jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.reviewer_override_consume as override_module
import pytest
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall
from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.interrupt.handler import (
    ResumeContext,
    ToolInterruptHandler,
)
from openjiuwen.core.single_agent.interrupt.state import (
    INTERRUPT_AUTO_CONFIRM_KEY,
    ToolInterruptEntry,
    ToolInterruptionState,
)
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    InvokeInputs,
    ToolCallInputs,
)
from openjiuwen.harness.tools.subagent.subagent_tools import build_subagent_tools
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
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    RootPermissionQueueRail,
    bind_root_permission_request,
    reset_root_permission_request,
)
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
        before_tool_module,
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
        before_tool_module,
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
        agent=SimpleNamespace(),
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
        before_tool_module,
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
