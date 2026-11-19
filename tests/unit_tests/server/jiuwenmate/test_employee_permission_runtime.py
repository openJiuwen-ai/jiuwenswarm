"""Employee automatic permissions use the current isolated reviewer contract."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.foundation.llm.model import Model
from openjiuwen.core.foundation.llm.schema.config import ModelClientConfig, ModelRequestConfig

from jiuwenswarm.server.jiuwenmate.permission_runtime import EmployeePermissionRailFactory


@pytest.mark.parametrize("output_format", ["json_object", "prompt"])
def test_employee_permission_factory_builds_with_current_reviewer(output_format, tmp_path):
    model = Model(
        model_client_config=ModelClientConfig(
            client_provider="OpenAI", api_key="test-key", api_base="https://example.invalid/v1",
        ),
        model_config=ModelRequestConfig(model="test-model"),
    )
    rail = EmployeePermissionRailFactory().build(
        execution=SimpleNamespace(),
        permissions={"enabled": True, "auto": {"reviewer_output_format": output_format}},
        model=model,
        model_name="test-model",
        workspace_root=str(tmp_path),
        sys_operation=None,
        tool_names=("bash",),
    )
    assert rail.auto_reviewer is not None
    assert rail.auto_reviewer.client.output_format == output_format
    assert rail.auto_reviewer.client._model is not model


def test_employee_permission_factory_without_model_preserves_manual_guard(tmp_path):
    rail = EmployeePermissionRailFactory().build(
        execution=SimpleNamespace(), permissions={"enabled": True}, model=None,
        model_name=None, workspace_root=str(tmp_path), sys_operation=None, tool_names=("bash",),
    )
    assert rail.auto_reviewer is None
    assert rail.base_rail is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("output_format", ["json_object", "prompt"])
@pytest.mark.parametrize("outcome", ["allow_once", "deny"])
async def test_employee_automatic_review_controls_tool_execution(output_format, outcome, tmp_path, monkeypatch):
    from openjiuwen.core.foundation.llm import AssistantMessage
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs

    from jiuwenswarm.agents.harness.common.rails.permissions.openjiuwen_contract import (
        classify_permission_result,
    )

    model = Model(
        model_client_config=ModelClientConfig(
            client_provider="OpenAI", api_key="test-key", api_base="https://example.invalid/v1",
        ),
        model_config=ModelRequestConfig(model="test-model"),
    )
    invoke = AsyncMock(return_value=AssistantMessage(content=json.dumps({
        "outcome": outcome, "reason_code": "test_review", "rationale": "Review of the requested search.",
    })))
    monkeypatch.setattr(Model, "invoke", invoke)
    rail = EmployeePermissionRailFactory().build(
        execution=SimpleNamespace(),
        permissions={
            "enabled": True, "defaults": {"*": "ask"},
            "auto": {"reviewer_output_format": output_format},
        },
        model=model, model_name="test-model", workspace_root=str(tmp_path), sys_operation=None,
        tool_names=("mcp_free_search",),
    )
    assert rail.auto_reviewer is not None
    assert rail.auto_reviewer.client._model is not model
    call = ToolCall(
        id="call-search", type="function", name="mcp_free_search",
        arguments='{"query":"public release notes"}',
    )
    ctx = AgentCallbackContext(
        agent=SimpleNamespace(ability_manager=SimpleNamespace(get=lambda _name: None)),
        inputs=ToolCallInputs(
            tool_call=call, tool_name=call.name, tool_args={"query": "public release notes"},
        ),
        session=SimpleNamespace(
            get_session_id=lambda: "session-a", session_id="session-a", get_state=lambda *_args: None,
        ),
        extra={},
    )

    result = await rail.before_tool_call(ctx)

    invoke.assert_awaited_once()
    expected_format = {"type": "json_object"} if output_format == "json_object" else None
    assert invoke.call_args.kwargs.get("response_format") == expected_format
    assert bool(ctx.extra.get("_skip_tool")) == (outcome == "deny")
    assert classify_permission_result(result) == ("allow" if outcome == "allow_once" else "denied")
    metadata = ctx.extra["permission_reviewer_metadata_by_tool_call_id"][call.id]
    assert metadata["final_reviewer_status"] == ("approved" if outcome == "allow_once" else "denied")
    assert metadata["reviewer_called"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
@pytest.mark.parametrize("policy", ["ask", "deny"])
@pytest.mark.parametrize("answer_scope", ["exact", "other_call", "other_owner", "other_kind"])
async def test_employee_consumes_native_confirmation_for_the_exact_call(approved, policy, answer_scope, tmp_path):
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
    from openjiuwen.core.runner.callback import AbortError
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
    from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
    from openjiuwen.core.single_agent.interrupt.state import InterruptAnswer, RESUME_INTERRUPT_ANSWERS_KEY
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
    from openjiuwen.harness.rails.interrupt.confirm_rail import ConfirmPayload

    rail = EmployeePermissionRailFactory().build(
        execution=SimpleNamespace(), permissions={"enabled": True, "defaults": {"*": policy}},
        model=None, model_name=None, workspace_root=str(tmp_path), sys_operation=None,
        tool_names=("search_group_messages",),
    )
    call = ToolCall(id="call-search", type="function", name="search_group_messages", arguments='{"limit":100}')
    owner = rail.base_rail.interrupt_owner
    if answer_scope == "other_owner":
        owner += ".other"
    kind = "confirm" if answer_scope == "other_kind" else "permission"
    request = InterruptRequest(message="Confirm search", interrupt_id="intr-search", interrupt_owner=owner,
                               interrupt_kind=kind)
    answer = InterruptAnswer(owner=owner, kind=kind, request=request,
                             value=ConfirmPayload(approved=approved))
    native_input = InteractiveInput()
    native_input.answer(request.interrupt_id, answer.value)
    ctx = AgentCallbackContext(
        agent=SimpleNamespace(ability_manager=SimpleNamespace(get=lambda _name: None)),
        inputs=ToolCallInputs(tool_call=call, tool_name=call.name, tool_args={"limit": 100}),
        session=SimpleNamespace(get_session_id=lambda: "session-a", session_id="session-a",
                                get_state=lambda *_args: None),
        extra={RESUME_INTERRUPT_ANSWERS_KEY: {
            "other-call" if answer_scope == "other_call" else call.id: {owner: answer}},
               "_jiuwenswarm_root_permission_resume_v1": native_input},
    )
    if answer_scope != "exact" and policy == "ask":
        with pytest.raises(AbortError):
            await rail.before_tool_call(ctx)
        return
    await rail.before_tool_call(ctx)
    assert bool(ctx.extra.get("_skip_tool")) == (not approved or policy == "deny")
